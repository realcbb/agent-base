# -*- coding: utf-8 -*-
"""装配探针（动作清单第 1 项证据）：进程内用生产装配起一台服务，查——

1. app.state.enable_channel_worker / enable_scheduler 是否为 False；
2. lifespan 后 scheduler_manager 是否处于 disabled、channel_dispatcher
   是否为 None（worker 未起）；
3. 一轮真实对话里模型实际收到的工具 schema 清单：Team*/AgentInvite/
   Schedule*/MCP 工具各自在不在（monkeypatch 本进程的 MockChatModel
   记录工具名——只观测、不改行为，记录后原样走原实现）。

用法（环境由 smoke_actions.py 备好：独立 SERVICE_DB_URL /
SERVICE_WORKSPACE_DIR / MCP_URL 指向已运行的 mcp_demo）：
    PROBE_PORT=8101 PROBE_OUT=<json 路径> python -m probe.probe_assembly
结果以 JSON 写 PROBE_OUT 并打印到 stdout；退出码 0 = 对话链跑通。
"""
import asyncio
import json
import os
import threading
import time

import httpx

RESULT: dict = {"checks": {}, "tools_seen": []}


def main() -> int:
    port = int(os.environ.get("PROBE_PORT", "8101"))
    out_path = os.environ.get("PROBE_OUT", "")

    # --- 观测钩子：记录模型每轮收到的工具名（原样放行原实现） ---
    from service import mock_model

    recorded: list[list[str]] = []
    orig_call_api = mock_model.MockChatModel._call_api

    async def recording_call_api(self, model_name, messages, tools=None, **kw):
        names = []
        for tool in tools or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            name = (fn or tool).get("name") if isinstance(fn or tool, dict) else None
            if name:
                names.append(name)
        recorded.append(names)
        return await orig_call_api(
            self, model_name, messages, tools=tools, **kw,
        )

    mock_model.MockChatModel._call_api = recording_call_api

    # --- 生产装配本体（service.app 的模块级 app 就是 build_service_app()） ---
    from service.app import app

    RESULT["checks"]["state_enable_channel_worker"] = app.state.enable_channel_worker
    RESULT["checks"]["state_enable_scheduler"] = app.state.enable_scheduler

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=asyncio.run, args=(server.serve(),), daemon=True,
    )
    thread.start()

    base = f"http://127.0.0.1:{port}"
    headers = {"X-Tenant-Id": "probe-t", "X-End-User-Id": "probe-u"}
    client = httpx.Client(base_url=base, headers=headers, timeout=30, trust_env=False)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            RESULT["checks"]["health"] = "timeout"
            return _emit(out_path, 1)

        # lifespan 后的运行时状态（worker/manager 是否真未启用）
        scheduler = getattr(app.state, "scheduler_manager", None)
        RESULT["checks"]["scheduler_manager_enabled"] = (
            getattr(scheduler, "_enabled", None) if scheduler else None
        )
        RESULT["checks"]["channel_dispatcher_is_none"] = (
            getattr(app.state, "channel_dispatcher", "missing") is None
        )

        r = client.post(
            "/credential/",
            json={"data": {"type": "mock_credential", "name": "mock"}},
        )
        credential_id = r.json()["credential_id"]
        r = client.post(
            "/agent/",
            json={"name": "probe-agent", "system_prompt": "你是探针助手。"},
        )
        agent_id = r.json()["agent_id"]
        r = client.post(
            "/sessions/",
            json={
                "agent_id": agent_id,
                "chat_model_config": {
                    "type": "mock_credential",
                    "credential_id": credential_id,
                    "model": "mock-1",
                    "parameters": {},
                },
            },
        )
        session_id = r.json()["session_id"]
        msg = {
            "name": "probe-u",
            "role": "user",
            "content": [{"type": "text", "text": "你好，探针"}],
        }
        r = client.post(
            "/chat/",
            json={"agent_id": agent_id, "session_id": session_id, "input": msg},
        )
        RESULT["checks"]["chat_status"] = r.status_code

        reply_ok = False
        for _ in range(30):
            time.sleep(1)
            r = client.get(
                f"/sessions/{session_id}/messages",
                params={"agent_id": agent_id},
            )
            for m in r.json().get("messages", []):
                for b in m.get("content", []):
                    if isinstance(b, dict) and "[mock]" in str(b.get("text", "")):
                        reply_ok = True
        RESULT["checks"]["chat_reply_ok"] = reply_ok

        tools_seen = sorted({n for call in recorded for n in call})
        RESULT["tools_seen"] = tools_seen
        RESULT["checks"]["tool_demo_time_present"] = any(
            n.endswith("__demo_time") for n in tools_seen
        )
        RESULT["checks"]["tool_team_present"] = [
            n for n in tools_seen if n.startswith(("Team", "AgentCreate"))
        ]
        RESULT["checks"]["tool_agent_invite_present"] = "AgentInvite" in tools_seen
        RESULT["checks"]["tool_schedule_present"] = [
            n for n in tools_seen if n.startswith("Schedule")
        ]
        return _emit(out_path, 0 if reply_ok else 1)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _emit(out_path: str, code: int) -> int:
    text = json.dumps(RESULT, ensure_ascii=False, indent=2)
    print(text, flush=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
