# -*- coding: utf-8 -*-
"""usage 流水落库：逐模型调用一行，SQLAlchemy 存储（生产口径 MySQL）。

设计依据：基座设计文档第 9 章（模型策略/计量）；粒度对齐官方 Java 版
AgentScope Service 的逐次调用计量记录——不落汇总数、只落原始流水，
汇总口径以后怎么变都可由流水重算。

连接（AGENT_BASE_USAGE_URL，同步驱动 URL），解析顺序：
1. AGENT_BASE_USAGE_URL 直接给全量 URL（生产：
   mysql+pymysql://用户:密码@主机:3306/库名?charset=utf8mb4，
   与基座服务存储同实例即可——本表名为 usage_calls，与服务表不冲突）；
2. 未给时从 SERVICE_DB_URL 推导（aiomysql→pymysql、aiosqlite→sqlite），
   即默认与服务存储同库；两者皆无则落库不可用（healthy=False），
   不再静默落本地 sqlite 文件——多副本网关下各写各的、账目分裂
   （2026-11-14 用户裁定去掉兜底；旧变量 AGENT_BASE_USAGE_DB 随之退役）。

口径：
- 一行 = 一次模型调用（adapter 在 ModelCallEndEvent 处写入）。
- /v1/usage 的 runs = 该租户产生过至少一次模型调用的 run 数
  （流水表 COUNT(DISTINCT run_id)），input/output 为流水求和；
  数值跨 gateway 进程重启累计不清零；多网关副本共写同一库。
- 写入失败只记日志并置降级标记（healthy=False），绝不向上抛；
  adapter 据此回退到进程内内存聚合（与落库前行为一致）。
"""
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
)

from common.config import env

logger = logging.getLogger(__name__)

_METADATA = MetaData()
USAGE_CALLS = Table(
    "usage_calls",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(40), nullable=False),
    Column("tenant", String(128), nullable=False),
    Column("end_user", String(128), nullable=False, default=""),
    Column("agent_id", String(256), nullable=False, default=""),
    Column("session_id", String(128), nullable=False, default=""),
    Column("run_id", String(128), nullable=False, default=""),
    Column("model", String(256), nullable=False, default=""),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("total_tokens", Integer, nullable=False, default=0),
    Column("token_source", String(32), nullable=False, default="model_reported"),
    Index("idx_usage_calls_tenant", "tenant"),
)


def default_usage_url() -> str:
    """按模块 docstring 的顺序解析 usage 库连接 URL。"""
    url = env("AGENT_BASE_USAGE_URL")
    if url:
        return url
    service_url = env("SERVICE_DB_URL", "")
    if service_url:
        derived = service_url.replace("+aiomysql", "+pymysql").replace(
            "+aiosqlite", "",
        )
        return derived
    return ""


class UsageStore:
    """线程安全的流水存储；不可用时静默降级（healthy=False）。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        url: str | None = None,
    ) -> None:
        if url is None and db_path is not None:
            url = f"sqlite:///{Path(db_path)}"
        self.url = url or default_usage_url()
        self._lock = threading.Lock()
        self._engine = None
        self._failed = False
        try:
            if not self.url:
                raise RuntimeError(
                    "未配置连接（AGENT_BASE_USAGE_URL 与 "
                    "SERVICE_DB_URL 均未设置）",
                )
            if self.url.startswith("sqlite:///"):
                Path(self.url[len("sqlite:///"):]).parent.mkdir(
                    parents=True, exist_ok=True,
                )
            self._engine = create_engine(
                self.url, pool_pre_ping=True, future=True,
            )
            with self._lock, self._engine.begin() as conn:
                _METADATA.create_all(conn, tables=[USAGE_CALLS])
        except Exception as exc:  # 建库失败不拖垮网关，走内存降级
            logger.warning("usage 落库不可用（%s）：%s", self.url, exc)
            self._engine = None
            self._failed = True

    @property
    def healthy(self) -> bool:
        return self._engine is not None and not self._failed

    def record_call(
        self,
        *,
        tenant: str,
        end_user: str = "",
        agent_id: str = "",
        session_id: str = "",
        run_id: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        token_source: str = "model_reported",
    ) -> bool:
        """写一行逐调用流水。成功 True；失败记日志、置降级标记、返回 False。"""
        if not self.healthy:
            return False
        try:
            with self._lock, self._engine.begin() as conn:
                conn.execute(
                    USAGE_CALLS.insert().values(
                        ts=datetime.now(timezone.utc).isoformat(),
                        tenant=tenant,
                        end_user=end_user,
                        agent_id=agent_id,
                        session_id=session_id,
                        run_id=run_id,
                        model=model,
                        input_tokens=int(input_tokens or 0),
                        output_tokens=int(output_tokens or 0),
                        total_tokens=int(input_tokens or 0)
                        + int(output_tokens or 0),
                        token_source=token_source,
                    ),
                )
            return True
        except Exception as exc:
            logger.warning("usage 流水写入失败，降级内存聚合：%s", exc)
            self._failed = True
            return False

    def count_calls(self, tenant: str) -> int:
        """该租户的流水行数（验收/巡检用；异常返回 -1）。"""
        if not self.healthy:
            return -1
        try:
            with self._lock, self._engine.connect() as conn:
                return int(
                    conn.execute(
                        select(func.count())
                        .select_from(USAGE_CALLS)
                        .where(USAGE_CALLS.c.tenant == tenant),
                    ).scalar_one(),
                )
        except Exception as exc:
            logger.warning("usage 流水计数失败：%s", exc)
            return -1

    def totals(self, tenant: str) -> dict:
        """流水原始合计＋一行样本（验收直查用；异常返回空字典）。"""
        if not self.healthy:
            return {}
        try:
            with self._lock, self._engine.connect() as conn:
                rows, in_tok, out_tok, total = conn.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(USAGE_CALLS.c.input_tokens), 0),
                        func.coalesce(func.sum(USAGE_CALLS.c.output_tokens), 0),
                        func.coalesce(func.sum(USAGE_CALLS.c.total_tokens), 0),
                    ).where(USAGE_CALLS.c.tenant == tenant),
                ).one()
                sample = conn.execute(
                    select(USAGE_CALLS)
                    .where(USAGE_CALLS.c.tenant == tenant)
                    .limit(1),
                ).mappings().first()
        except Exception as exc:
            logger.warning("usage 流水直查失败：%s", exc)
            return {}
        return {
            "rows": int(rows or 0),
            "input_tokens": int(in_tok or 0),
            "output_tokens": int(out_tok or 0),
            "total_tokens": int(total or 0),
            "sample": dict(sample) if sample else None,
        }

    def aggregate(self, tenant: str) -> dict:
        """按租户聚合出 /v1/usage 的响应主体（不含 tenant 字段本身）。

        租户隔离：所有查询都带 tenant 条件，只能看到本租户的流水。
        存储异常时返回零值字典（调用方决定是否回退内存聚合）。
        """
        empty = {
            "runs": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "token_source": "none",
            "by_agent": {},
        }
        if not self.healthy:
            return empty
        try:
            with self._lock, self._engine.connect() as conn:
                runs, in_tok, out_tok = conn.execute(
                    select(
                        func.count(func.distinct(USAGE_CALLS.c.run_id)),
                        func.coalesce(func.sum(USAGE_CALLS.c.input_tokens), 0),
                        func.coalesce(func.sum(USAGE_CALLS.c.output_tokens), 0),
                    ).where(USAGE_CALLS.c.tenant == tenant),
                ).one()
                rows = conn.execute(
                    select(
                        USAGE_CALLS.c.agent_id,
                        func.count(func.distinct(USAGE_CALLS.c.run_id)),
                    )
                    .where(USAGE_CALLS.c.tenant == tenant)
                    .group_by(USAGE_CALLS.c.agent_id),
                ).all()
        except Exception as exc:
            logger.warning("usage 聚合查询失败：%s", exc)
            self._failed = True
            return empty
        input_tokens = int(in_tok or 0)
        output_tokens = int(out_tok or 0)
        return {
            "runs": int(runs or 0),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "token_source": (
                "model_reported" if (input_tokens or output_tokens) else "none"
            ),
            "by_agent": {agent_id: int(n) for agent_id, n in rows},
        }
