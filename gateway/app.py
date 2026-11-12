# -*- coding: utf-8 -*-
"""薄网关：对外契约 v1 核心子集（设计文档第 7 章）。

职责（设计 3.2/5.2）：X-Project-Key 鉴权、租户映射、简单限流、
统一错误信封（7.4）、契约事件（7.2）、HITL 确认回路（7.3）。
内部经 gateway.adapter 走 HTTP 调用 AgentScope Agent Service
（adapter 隔离框架原生 API；会话与消息历史在服务存储里）。
"""
import hashlib
import hmac
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, model_validator

from agent_registry.loader import load_agent, load_agents
from common.config import env, load_env, parse_project_keys
from common.errors import error_body, new_trace_id
from gateway.adapter import (
    AgentScopeAdapter,
    ModelCapabilityError,
    SessionBusyError,
)

load_env()

TENANT_KEYS = parse_project_keys(env("PROJECT_KEYS"))
KEY_TO_TENANT = {v: k for k, v in TENANT_KEYS.items()}
RATE_LIMIT = int(env("GATEWAY_RATE_LIMIT_PER_MIN", "120"))
# 按终端用户限流（0=关）：在租户级之下再按 (租户, 工号) 分桶，
# 防单个工号刷爆全租户陪绑。
USER_RATE_LIMIT = int(env("GATEWAY_RATE_LIMIT_PER_USER_PER_MIN", "0"))
# 终端身份断言校验（0=关）：业务后端不再只是"声称"工号——
# 开启后凡带 X-End-User-Id 的请求必须附带断言签名
# X-End-User-Proof = HMAC_SHA256(项目密钥, "{工号}.{时间戳}")
#（十六进制）与 X-End-User-Ts（秒级时间戳，±300 秒内有效）。
# 项目密钥只有业务后端与网关持有，工号自此可验真伪。
REQUIRE_USER_PROOF = env("GATEWAY_REQUIRE_USER_PROOF", "0") == "1"

adapter = AgentScopeAdapter()

app = FastAPI(title="agent-base gateway", version="1.0")


class SessionCreate(BaseModel):
    agent_id: str


IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGES_PER_MESSAGE = 8
MAX_IMAGE_B64_LEN = 14_000_000  # ≈10MB 二进制，防超大图打爆上下文与存储


class ImageInput(BaseModel):
    """一条图片输入：url 与 data_base64 二选一（契约 v1 加法字段）。"""

    url: str | None = None
    data_base64: str | None = None
    media_type: str

    @model_validator(mode="after")
    def _validate(self):
        if bool(self.url) == bool(self.data_base64):
            raise ValueError("url 与 data_base64 必须二选一")
        if self.media_type not in IMAGE_MEDIA_TYPES:
            raise ValueError(f"不支持的图片类型：{self.media_type}")
        if self.data_base64 and len(self.data_base64) > MAX_IMAGE_B64_LEN:
            raise ValueError("图片过大（base64 超过上限）")
        return self

    def to_source(self) -> dict:
        if self.url:
            return {"type": "url", "url": self.url, "media_type": self.media_type}
        return {
            "type": "base64",
            "data": self.data_base64,
            "media_type": self.media_type,
        }


class MessageCreate(BaseModel):
    text: str = ""
    images: list[ImageInput] | None = None

    @model_validator(mode="after")
    def _validate(self):
        if self.images and len(self.images) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"单条消息图片数超过上限 {MAX_IMAGES_PER_MESSAGE}")
        if not self.text.strip() and not self.images:
            raise ValueError("text 与 images 至少要有一个非空")
        return self


class CallDecision(BaseModel):
    """逐调用决定条目：按 tool_call_id 定位（缺省按序匹配）。"""

    tool_call_id: str | None = None
    decision: str  # approved / rejected / edited
    edited_input: str | None = None
    # 透传原生 ConfirmResult.rules（权限规则，如"记住放行"）；
    # 网关不解释其内容，原样下发框架。
    rules: list[dict] | None = None


class ConfirmationCreate(BaseModel):
    # 两种形状二选一：整批决定 decision(+edited_input)，或
    # decisions 逐调用表决（可与 decision 并存作未覆盖回落）。
    decision: str | None = None
    edited_input: str | None = None
    decisions: list[CallDecision] | None = None

    @model_validator(mode="after")
    def _validate(self):
        if self.decision is None and not self.decisions:
            raise ValueError("decision 与 decisions 至少要给一个")
        values = [self.decision] if self.decision else []
        values += [d.decision for d in self.decisions or []]
        for v in values:
            if v not in ("approved", "rejected", "edited"):
                raise ValueError("decision must be approved/rejected/edited")
        return self


def _err(status: int, code: str, message: str, trace_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_body(code, message, trace_id),
    )


@app.middleware("http")
async def trace_and_auth(request: Request, call_next):
    trace_id = new_trace_id()
    request.state.trace_id = trace_id
    if request.url.path == "/health":
        return await call_next(request)
    key = request.headers.get("x-project-key", "")
    tenant = KEY_TO_TENANT.get(key)
    if not tenant:
        return _err(401, "AUTH_FAILED", "missing or invalid X-Project-Key", trace_id)
    # 限流：经状态后端计数（内存后端＝本进程窗口；Redis 后端＝
    # 全局滑动窗口，多副本下不再是副本数倍口径）
    if not adapter.state.rate_allowed(tenant, RATE_LIMIT):
        return _err(429, "QUOTA_EXCEEDED", "rate limit exceeded", trace_id)
    end_user = request.headers.get("x-end-user-id", "")
    if end_user:
        if USER_RATE_LIMIT and not adapter.state.rate_allowed(
            f"{tenant}:{end_user}", USER_RATE_LIMIT,
        ):
            return _err(
                429, "QUOTA_EXCEEDED", "per-user rate limit exceeded",
                trace_id,
            )
        if REQUIRE_USER_PROOF:
            ts_raw = request.headers.get("x-end-user-ts", "")
            proof = request.headers.get("x-end-user-proof", "")
            try:
                ts = int(ts_raw)
            except ValueError:
                ts = 0
            if not ts or abs(time.time() - ts) > 300:
                return _err(
                    401, "USER_PROOF_EXPIRED",
                    "end-user assertion missing or expired", trace_id,
                )
            expected = hmac.new(
                key.encode(),
                f"{end_user}.{ts_raw}".encode(),
                hashlib.sha256,
            ).hexdigest()
            if not proof or not hmac.compare_digest(proof, expected):
                return _err(
                    401, "USER_PROOF_INVALID",
                    "end-user assertion invalid", trace_id,
                )
    request.state.tenant = tenant
    return await call_next(request)


def _tenant(request: Request) -> str:
    return request.state.tenant


async def _get_owned_session(
    request: Request, session_id: str, end_user: str,
):
    """会话归属校验。句柄不在网关内存时（进程重启后），adapter
    带 (tenant, end_user) 去服务侧探测重建：服务按
    user_id={tenant}:{end_user} 隔离，租户/用户对不上一律 404。"""
    session = await adapter.resolve_session(session_id, _tenant(request), end_user)
    if session is None or session.archived:
        return None, _err(
            404, "SESSION_NOT_FOUND", "session not found", request.state.trace_id,
        )
    if session.tenant != _tenant(request):
        return None, _err(
            403, "TENANT_FORBIDDEN", "session belongs to another tenant",
            request.state.trace_id,
        )
    return session, None


@app.get("/health")
async def health():
    return {"status": "ok", "model": adapter.model_name}


@app.get("/v1/agents")
async def list_agents(request: Request):
    tenant = _tenant(request)
    agents = [
        {
            "id": a["id"],
            "version": a["version"],
            "display_name": a["display_name"],
            "model": a["model"]["primary"],
        }
        for a in load_agents()
        if a["tenant"] == tenant
    ]
    return {"agents": agents, "total": len(agents)}


@app.post("/v1/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreate,
    x_end_user_id: str = Header(...),
):
    agent_config = load_agent(body.agent_id)
    if agent_config is None or agent_config["tenant"] != _tenant(request):
        return _err(
            404, "AGENT_NOT_FOUND", "agent not found for tenant",
            request.state.trace_id,
        )
    session = await adapter.create_session(_tenant(request), x_end_user_id, agent_config)
    return {
        "session_id": session.session_id,
        "agent_id": agent_config["id"],
        "tenant": session.tenant,
        "end_user": session.end_user,
    }


@app.post("/v1/sessions/{session_id}/messages")
async def post_message(
    request: Request,
    session_id: str,
    body: MessageCreate,
    mode: str = "sync",
    x_end_user_id: str = Header(...),
):
    session, err = await _get_owned_session(request, session_id, x_end_user_id)
    if err:
        return err
    if mode not in ("sync", "async"):
        return _err(
            400, "INTERNAL", "mode must be sync or async", request.state.trace_id,
        )
    try:
        run = await adapter.send_message(
            session,
            body.text,
            mode=mode,
            images=[img.to_source() for img in body.images or []],
        )
    except ModelCapabilityError as exc:
        return _err(400, "MODEL_NO_VISION", str(exc), request.state.trace_id)
    except SessionBusyError:
        return _err(
            409, "SESSION_BUSY", "session is busy; retry later",
            request.state.trace_id,
        )
    if mode == "async":
        return JSONResponse(status_code=202, content=run)
    return run


@app.get("/v1/sessions/{session_id}/events")
async def get_events(
    request: Request,
    session_id: str,
    stream: bool = False,
    last_event_id: str | None = None,
    x_end_user_id: str = Header(...),
):
    """事件查询；stream=true 时以 SSE 先回放已缓冲事件、再实时推送新事件。"""
    session, err = await _get_owned_session(request, session_id, x_end_user_id)
    if err:
        return err
    if stream:
        import json

        async def gen():
            # 回放＋实时跟随由状态后端提供（gateway/state.py）：内存
            # 后端走本进程订阅队列，Redis 后端走 Pub/Sub——任一网关
            # 副本都能完整订阅任一会话的事件（高可用）。
            async for e in adapter.state.follow_events(
                session_id, last_event_id,
            ):
                if e is None:
                    yield ": heartbeat\n\n"
                    continue
                yield (
                    f"id: {e['event_id']}\n"
                    f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                )

        return StreamingResponse(gen(), media_type="text/event-stream")
    events = adapter.state.list_events(session_id, last_event_id)
    return {"events": events, "total": len(events)}


@app.post("/v1/sessions/{session_id}/confirmations/{cid}")
async def post_confirmation(
    request: Request,
    session_id: str,
    cid: str,
    body: ConfirmationCreate,
    mode: str = "sync",
    x_end_user_id: str = Header(...),
):
    session, err = await _get_owned_session(request, session_id, x_end_user_id)
    if err:
        return err
    if mode not in ("sync", "async"):
        return _err(
            400, "INTERNAL", "mode must be sync or async", request.state.trace_id,
        )
    try:
        run = await adapter.confirm(
            session, cid, body.decision, body.edited_input, mode=mode,
            decisions=[
                d.model_dump() for d in body.decisions
            ] if body.decisions else None,
        )
    except SessionBusyError:
        return _err(
            409, "SESSION_BUSY", "session is busy; retry later",
            request.state.trace_id,
        )
    if run is None:
        return _err(
            404, "CONFIRMATION_EXPIRED", "confirmation not found or expired",
            request.state.trace_id,
        )
    if mode == "async":
        return JSONResponse(status_code=202, content=run)
    return run


@app.delete("/v1/sessions/{session_id}")
async def delete_session(
    request: Request,
    session_id: str,
    x_end_user_id: str = Header(...),
):
    session, err = await _get_owned_session(request, session_id, x_end_user_id)
    if err:
        return err
    await adapter.delete_session(session)
    return {"session_id": session_id, "archived": True}


@app.get("/v1/sessions")
async def list_sessions(
    request: Request,
    agent_id: str | None = None,
    x_end_user_id: str = Header(...),
):
    return await adapter.list_sessions(_tenant(request), x_end_user_id, agent_id)


@app.get("/v1/sessions/{session_id}/history")
async def get_session_history(
    request: Request,
    session_id: str,
    x_end_user_id: str = Header(...),
):
    session, err = await _get_owned_session(request, session_id, x_end_user_id)
    if err:
        return err
    return await adapter.session_history(session)


@app.get("/v1/runs/{run_id}")
async def get_run(request: Request, run_id: str):
    run = adapter.get_run(run_id)
    if run is None or run.get("tenant") != _tenant(request):
        return _err(404, "INTERNAL", "run not found", request.state.trace_id)
    return run


@app.get("/v1/usage")
async def get_usage(request: Request):
    return adapter.usage(_tenant(request))


@app.get("/v1/audit")
async def get_audit(request: Request, limit: int = 100):
    """审计账查询（租户内）：会话创建/归档、确认决定，只读。

    数据在网关台账库（audit_events），非运行日志——重启、
    状态后端丢失都不影响本账。
    """
    return {
        "events": adapter.ledger.list_audit(
            _tenant(request), limit=max(1, min(limit, 500)),
        ),
    }
