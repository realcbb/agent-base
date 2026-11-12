# -*- coding: utf-8 -*-
"""原生 AgentScope Service 全链检查（不经网关）。

验证：可信头身份注入 → 建凭证(mock) → 建 Agent → 建会话 → 触发对话 →
轮询消息拿到 mock 回复。供 smoke_test.py 调用，也可独立运行：
    python -m service.native_check   （需 service 已启动）
"""
import time

import httpx


def run_native_check(base_url: str) -> tuple[bool, list[str]]:
    lines: list[str] = []
    headers = {"X-Tenant-Id": "demo-dev", "X-End-User-Id": "demo-user"}
    client = httpx.Client(
        base_url=base_url, headers=headers, timeout=30, trust_env=False,
    )
    try:
        r = client.get("/health")
        lines.append(f"health: {r.status_code} {r.json().get('status')}")
        if r.status_code != 200:
            return False, lines

        r = client.post(
            "/credential/",
            json={"data": {"type": "mock_credential", "name": "mock"}},
        )
        lines.append(f"create credential: {r.status_code}")
        if r.status_code != 201:
            lines.append(r.text[:300])
            return False, lines
        credential_id = r.json()["credential_id"]

        r = client.post(
            "/agent/",
            json={"name": "native-demo", "system_prompt": "你是演示助手。"},
        )
        lines.append(f"create agent: {r.status_code}")
        if r.status_code != 201:
            lines.append(r.text[:300])
            return False, lines
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
        lines.append(f"create session: {r.status_code}")
        if r.status_code != 201:
            lines.append(r.text[:300])
            return False, lines
        session_id = r.json()["session_id"]

        msg = {
            "name": "demo-user",
            "role": "user",
            "content": [{"type": "text", "text": "你好，原生服务"}],
        }
        r = client.post(
            "/chat/",
            json={"agent_id": agent_id, "session_id": session_id, "input": msg},
        )
        lines.append(f"chat trigger: {r.status_code}")
        if r.status_code != 200:
            lines.append(r.text[:300])
            return False, lines

        for _ in range(30):
            time.sleep(1)
            r = client.get(
                f"/sessions/{session_id}/messages",
                params={"agent_id": agent_id},
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for m in data.get("messages", []):
                for b in m.get("content", []):
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "text"
                        and "[mock]" in str(b.get("text", ""))
                    ):
                        lines.append(f"reply: {b['text']}")
                        return True, lines
        lines.append("reply: TIMEOUT，未在 30s 内拿到 mock 回复")
        return False, lines
    except Exception as e:  # noqa: BLE001 - 检查脚本如实记录异常
        lines.append(f"exception: {type(e).__name__}: {e}")
        return False, lines


if __name__ == "__main__":
    import os

    ok, out = run_native_check(os.environ.get("SERVICE_URL", "http://127.0.0.1:8100"))
    for line in out:
        print(line)
    raise SystemExit(0 if ok else 1)
