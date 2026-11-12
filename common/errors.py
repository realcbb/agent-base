# -*- coding: utf-8 -*-
"""设计文档 7.4 的统一错误信封与错误码。"""
import uuid

ERROR_CODES = {
    "AUTH_FAILED",
    "USER_PROOF_INVALID",
    "USER_PROOF_EXPIRED",
    "TENANT_FORBIDDEN",
    "QUOTA_EXCEEDED",
    "AGENT_NOT_FOUND",
    "SESSION_NOT_FOUND",
    "SESSION_BUSY",
    "MODEL_UNAVAILABLE",
    "MODEL_NO_VISION",
    "TOOL_DENIED",
    "TOOL_TIMEOUT",
    "CONFIRMATION_EXPIRED",
    "RUN_LIMIT_EXCEEDED",
    "INTERNAL",
}


def new_trace_id() -> str:
    return uuid.uuid4().hex


def error_body(code: str, message: str, trace_id: str) -> dict:
    assert code in ERROR_CODES, f"unknown error code {code}"
    return {"code": code, "message": message, "trace_id": trace_id}
