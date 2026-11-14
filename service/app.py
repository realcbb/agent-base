# -*- coding: utf-8 -*-
"""AgentScope Agent Service 装配入口（设计文档第 3、4 章）。

装配内容（全部为探针实测的真实 API，agentscope 2.0.11）：
- create_app(storage, message_bus, workspace_manager, ...) 三件必填；
- storage：默认 AsyncSQLAlchemyStorage（连接串 SERVICE_DB_URL，
  生产为 MySQL），SERVICE_STORAGE=redis 时换 RedisStorage；
- message_bus：由 SERVICE_BUS 独立选择（memory｜redis），与存储
  解耦——生产组合为「MySQL 存储＋Redis 总线」。SERVICE_BUS 未设置
  时沿旧行为（存储 redis 则总线 redis、否则进程内）兼容既有部署；
- workspace_manager：LocalWorkspaceManager（per_session 隔离）；
  装配不种 default_mcps——MCP 一律走注册表，由网关在建会话后
  按租户注入（agent_registry/mcp_servers.yaml）；
- extra_credentials=[MockCredential] 注册 mock 模型；
- enable_channel_worker=False / enable_scheduler=False：排查清单
  H1/D1/D2 的处置——频道子系统（个人助理的 IM 入口假设＋凭证
  明文落库）不属本基座形态；调度触发不归框架管（基座定时需求由
  业务系统/网关调度）。注意实测边界：关调度只停本节点的定时触发
  循环，Schedule* 工具仍挂在会话工具组里（框架 list_tools 不看
  enabled），详见 NOTES；
- resource_access_policy=TenantPrefixPolicy()：项目级共享正门
  （service/access_policy.py）——同租户 {tenant}: 前缀内 KB/凭证
  只读互通，跨租户全拒；
- 身份：框架原生 deps.get_current_user_id 读 X-User-ID 头。本装配加一层
  ASGI 中间件：当请求带网关可信头 X-Tenant-Id + X-End-User-Id 且无
  X-User-ID 时，按设计 5.1 规则注入 X-User-ID = {tenant}:{end_user}。
  生产环境须以网络策略保证本服务只接受网关流量（设计 5.2）。
"""
import os

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus, RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.storage._sql import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import (
    IsolationPolicy,
    LocalWorkspaceManager,
)

from common.config import env, load_env, resolve_path
from service.access_policy import TenantPrefixPolicy
from service.mock_model import MockCredential


class TrustedHeaderIdentityMiddleware:
    """把网关可信头映射为框架的 X-User-ID（纯 ASGI，不改框架源码）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            tenant = headers.get(b"x-tenant-id", b"").decode()
            end_user = headers.get(b"x-end-user-id", b"").decode()
            if tenant and end_user and b"x-user-id" not in headers:
                user_id = f"{tenant}:{end_user}".encode()
                scope["headers"] = list(headers.items()) + [
                    (b"x-user-id", user_id),
                ]
        await self.app(scope, receive, send)


def _parse_redis_addrs(raw: str, default_port: int) -> list:
    addrs = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        host, _, port = part.rpartition(":")
        addrs.append((host, int(port or str(default_port))))
    return addrs


def _redis_mode() -> str:
    """Redis 拓扑模式：standalone｜sentinel｜cluster。

    SERVICE_REDIS_MODE 显式指定优先；未指定时按地址变量推断
    （配了 SENTINELS 走哨兵、配了 CLUSTER_NODES 走集群），保证
    既有部署 env 不变、行为不变。
    """
    mode = env("SERVICE_REDIS_MODE", "").strip().lower()
    if mode:
        return mode
    if env("SERVICE_REDIS_SENTINELS", ""):
        return "sentinel"
    if env("SERVICE_REDIS_CLUSTER_NODES", ""):
        return "cluster"
    return "standalone"


def _patch_k8s_namespace_ensure():
    """共享命名空间部署适配（装配层补丁，框架本体不动）。

    框架 K8sWorkspace.initialize 无条件先 read_namespace（404 才
    create、403 直接抛错），它假设框架独占命名空间且拿得到集群
    级 ClusterRole（get namespaces）。生产是业务共享命名空间：
    命名空间由业务预置、本服务就运行在其中，存在性自明；而
    ClusterRole 申请不了（2026-11-13 用户确认）。故把这一步换
    成容忍 403 的版本：读得到照旧；403 视为「无权核实、按部
    署约定当作存在」放行并记警告；其余错误（含 404）照旧抛，
    真不存在时响亮失败；任何情况下都不 create_namespace。
    """
    import logging

    from agentscope.workspace import K8sWorkspace

    if getattr(K8sWorkspace._ensure_namespace, "_shared_ns", False):
        return

    async def _ensure_namespace_shared(self):
        from kubernetes_asyncio.client.rest import ApiException

        try:
            await self._v1.read_namespace(self._namespace)
        except ApiException as e:
            if e.status == 403:
                logging.getLogger(__name__).warning(
                    "read_namespace 403（无集群级权限）：共享命名"
                    "空间部署，命名空间 %s 由业务预置，跳过存在"
                    "性检查",
                    self._namespace,
                )
                return
            raise

    _ensure_namespace_shared._shared_ns = True  # type: ignore[attr-defined]
    K8sWorkspace._ensure_namespace = _ensure_namespace_shared


def _patch_k8s_write_stream():
    """技能包落盘解死锁（装配层补丁，框架本体不动）。

    框架 K8sBackend.write_stream 用 exec 把字节流喂给 Pod 里的
    ``cat > <path>``，写完只发一个空的 0 通道帧当 EOF——K8s
    exec 协议没有这种信号：cat 的 stdin 只要 websocket 不断就
    一直开着，而 _exec_ws_stdin 又在等进程退出才肯收尾，天生
    死锁。2026-11-14 实测：嵌套 kind 与宿主机直跑的 k3s（真
    kubelet）同签名复现——tar 完整落盘、cat 永不退出、请求永
    不返回（旧「嵌套集群怪癖」结论作废）。

    换法：write_stream 的唯一调用方是 add_skill_archive（包
    体上游有解包上限），先把流在服务侧缓冲成整块，再走原
    _exec_ws_stdin、远端命令换成 ``head -c <总字节数>``——读
    满自行退出，终止由远端进程退出驱动，不再依赖关流信号。
    缓冲上限 64MB，超限响亮报错（真有这种包应走别的通道）。
    """
    import posixpath

    from agentscope.workspace._k8s._k8s_backend import K8sBackend

    if getattr(K8sBackend.write_stream, "_sized_head", False):
        return

    _max_buffer = 64 * 1024 * 1024

    async def _write_stream_sized(self, path, stream):
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            total += len(chunk)
            if total > _max_buffer:
                raise RuntimeError(
                    f"write_stream 缓冲超限（>{_max_buffer} 字"
                    f"节）：{path!r}",
                )
            chunks.append(chunk)
        parent = posixpath.dirname(path) or "/"
        await self.exec_shell(["mkdir", "-p", parent])
        await self._exec_ws_stdin(
            [
                "sh",
                "-c",
                'head -c "$1" > "$2"',
                "sh",
                str(total),
                path,
            ],
            chunks,
            path,
        )

    _write_stream_sized._sized_head = True  # type: ignore[attr-defined]
    K8sBackend.write_stream = _write_stream_sized  # type: ignore[method-assign]


def _redis_sentinel_pool():
    """哨兵连接池：框架的总线/存储都接受外部 connection_pool 并
    原样使用，池由 redis.asyncio.sentinel 维护——取连接时按服务
    名问哨兵要当前主节点，切主后新连接自动跟随，不改框架、也
    不用重启本服务。decode_responses 与框架自建池一致（True）。
    """
    from redis.asyncio.sentinel import Sentinel

    addrs = _parse_redis_addrs(env("SERVICE_REDIS_SENTINELS", ""), 26379)
    # protocol 必须同时钉在 Sentinel 本体与 sentinel_kwargs 上：
    # redis-py 建哨兵节点自身的连接时只取 socket_* 参数，
    # master_for 上的 protocol 管不到它——哨兵连接默认 RESP3
    # 发 HELLO，平台哨兵不认即整链失败（2026-11-13 生产实测：
    # 16:20 的 traceback 就死在 discover_master 这一步）。
    master = Sentinel(
        addrs,
        socket_connect_timeout=5,
        protocol=2,
        sentinel_kwargs={"protocol": 2, "socket_connect_timeout": 5},
    ).master_for(
        env("SERVICE_REDIS_SENTINEL_MASTER", "mymaster"),
        db=0,
        password=env("SERVICE_REDIS_PASSWORD") or None,
        decode_responses=True,
        # 钉 RESP2：redis-py 8 默认 RESP3、建连即发 HELLO 3，
        # 平台 Redis（<6.0 或兼容引擎）不认 HELLO 会整连失败
        #（2026-11-13 生产实测）。本栈命令面纯 RESP2 语义。
        protocol=2,
    )
    return master.connection_pool


class ClusterRedisMessageBus(RedisMessageBus):
    """Cluster 模式的总线。

    框架 RedisMessageBus 的客户端构造写死单机
    Redis(connection_pool=...)，集群客户端套不进它留的池子口；
    但其命令面已逐项核过源码——单键 Lua、单键 delete、Pub/Sub，
    全是集群兼容操作。故只覆写客户端的建立/关闭，命令面仍走
    框架原实现。
    """

    def __init__(self, startup_nodes, password=None):
        super().__init__(host="127.0.0.1", port=6379)  # 占位，不会被用
        self._startup_nodes = startup_nodes
        self._cluster_password = password

    async def __aenter__(self):
        from redis.asyncio.cluster import ClusterNode, RedisCluster

        self._client = RedisCluster(
            startup_nodes=[
                ClusterNode(h, p) for h, p in self._startup_nodes
            ],
            password=self._cluster_password,
            decode_responses=True,
            protocol=2,  # 钉 RESP2，理由见 _redis_sentinel_pool
        )
        # redis-py 集群客户端的槽位缓存是惰性初始化的：首条命令若
        # 是 SUBSCRIBE（框架总线启动即订阅），get_node_from_slot
        # 会因缓存未建而 KeyError（2026-11-13 wstest 实测复现）。
        # 先 ping 一次把拓扑缓存建好，再交回框架使用。
        await self._client.ping()
        return self

    async def aclose(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()
            self._client = None


def build_service_app():
    load_env()
    storage_kind = env("SERVICE_STORAGE", "sql")
    redis_host = env("SERVICE_REDIS_HOST", "127.0.0.1")
    redis_port = int(env("SERVICE_REDIS_PORT", "6379"))
    redis_mode = _redis_mode()
    redis_password = env("SERVICE_REDIS_PASSWORD") or None
    redis_pool = _redis_sentinel_pool() if redis_mode == "sentinel" else None
    # 总线与存储独立选择（2026-11-08 解耦）：SERVICE_BUS 未设置时沿
    # 旧捆绑行为，保证既有部署与冒烟的 env 不变、行为不变。
    bus_kind = env("SERVICE_BUS", "redis" if storage_kind == "redis" else "memory")

    if storage_kind == "redis":
        if redis_mode == "cluster":
            raise RuntimeError(
                "SERVICE_STORAGE=redis 不支持 cluster 模式：框架 "
                "RedisStorage 存在跨槽多键操作（多键 delete、跨键 "
                "pipeline），在 Redis Cluster 上会 CROSSSLOT 失败；"
                "请用默认的 sql 存储（生产组合即 MySQL）",
            )
        storage = (
            RedisStorage(connection_pool=redis_pool)
            if redis_pool is not None
            else RedisStorage(
                host=redis_host, port=redis_port, password=redis_password,
                protocol=2,  # 钉 RESP2，理由见 _redis_sentinel_pool
            )
        )
    else:
        db_url = env(
            "SERVICE_DB_URL",
            "sqlite+aiosqlite:///./data/service.db",
        )
        if db_url.startswith("sqlite"):
            db_path = db_url.split("///", 1)[-1]
            resolve_path(db_path).parent.mkdir(parents=True, exist_ok=True)
        storage = AsyncSQLAlchemyStorage(url=db_url, create_tables=True)

    if bus_kind == "redis":
        if redis_mode == "cluster":
            message_bus = ClusterRedisMessageBus(
                _parse_redis_addrs(env("SERVICE_REDIS_CLUSTER_NODES", ""), 6379),
                password=redis_password,
            )
        elif redis_pool is not None:
            message_bus = RedisMessageBus(connection_pool=redis_pool)
        else:
            message_bus = RedisMessageBus(
                host=redis_host, port=redis_port, password=redis_password,
                protocol=2,  # 钉 RESP2，理由见 _redis_sentinel_pool
            )
    else:
        message_bus = InMemoryMessageBus()

    # 【装配锁定 · 勿改】隔离粒度必须是 PER_SESSION：框架管理器默认
    # PER_AGENT（同 agent 新会话继承旧工作区文件＝个人桌面语义＋数据
    # 串扰面，排查清单 W2）；PER_USER 更是直接跟人走。改回任何一档
    # 都要先过注册表/清单评审。local 与 k8s 两个分支都显式传。
    workspace_kind = env("SERVICE_WORKSPACE", "local")
    if workspace_kind == "k8s":
        # K8s 工作区（生产目标形态，内网集群）：每工作区一个 Pod＋
        # PVC（PVC 名由 workspace_id 确定性派生，任一服务副本都能
        # 按名找回——服务多实例的前提）；工具在 Pod 内执行。服务
        # 进程需集群内身份（in-cluster）或 kubeconfig，且其账号须
        # 有在工作区命名空间管理 Pod/PVC 的 RBAC。依赖
        # kubernetes-asyncio 已入 pyproject 锁定。本分支尚未在真实
        # 集群实测，接入时须按 DEPLOY.md 的 K8s 一节验收。
        from agentscope.app.workspace_manager import (
            K8sWorkspaceManager,
        )

        _patch_k8s_namespace_ensure()
        _patch_k8s_write_stream()

        pull_secrets = [
            s.strip()
            for s in env("SERVICE_K8S_IMAGE_PULL_SECRETS", "").split(",")
            if s.strip()
        ]

        def _json_env(name: str):
            raw = env(name, "")
            if not raw:
                return None
            import json

            return json.loads(raw)

        workspace_manager = K8sWorkspaceManager(
            isolation=IsolationPolicy.PER_SESSION,
            namespace=env("SERVICE_K8S_NAMESPACE", "agentscope"),
            image=env("SERVICE_K8S_IMAGE", "python:3.11-slim"),
            image_pull_secrets=pull_secrets or None,
            storage_class=env("SERVICE_K8S_STORAGE_CLASS") or None,
            storage_size=env("SERVICE_K8S_STORAGE_SIZE", "1Gi"),
            # 工作区 Pod 的身份与资源（共享命名空间部署时是刚需）：
            # service_account 指定专用零权限账号——工作区是不可信
            # 执行面，绝不能继承命名空间 default 账号（共享集群里
            # 它常带业务权限）；resources 给 Pod 定 requests/
            # limits，避免 BestEffort 挤占同命名空间的业务容器；
            # node_selector/tolerations 用于把工作区钉到专用节点
            # 池。三者均为 JSON env，格式见 .env.example。
            service_account=env("SERVICE_K8S_SERVICE_ACCOUNT") or None,
            resources=_json_env("SERVICE_K8S_RESOURCES"),
            node_selector=_json_env("SERVICE_K8S_NODE_SELECTOR"),
            tolerations=_json_env("SERVICE_K8S_TOLERATIONS"),
        )
    else:
        workspace_dir = resolve_path(
            env("SERVICE_WORKSPACE_DIR", "./data/workspaces"),
        )
        workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_manager = LocalWorkspaceManager(
            basedir=str(workspace_dir),
            isolation=IsolationPolicy.PER_SESSION,
        )

    # 【装配锁定 · 勿改】跨租户姿态：默认全拒是底线。框架不传策略时
    # 默认 DenyAll；本装配传 TenantPrefixPolicy 是唯一受控例外——只在
    # 同一租户前缀内放行 KB/凭证的 READ，跨租户仍然全拒，AGENT 默认
    # 不放行。不许换成放行面更大的策略，不许给跨 user EDIT。
    # 【装配锁定 · 勿改】长期记忆 middleware 默认不装配：create_app
    # 的 extra_agent_middlewares 有意不传。框架的 AgenticMemory 是
    # 「个人记忆册」模型（排查清单 M1/M2），项目级知识走 KB/注册表
    # 通道；将来真要启用，须在网关接入评审里显式定粒度与检索成本。
    app = create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_credentials=[MockCredential],
        enable_channel_worker=False,
        enable_scheduler=False,
        resource_access_policy=TenantPrefixPolicy(),
        title="AgentScope (agent-base assembly)",
    )
    app.add_middleware(TrustedHeaderIdentityMiddleware)
    return app


app = build_service_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SERVICE_PORT", "8100")),
    )
