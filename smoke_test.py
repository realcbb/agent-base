# -*- coding: utf-8 -*-
"""一键冒烟：一条命令验证基座 v1 骨架全链。

用法：  python smoke_test.py
行为：  停掉占用 8000/8100/8200 的旧进程 → 用独立 demo key 与独立数据目录
        拉起 mcp_demo / service / gateway 三个子进程 → 依次验证：
        1) 无 key 请求被拒（AUTH_FAILED）
        2) demo key 列 Agent
        3) 建会话
        4) 普通消息收到 mock 回复
        5) 时间问题触发 ALLOW 工具 demo_time 并拿到结果
        6) 记笔记触发 ASK 确认 → 批准 → 工具落盘
        7) events 回放含 confirmation.required
        8) usage 有计量
        9) usage 落库：直查 SQLite 逐调用流水 + 重启 gateway 后数值不变
           （重启只动 gateway，service 保持运行）
        10) 原生 service 全链（可信头身份 + mock 回复）
        11) 会话落服务存储：直查服务 API 能看到会话、消息历史与 parked 状态
        12) 重启 gateway 后 ASK 确认仍走 HTTP 链路：批准重启前 park 的确认，
            网关从服务状态重建 pending，工具恢复执行并落盘
        13) 重启 gateway 后用旧 session id 续聊：回复完成，且新一轮追加进
            服务侧同一份历史（旧消息仍在）
        全程输出写入 smoke_output.txt；退出码 0 = 全部通过。
"""
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
GATEWAY = "http://127.0.0.1:8000"
SERVICE = "http://127.0.0.1:8100"
LINES: list[str] = []
FAILURES: list[str] = []


def log(msg: str = "") -> None:
    print(msg, flush=True)
    LINES.append(msg)


def check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    log(f"[{mark}] {name} {detail}".rstrip())
    if not cond:
        FAILURES.append(name)
    return cond


def wait_health(url: str, headers: dict, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    client = httpx.Client(timeout=5, trust_env=False)
    while time.time() < deadline:
        try:
            r = client.get(url, headers=headers)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def free_port(port: int) -> None:
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except FileNotFoundError:
        pass  # 无 psmisc 的干净机器上端口本就无人占，跳过即可


def main() -> int:
    log("=" * 64)
    log("agent-base v1 smoke test")
    log("=" * 64)

    for port in (8000, 8100, 8200):
        free_port(port)
    time.sleep(1)

    demo_key = secrets.token_urlsafe(32)
    notes_file = ROOT / "data" / "smoke-notes.txt"
    if notes_file.exists():
        notes_file.unlink()
    service_db = ROOT / "data" / "smoke-service.db"
    if service_db.exists():
        service_db.unlink()
    usage_db = ROOT / "data" / "smoke-usage.db"
    if usage_db.exists():
        usage_db.unlink()

    env = dict(os.environ)
    env.update({
        "PROJECT_KEYS": f"demo-dev={demo_key}",
        "OPENAI_BASE_URL": "",
        "OPENAI_API_KEY": "",
        "OPENAI_MODEL": "",
        "MCP_DEMO_NOTES_FILE": str(notes_file),
        "SERVICE_DB_URL": f"sqlite+aiosqlite:///{service_db}",
        "SERVICE_STORAGE": "sql",
        "AGENT_BASE_USAGE_DB": str(usage_db),
        "PYTHONUNBUFFERED": "1",
    })

    procs: list[subprocess.Popen] = []
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    def spawn(name: str, module: str) -> subprocess.Popen:
        fh = open(log_dir / f"smoke-{name}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", module], cwd=ROOT, env=env,
            stdout=fh, stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    try:
        spawn("mcp", "mcp_demo.server")
        spawn("service", "service.run")
        gateway_proc = spawn("gateway", "gateway.run")

        gw = httpx.Client(base_url=GATEWAY, timeout=60, trust_env=False)
        key_headers = {"X-Project-Key": demo_key, "X-End-User-Id": "demo-user"}

        ok = wait_health(f"{GATEWAY}/health", {})
        check("gateway health", ok)
        ok = wait_health(
            f"{SERVICE}/health",
            {"X-Tenant-Id": "demo-dev", "X-End-User-Id": "smoke"},
        )
        check("service health（可信头身份）", ok)

        # 1) 无 key 被拒
        r = gw.get("/v1/agents")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check(
            "无 key 请求被拒 AUTH_FAILED",
            r.status_code == 401 and body.get("code") == "AUTH_FAILED",
            f"-> {r.status_code} {body.get('code')}",
        )

        # 2) 列 Agent
        r = gw.get("/v1/agents", headers=key_headers)
        agents = r.json().get("agents", []) if r.status_code == 200 else []
        check(
            "GET /v1/agents",
            r.status_code == 200 and any(a["id"] == "demo.echo-assistant" for a in agents),
            f"-> {[a['id'] for a in agents]}",
        )

        # 3) 建会话
        r = gw.post(
            "/v1/sessions", headers=key_headers,
            json={"agent_id": "demo.echo-assistant"},
        )
        check("POST /v1/sessions", r.status_code == 201, f"-> {r.status_code}")
        if r.status_code != 201:
            return finish(procs)
        session_id = r.json()["session_id"]
        log(f"     session_id={session_id}")
        check(
            "session_id 为服务侧会话 id 透传（32 位 hex，非 ses- 前缀）",
            len(session_id) == 32
            and all(c in "0123456789abcdef" for c in session_id),
            f"-> {session_id}",
        )

        # 4) 普通消息
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "你好"},
        )
        d = r.json() if r.status_code == 200 else {}
        check(
            "普通消息 mock 回复",
            r.status_code == 200 and d.get("status") == "completed"
            and "[mock]" in str(d.get("reply", "")),
            f"-> {d.get('reply')}",
        )

        # 5) ALLOW 工具（原 L0）
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "现在几点？请查一下时间"},
        )
        d = r.json() if r.status_code == 200 else {}
        check(
            "ALLOW 工具 demo_time 直接执行",
            r.status_code == 200 and d.get("status") == "completed"
            and "demo_time" in str(d.get("reply", "")),
            f"-> {str(d.get('reply'))[:100]}",
        )

        # 6) ASK 确认流
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "帮我记一条笔记：冒烟笔记XYZ"},
        )
        d = r.json() if r.status_code == 200 else {}
        check(
            "ASK 工具 demo_note_write 触发确认",
            r.status_code == 200 and d.get("status") == "awaiting_confirmation",
            f"-> status={d.get('status')}",
        )

        r = gw.get(f"/v1/sessions/{session_id}/events", headers=key_headers)
        events = r.json().get("events", []) if r.status_code == 200 else []
        cids = [
            e["payload"]["confirmation_id"] for e in events
            if e["type"] == "confirmation.required"
        ]
        check(
            "events 回放含 confirmation.required",
            len(cids) >= 1,
            f"-> types={[e['type'] for e in events]}",
        )
        if cids:
            r = gw.post(
                f"/v1/sessions/{session_id}/confirmations/{cids[-1]}",
                headers=key_headers, json={"decision": "approved"},
            )
            d = r.json() if r.status_code == 200 else {}
            note_written = (
                notes_file.exists() and "冒烟笔记XYZ" in notes_file.read_text(encoding="utf-8")
            )
            check(
                "批准确认后工具执行并落盘",
                r.status_code == 200 and d.get("status") == "completed" and note_written,
                f"-> reply={str(d.get('reply'))[:80]} notes_file={note_written}",
            )

        # 6.5) 再 park 一个确认，留到 gateway 重启之后处理（断言 12 前置）
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "帮我记一条笔记：重启前待确认"},
        )
        d = r.json() if r.status_code == 200 else {}
        check(
            "重启前再 park 一个 ASK 确认",
            r.status_code == 200 and d.get("status") == "awaiting_confirmation",
            f"-> status={d.get('status')}",
        )
        r = gw.get(f"/v1/sessions/{session_id}/events", headers=key_headers)
        events = r.json().get("events", []) if r.status_code == 200 else []
        cids = [
            e["payload"]["confirmation_id"] for e in events
            if e["type"] == "confirmation.required"
        ]
        restart_cid = cids[-1] if len(cids) >= 2 else None
        log(f"     restart_cid={restart_cid}")

        # 7) usage
        r = gw.get("/v1/usage", headers=key_headers)
        d = r.json() if r.status_code == 200 else {}
        usage_before = dict(d)
        check(
            "GET /v1/usage 有计量",
            r.status_code == 200 and d.get("runs", 0) >= 3
            and d.get("input_tokens", 0) > 0,
            f"-> {d}",
        )

        # 7.5) usage 落库持久化：经存储层直查流水（逐调用行、字段完整、
        # 与 API 对齐；存储实现由 URL 决定，生产为 MySQL）
        sys.path.insert(0, str(ROOT))
        from gateway.usage_store import UsageStore

        t = UsageStore(db_path=usage_db).totals("demo-dev")
        row_count = t.get("rows", 0)
        db_in = t.get("input_tokens", 0)
        db_out = t.get("output_tokens", 0)
        db_total = t.get("total_tokens", 0)
        sample = t.get("sample") or {}
        fields_ok = (
            bool(sample)
            and all(
                str(sample.get(k, "")) != ""
                for k in ("end_user", "agent_id", "session_id", "run_id", "model")
            )
            and sample.get("total_tokens")
            == sample.get("input_tokens", 0) + sample.get("output_tokens", 0)
            and sample.get("token_source") == "model_reported"
        )
        check(
            "usage 流水已落库（逐调用行、字段完整）",
            row_count >= 4
            and db_in == usage_before.get("input_tokens")
            and db_out == usage_before.get("output_tokens")
            and db_total == db_in + db_out and fields_ok,
            f"-> rows={row_count} db_in={db_in} db_out={db_out} sample={sample}",
        )

        # 重启 gateway 进程后再查 /v1/usage，数值须与重启前完全一致
        gateway_proc.terminate()
        try:
            gateway_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gateway_proc.kill()
        free_port(8000)
        time.sleep(1)
        gateway_proc = spawn("gateway", "gateway.run")
        ok = wait_health(f"{GATEWAY}/health", {})
        r = gw.get("/v1/usage", headers=key_headers)
        d2 = r.json() if r.status_code == 200 else {}
        check(
            "重启 gateway 后 /v1/usage 数值不变（落库生效）",
            ok and r.status_code == 200
            and d2.get("runs") == usage_before.get("runs")
            and d2.get("input_tokens") == usage_before.get("input_tokens")
            and d2.get("output_tokens") == usage_before.get("output_tokens")
            and d2.get("by_agent") == usage_before.get("by_agent"),
            f"-> before={usage_before} after={d2}",
        )

        # 11) 会话落服务存储：绕过网关、直查服务 API
        svc = httpx.Client(base_url=SERVICE, timeout=30, trust_env=False)
        svc_headers = {"X-Tenant-Id": "demo-dev", "X-End-User-Id": "demo-user"}
        svc_agent_id = None
        try:
            r = svc.get("/agent/", headers=svc_headers)
            for a in r.json().get("agents", []):
                if (a.get("data") or {}).get("name") == "demo.echo-assistant":
                    svc_agent_id = a.get("id")
                    break
        except Exception as exc:
            log(f"     service agent list error: {exc}")

        def svc_messages() -> list:
            if not svc_agent_id:
                return []
            r = svc.get(
                f"/sessions/{session_id}/messages", headers=svc_headers,
                params={"agent_id": svc_agent_id},
            )
            return r.json().get("messages", []) if r.status_code == 200 else []

        msgs = svc_messages()
        msgs_text = json.dumps(msgs, ensure_ascii=False)
        check(
            "会话与消息历史在服务存储（直查服务 API 可见）",
            bool(svc_agent_id) and len(msgs) >= 6
            and "你好" in msgs_text and "冒烟笔记XYZ" in msgs_text,
            f"-> service_agent={svc_agent_id} messages={len(msgs)}",
        )
        svc_status = None
        if svc_agent_id:
            r = svc.get(
                f"/sessions/{session_id}/status", headers=svc_headers,
                params={"agent_id": svc_agent_id},
            )
            svc_status = r.json().get("status") if r.status_code == 200 else None
        check(
            "服务侧会话状态为 awaiting_permission（parked 归服务管）",
            svc_status == "awaiting_permission",
            f"-> status={svc_status}",
        )
        msgs_before_continue = len(msgs)

        # 12) 重启后批准重启前 park 的确认：网关内存里已无该 pending，
        # 须从服务会话状态重建后走 HTTP 链路提交、工具恢复执行落盘
        if restart_cid:
            r = gw.post(
                f"/v1/sessions/{session_id}/confirmations/{restart_cid}",
                headers=key_headers, json={"decision": "approved"},
            )
            d = r.json() if r.status_code == 200 else {}
            note_ok = (
                notes_file.exists()
                and "重启前待确认" in notes_file.read_text(encoding="utf-8")
            )
            check(
                "gateway 重启后 ASK 确认走 HTTP 链路恢复并落盘",
                r.status_code == 200 and d.get("status") == "completed" and note_ok,
                f"-> status={d.get('status')} notes={note_ok}",
            )
        else:
            check("gateway 重启后 ASK 确认走 HTTP 链路恢复并落盘", False, "-> 无 restart_cid")

        # 13) 重启后用旧 session id 续聊：回复完成，且新一轮追加进
        # 服务侧同一份历史、旧消息仍在（历史归服务存储的直接证据）
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "重启后继续：基座还在吗"},
        )
        d = r.json() if r.status_code == 200 else {}
        msgs_after = svc_messages()
        msgs_after_text = json.dumps(msgs_after, ensure_ascii=False)
        tail_text = json.dumps(msgs_after[-3:], ensure_ascii=False) if msgs_after else ""
        check(
            "重启 gateway 后旧会话续聊且历史完整",
            r.status_code == 200 and d.get("status") == "completed"
            and len(msgs_after) > msgs_before_continue
            and "重启后继续" in tail_text and "你好" in msgs_after_text,
            f"-> reply={str(d.get('reply'))[:60]}"
            f" messages {msgs_before_continue}->{len(msgs_after)}",
        )

        # 8) 原生 service 全链
        from service.native_check import run_native_check

        ok, native_lines = run_native_check(SERVICE)
        for line in native_lines:
            log(f"     native: {line}")
        check("原生 service 全链（凭证→Agent→会话→对话→mock 回复）", ok)

        return finish(procs)
    finally:
        pass


def finish(procs: list[subprocess.Popen]) -> int:
    log("-" * 64)
    if FAILURES:
        log(f"RESULT: FAIL（{len(FAILURES)} 项未过：{', '.join(FAILURES)}）")
        code = 1
    else:
        log("RESULT: ALL PASS")
        code = 0
    (ROOT / "smoke_output.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
    for p in procs:
        p.terminate()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
