# -*- coding: utf-8 -*-
"""确认台账与审计账：SQLAlchemy 落库（生产口径 MySQL）。

为什么需要这一本账（2026-11-09 源码复核后定）：
- 会话正文（用户/助手消息）由基座服务存储供数（原生
  GET /sessions/{id}/messages），网关不再自拼；
- 但"确认卡"不是消息：决定一旦落定，原生消息里只剩
  state=finished 的工具调用——谁在何时批/拒了哪张卡，
  原生侧不保存。这部分是网关契约层的事实，必须由网关
  自己持久化，且不能放在可淘汰的状态后端（Redis/内存）
  里——那正是旧投影"状态一丢就失忆"的病根。
- 同理，关键动作的审计（建/关会话、确认决定）也要一份
  只追加的账，与运行日志分开。

连接（AGENT_BASE_LEDGER_URL，同步驱动 URL），解析顺序与
usage_store 一致：显式 URL → 从 SERVICE_DB_URL 推导；两者皆无
则落库不可用（healthy=False），不静默落本地 sqlite 文件
（2026-11-14 用户裁定去掉兜底）。

失败口径与 usage_store 一致：落库不可用时静默降级
（healthy=False），绝不拖垮网关主链；但要在日志里留痕，
且 /v1/audit 与历史里的确认条目会随之缺失——生产部署
必须保证本账可用（见 DEPLOY.md）。
"""
import json
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
    Text,
    create_engine,
    select,
)

from common.config import env

logger = logging.getLogger(__name__)

_METADATA = MetaData()
CONFIRMATIONS = Table(
    "confirmation_ledger",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("confirmation_id", String(64), nullable=False, unique=True),
    Column("session_id", String(128), nullable=False),
    Column("tenant", String(128), nullable=False),
    Column("end_user", String(128), nullable=False, default=""),
    Column("agent_id", String(256), nullable=False, default=""),
    Column("run_id", String(128), nullable=False, default=""),
    Column("tool_calls", Text, nullable=False, default="[]"),
    Column("impact", Text, nullable=False, default=""),
    Column("status", String(32), nullable=False, default="pending"),
    Column("decision", String(32), nullable=False, default=""),
    Column("decisions", Text, nullable=False, default=""),
    Column("decided_by", String(128), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Column("decided_at", String(40), nullable=False, default=""),
    Index("idx_confirmation_ledger_session", "session_id"),
)
AUDIT_EVENTS = Table(
    "audit_events",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(40), nullable=False),
    Column("tenant", String(128), nullable=False),
    Column("end_user", String(128), nullable=False, default=""),
    Column("action", String(64), nullable=False),
    Column("session_id", String(128), nullable=False, default=""),
    Column("ref_id", String(128), nullable=False, default=""),
    Column("detail", Text, nullable=False, default=""),
    Index("idx_audit_events_tenant", "tenant"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_ledger_url() -> str:
    url = env("AGENT_BASE_LEDGER_URL")
    if url:
        return url
    service_url = env("SERVICE_DB_URL", "")
    if service_url:
        return service_url.replace("+aiomysql", "+pymysql").replace(
            "+aiosqlite", "",
        )
    return ""


class LedgerStore:
    """线程安全的台账存储；不可用时静默降级（healthy=False）。"""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or default_ledger_url()
        self._lock = threading.Lock()
        self._engine = None
        self._failed = False
        try:
            if not self.url:
                raise RuntimeError(
                    "未配置连接（AGENT_BASE_LEDGER_URL 与 "
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
                _METADATA.create_all(
                    conn, tables=[CONFIRMATIONS, AUDIT_EVENTS],
                )
        except Exception as exc:
            logger.warning("台账落库不可用（%s）：%s", self.url, exc)
            self._engine = None
            self._failed = True

    @property
    def healthy(self) -> bool:
        return self._engine is not None and not self._failed

    # ------------------------------------------------------ 确认台账
    def put_confirmation(
        self,
        *,
        confirmation_id: str,
        session_id: str,
        tenant: str,
        end_user: str,
        agent_id: str,
        run_id: str,
        tool_calls: list,
        impact: str,
    ) -> None:
        if not self.healthy:
            return
        try:
            with self._lock, self._engine.begin() as conn:
                conn.execute(
                    CONFIRMATIONS.insert().values(
                        confirmation_id=confirmation_id,
                        session_id=session_id,
                        tenant=tenant,
                        end_user=end_user,
                        agent_id=agent_id,
                        run_id=run_id,
                        tool_calls=json.dumps(tool_calls, ensure_ascii=False),
                        impact=impact,
                        status="pending",
                        created_at=_now(),
                    ),
                )
        except Exception as exc:
            logger.warning("确认台账写入失败：%s", exc)

    def mark_decided(
        self,
        confirmation_id: str,
        *,
        status: str,
        decision: str,
        decisions: list | None,
        decided_by: str,
    ) -> None:
        if not self.healthy:
            return
        try:
            with self._lock, self._engine.begin() as conn:
                conn.execute(
                    CONFIRMATIONS.update()
                    .where(
                        CONFIRMATIONS.c.confirmation_id == confirmation_id,
                    )
                    .values(
                        status=status,
                        decision=decision,
                        decisions=json.dumps(
                            decisions or [], ensure_ascii=False,
                        ),
                        decided_by=decided_by,
                        decided_at=_now(),
                    ),
                )
        except Exception as exc:
            logger.warning("确认台账回写失败：%s", exc)

    def list_confirmations(self, session_id: str) -> list[dict]:
        if not self.healthy:
            return []
        try:
            with self._lock, self._engine.connect() as conn:
                rows = conn.execute(
                    select(CONFIRMATIONS)
                    .where(CONFIRMATIONS.c.session_id == session_id)
                    .order_by(CONFIRMATIONS.c.id),
                ).mappings()
                out = []
                for row in rows:
                    item = dict(row)
                    item["tool_calls"] = json.loads(
                        item.get("tool_calls") or "[]",
                    )
                    item["decisions"] = json.loads(
                        item.get("decisions") or "[]",
                    )
                    out.append(item)
                return out
        except Exception as exc:
            logger.warning("确认台账读取失败：%s", exc)
            return []

    # ---------------------------------------------------------- 审计
    def append_audit(
        self,
        *,
        tenant: str,
        end_user: str,
        action: str,
        session_id: str = "",
        ref_id: str = "",
        detail: dict | None = None,
    ) -> None:
        if not self.healthy:
            return
        try:
            with self._lock, self._engine.begin() as conn:
                conn.execute(
                    AUDIT_EVENTS.insert().values(
                        ts=_now(),
                        tenant=tenant,
                        end_user=end_user,
                        action=action,
                        session_id=session_id,
                        ref_id=ref_id,
                        detail=json.dumps(detail or {}, ensure_ascii=False),
                    ),
                )
        except Exception as exc:
            logger.warning("审计写入失败：%s", exc)

    def list_audit(self, tenant: str, limit: int = 100) -> list[dict]:
        if not self.healthy:
            return []
        try:
            with self._lock, self._engine.connect() as conn:
                rows = conn.execute(
                    select(AUDIT_EVENTS)
                    .where(AUDIT_EVENTS.c.tenant == tenant)
                    .order_by(AUDIT_EVENTS.c.id.desc())
                    .limit(limit),
                ).mappings()
                out = []
                for row in rows:
                    item = dict(row)
                    item["detail"] = json.loads(item.get("detail") or "{}")
                    out.append(item)
                return out
        except Exception as exc:
            logger.warning("审计读取失败：%s", exc)
            return []
