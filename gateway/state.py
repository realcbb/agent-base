# -*- coding: utf-8 -*-
"""网关状态后端：events / runs / 限流的可替换存储（网关高可用地基）。

单实例时代这些状态都在网关进程内存里，网关因此只能开一个副本。
本模块把它们抽成后端接口：

- memory（默认）：进程内字典，行为与单实例时代完全一致，本地开发
  与冒烟用；
- redis（生产）：events 落 Redis List 并经 Pub/Sub 扇出、runs 落
  Redis KV、限流走 Redis 有序集合滑动窗口。多个网关副本共享同一
  份状态：任一副本产生的事件/轮次，其他副本可读可订阅；限流按
  租户全局生效（不再是副本数倍）。

仍留在进程内的（有意为之，均可重建）：会话句柄（可凭服务侧探测
重建）、待确认（可凭服务侧会话状态重建）、开通缓存（幂等重复
执行无害）。在途轮次的执行本身不迁移：执行副本死亡时该轮记录
停在 running，由 adapter.get_run 的超时对账置 failed（见
adapter），业务侧据 run.failed 语义重试即可。

选择：GATEWAY_STATE_BACKEND=memory|redis（默认 memory）；Redis
连接取 GATEWAY_REDIS_HOST/PORT，未设时回退 SERVICE_REDIS_*。
"""
import asyncio
import json
import time
from collections import defaultdict, deque

from common.config import env

_EVENTS_CAP = 10000  # 每会话事件留存上限（redis list 裁剪）


def _after(events: list[dict], after_event_id: str | None) -> list[dict]:
    if not after_event_id:
        return list(events)
    for i, ev in enumerate(events):
        if ev.get("event_id") == after_event_id:
            return list(events[i + 1:])
    return list(events)


class MemoryState:
    """进程内状态后端（单副本/开发）。"""

    kind = "memory"

    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._runs: dict[str, dict] = {}
        self._session_runs: dict[str, list[str]] = defaultdict(list)
        self._rate: dict[str, deque] = defaultdict(deque)

    # ---------------------------------------------------------- events
    def append_event(self, event: dict) -> None:
        sid = event["session_id"]
        self._events[sid].append(event)
        for q in list(self._subscribers.get(sid, ())):
            q.put_nowait(event)

    def list_events(
        self, session_id: str, after_event_id: str | None = None,
    ) -> list[dict]:
        return _after(self._events.get(session_id, []), after_event_id)

    async def follow_events(
        self, session_id: str, after_event_id: str | None = None,
    ):
        """先回放、再实时跟随；15 秒无事件产出 None（心跳哨兵）。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[session_id].add(q)
        seen: set[str] = set()
        try:
            for ev in self.list_events(session_id, after_event_id):
                seen.add(ev["event_id"])
                yield ev
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if ev["event_id"] in seen:
                    continue
                seen.add(ev["event_id"])
                yield ev
        finally:
            self._subscribers[session_id].discard(q)

    # ------------------------------------------------------------- runs
    def put_run(self, run: dict) -> None:
        self._runs[run["run_id"]] = dict(run)
        self._session_runs[run["session_id"]].append(run["run_id"])

    def get_run(self, run_id: str) -> dict | None:
        run = self._runs.get(run_id)
        return dict(run) if run else None

    def update_run(self, run_id: str, **fields) -> dict | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        run.update(fields)
        return dict(run)

    def list_session_runs(self, session_id: str) -> list[dict]:
        return [
            dict(self._runs[rid])
            for rid in self._session_runs.get(session_id, [])
            if rid in self._runs
        ]

    # -------------------------------------------------------- rate limit
    def rate_allowed(self, tenant: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        dq = self._rate[tenant]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def _parse_addrs(raw: str, default_port: int) -> list:
    addrs = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        host, _, port = part.rpartition(":")
        addrs.append((host, int(port or str(default_port))))
    return addrs


class RedisState:
    """Redis 状态后端（生产多副本）。支持三种拓扑（mode）：

    - standalone：host:port 直连；
    - sentinel：哨兵地址组＋服务名，客户端经哨兵问当前主节点，
      切主后新连接自动跟随；
    - cluster：集群启动节点组，走 RedisCluster 客户端。事件键名
      带 {session_id} 哈希标签（append 的 pipeline 需同槽），其余
      模式下花括号只是普通字符、行为不变。
    """

    kind = "redis"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        mode: str = "standalone",
        sentinels: list | None = None,
        master_name: str = "mymaster",
        cluster_nodes: list | None = None,
        password: str | None = None,
    ) -> None:
        import redis

        self._r_cached = None
        self._mode = mode
        self._host = host
        self._port = port
        self._sentinels = sentinels
        self._master_name = master_name
        self._cluster_nodes = cluster_nodes
        self._password = password
        if mode == "sentinel":
            import redis.sentinel

            self._r = redis.sentinel.Sentinel(
                sentinels, socket_connect_timeout=5,
            ).master_for(
                master_name,
                decode_responses=True,
                password=password,
                socket_connect_timeout=5,
            )
        elif mode == "cluster":
            # RedisCluster 同步客户端构造期即做拓扑发现（连不上直
            # 接抛错），与单机/哨兵的惰性构造不同——延迟到首次实
            # 际调用时再建（见下方 _r 属性）。
            self._r = None
        else:
            self._r = redis.Redis(
                host=host, port=port, decode_responses=True,
                password=password,
                socket_connect_timeout=5,
            )

    @property
    def _r(self):  # type: ignore[no-redef]
        if self._r_cached is None:
            import redis.cluster

            self._r_cached = redis.cluster.RedisCluster(
                startup_nodes=[
                    redis.cluster.ClusterNode(h, p)
                    for h, p in self._cluster_nodes or []
                ],
                decode_responses=True,
                password=self._password,
                socket_connect_timeout=5,
            )
        return self._r_cached

    @_r.setter
    def _r(self, value) -> None:
        self._r_cached = value

    def _async_client(self):
        """follow_events 用的异步客户端，与同步侧同拓扑。"""
        import redis.asyncio as aioredis

        if self._mode == "sentinel":
            import redis.asyncio.sentinel

            return redis.asyncio.sentinel.Sentinel(
                self._sentinels, socket_connect_timeout=5,
            ).master_for(
                self._master_name,
                decode_responses=True,
                password=self._password,
                socket_connect_timeout=5,
            )
        if self._mode == "cluster":
            import redis.asyncio.cluster

            return redis.asyncio.cluster.RedisCluster(
                startup_nodes=[
                    redis.asyncio.cluster.ClusterNode(h, p)
                    for h, p in self._cluster_nodes or []
                ],
                decode_responses=True,
                password=self._password,
                socket_connect_timeout=5,
            )
        return aioredis.Redis(
            host=self._host, port=self._port, decode_responses=True,
            password=self._password,
        )

    # ---------------------------------------------------------- events
    def append_event(self, event: dict) -> None:
        sid = event["session_id"]
        raw = json.dumps(event, ensure_ascii=False)
        pipe = self._r.pipeline()
        pipe.rpush(f"gw:events:{{{sid}}}", raw)
        pipe.ltrim(f"gw:events:{{{sid}}}", -_EVENTS_CAP, -1)
        pipe.publish(f"gw:live:{{{sid}}}", raw)
        pipe.execute()

    def list_events(
        self, session_id: str, after_event_id: str | None = None,
    ) -> list[dict]:
        raw = self._r.lrange(f"gw:events:{{{session_id}}}", 0, -1)
        return _after([json.loads(x) for x in raw], after_event_id)

    async def follow_events(
        self, session_id: str, after_event_id: str | None = None,
    ):
        client = self._async_client()
        if self._mode == "cluster":
            # 同总线侧的坑：集群客户端槽位缓存惰性初始化，首条命令
            # 是 SUBSCRIBE 会 KeyError，先 ping 建好拓扑缓存。
            await client.ping()
        pubsub = client.pubsub()
        # 先订阅再回放：回放期间新事件进 pubsub 缓冲，不丢；重叠部分
        # 靠 seen 集合去重。
        await pubsub.subscribe(f"gw:live:{{{session_id}}}")
        seen: set[str] = set()
        try:
            for ev in self.list_events(session_id, after_event_id):
                seen.add(ev["event_id"])
                yield ev
            while True:
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=15,
                        ),
                        timeout=20,
                    )
                except asyncio.TimeoutError:
                    yield None
                    continue
                if msg is None:
                    yield None
                    continue
                ev = json.loads(msg["data"])
                if ev["event_id"] in seen:
                    continue
                seen.add(ev["event_id"])
                yield ev
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.close()
                await client.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------- runs
    def put_run(self, run: dict) -> None:
        if self._mode == "cluster":
            # 两键分属不同槽位，集群下不能同 pipeline（CROSSSLOT）；
            # 顺序执行，语义不变（这对写入原本也不依赖跨键原子性）。
            self._r.set(
                f"gw:run:{run['run_id']}",
                json.dumps(run, ensure_ascii=False),
            )
            self._r.rpush(
                f"gw:session_runs:{run['session_id']}", run["run_id"],
            )
            return
        pipe = self._r.pipeline()
        pipe.set(
            f"gw:run:{run['run_id']}",
            json.dumps(run, ensure_ascii=False),
        )
        pipe.rpush(f"gw:session_runs:{run['session_id']}", run["run_id"])
        pipe.execute()

    def get_run(self, run_id: str) -> dict | None:
        raw = self._r.get(f"gw:run:{run_id}")
        return json.loads(raw) if raw else None

    def update_run(self, run_id: str, **fields) -> dict | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        run.update(fields)
        self._r.set(f"gw:run:{run_id}", json.dumps(run, ensure_ascii=False))
        return run

    def list_session_runs(self, session_id: str) -> list[dict]:
        ids = self._r.lrange(f"gw:session_runs:{session_id}", 0, -1)
        out = []
        for rid in ids:
            run = self.get_run(rid)
            if run:
                out.append(run)
        return out

    # -------------------------------------------------------- rate limit
    def rate_allowed(self, tenant: str, limit: int, window: float = 60.0) -> bool:
        key = f"gw:rl:{tenant}"
        now = time.time()
        pipe = self._r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zcard(key)
        _, count = pipe.execute()
        if count >= limit:
            return False
        pipe = self._r.pipeline()
        pipe.zadd(key, {f"{now}-{time.monotonic_ns()}": now})
        pipe.expire(key, int(window) + 5)
        pipe.execute()
        return True


def build_state():
    """按 GATEWAY_STATE_BACKEND 建状态后端（默认 memory）。

    redis 拓扑三模式（standalone｜sentinel｜cluster）：变量优先读
    GATEWAY_REDIS_*，缺项回退 SERVICE_REDIS_*（与 HOST/PORT 的既有
    回退口径一致）；模式未显式给时按地址变量推断。
    """
    kind = env("GATEWAY_STATE_BACKEND", "memory")
    if kind == "redis":
        def pick(name: str) -> str:
            return env(f"GATEWAY_REDIS_{name}") or env(
                f"SERVICE_REDIS_{name}", "",
            )

        sentinels_raw = pick("SENTINELS")
        cluster_raw = pick("CLUSTER_NODES")
        mode = (pick("MODE") or "").strip().lower()
        if not mode:
            mode = (
                "sentinel" if sentinels_raw
                else "cluster" if cluster_raw
                else "standalone"
            )
        host = env("GATEWAY_REDIS_HOST") or env(
            "SERVICE_REDIS_HOST", "127.0.0.1",
        )
        port = int(
            env("GATEWAY_REDIS_PORT") or env("SERVICE_REDIS_PORT", "6379"),
        )
        return RedisState(
            host,
            port,
            mode=mode,
            sentinels=_parse_addrs(sentinels_raw, 26379),
            master_name=pick("SENTINEL_MASTER") or "mymaster",
            cluster_nodes=_parse_addrs(cluster_raw, 6379),
            password=pick("PASSWORD") or None,
        )
    return MemoryState()
