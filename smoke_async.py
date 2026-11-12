# -*- coding: utf-8 -*-
"""异步模式冒烟：验证契约双模式（sync/async）与 events 实时 SSE。

用法：  python smoke_async.py
行为：  与 smoke_test.py 相同的拉起方式（独立 demo key 与数据文件），验证：
        1) POST messages?mode=async 立即 202 返回 run 句柄（status=running）
        2) 在途 run 可经 GET /v1/runs/{id} 查到状态推进（running→awaiting_confirmation）
        3) events 含 confirmation.required（拿到确认号）
        4) events?stream=true 为实时流：订阅后提交 async 确认，
           流上实时收到续跑轮的 run.completed（不是回放）
        5) 续跑 run 终态 completed、笔记落盘
        6) 同步模式回归：默认 mode=sync 仍阻塞等结果
        7) mode 非法值 400
        全程输出写入 smoke_output_async.txt；退出码 0 = 全部通过。
"""
import json
import os
import secrets
import subprocess
import sys
import threading
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


def poll_run(gw: httpx.Client, headers: dict, run_id: str,
             want: set[str], timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = gw.get(f"/v1/runs/{run_id}", headers=headers)
        if r.status_code == 200:
            last = r.json()
            if last.get("status") in want:
                return last
        time.sleep(0.5)
    return last


def main() -> int:
    log("=" * 64)
    log("agent-base async-mode smoke test")
    log("=" * 64)

    for port in (8000, 8100, 8200):
        free_port(port)
    time.sleep(1)

    demo_key = secrets.token_urlsafe(32)
    notes_file = ROOT / "data" / "async-notes.txt"
    if notes_file.exists():
        notes_file.unlink()
    service_db = ROOT / "data" / "async-service.db"
    if service_db.exists():
        service_db.unlink()
    usage_db = ROOT / "data" / "async-usage.db"
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
        fh = open(log_dir / f"smoke-async-{name}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", module], cwd=ROOT, env=env,
            stdout=fh, stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    try:
        spawn("mcp", "mcp_demo.server")
        spawn("service", "service.run")
        spawn("gateway", "gateway.run")

        gw = httpx.Client(base_url=GATEWAY, timeout=60, trust_env=False)
        key_headers = {"X-Project-Key": demo_key, "X-End-User-Id": "demo-user"}

        check("gateway health", wait_health(f"{GATEWAY}/health", {}))
        check("service health", wait_health(
            f"{SERVICE}/health",
            {"X-Tenant-Id": "demo-dev", "X-End-User-Id": "smoke"},
        ))

        r = gw.post(
            "/v1/sessions", headers=key_headers,
            json={"agent_id": "demo.echo-assistant"},
        )
        check("POST /v1/sessions", r.status_code == 201, f"-> {r.status_code}")
        if r.status_code != 201:
            return finish(procs)
        session_id = r.json()["session_id"]

        # 1) async 发消息：立即 202 + run 句柄
        t0 = time.time()
        r = gw.post(
            f"/v1/sessions/{session_id}/messages?mode=async",
            headers=key_headers,
            json={"text": "帮我记一条笔记：异步冒烟笔记Q7"},
        )
        elapsed = time.time() - t0
        d = r.json() if r.status_code == 202 else {}
        run_id = d.get("run_id", "")
        check(
            "async 发消息立即 202 返回 running 句柄",
            r.status_code == 202 and d.get("status") == "running" and bool(run_id),
            f"-> {r.status_code} {d} ({elapsed:.1f}s)",
        )

        # 2) 在途 run 状态推进到 awaiting_confirmation
        run = poll_run(gw, key_headers, run_id, {"awaiting_confirmation"})
        check(
            "在途 run 可查且推进到 awaiting_confirmation",
            run.get("status") == "awaiting_confirmation",
            f"-> {run.get('status')}",
        )

        # 3) events 拿确认号
        r = gw.get(f"/v1/sessions/{session_id}/events", headers=key_headers)
        events = r.json().get("events", []) if r.status_code == 200 else []
        cids = [
            e["payload"]["confirmation_id"] for e in events
            if e["type"] == "confirmation.required"
        ]
        check("events 含 confirmation.required", len(cids) >= 1,
              f"-> types={[e['type'] for e in events]}")
        last_event_id = events[-1]["event_id"] if events else None

        # 4) 实时 SSE：先订阅（带 last_event_id 只收新事件），再 async 提交确认
        sse_events: list[dict] = []
        sse_stop = threading.Event()

        def sse_reader() -> None:
            try:
                with httpx.Client(timeout=httpx.Timeout(10, read=40),
                                  trust_env=False) as c:
                    with c.stream(
                        "GET",
                        f"{GATEWAY}/v1/sessions/{session_id}/events",
                        params={"stream": "true", "last_event_id": last_event_id},
                        headers=key_headers,
                    ) as resp:
                        for line in resp.iter_lines():
                            if sse_stop.is_set():
                                break
                            if line.startswith("data:"):
                                try:
                                    sse_events.append(json.loads(line[5:].strip()))
                                except ValueError:
                                    pass
            except httpx.HTTPError:
                pass

        reader = threading.Thread(target=sse_reader, daemon=True)
        reader.start()
        time.sleep(1.5)  # 等订阅建立

        r = gw.post(
            f"/v1/sessions/{session_id}/confirmations/{cids[-1]}?mode=async",
            headers=key_headers, json={"decision": "approved"},
        )
        d = r.json() if r.status_code == 202 else {}
        resume_run_id = d.get("run_id", "")
        check(
            "async 提交确认立即 202 返回续跑句柄",
            r.status_code == 202 and d.get("status") == "running"
            and bool(resume_run_id),
            f"-> {r.status_code} {d}",
        )

        deadline = time.time() + 30
        got_completed = False
        while time.time() < deadline:
            if any(
                e["type"] == "run.completed" and e["run_id"] == resume_run_id
                for e in sse_events
            ):
                got_completed = True
                break
            time.sleep(0.5)
        sse_stop.set()
        check(
            "SSE 实时收到续跑轮 run.completed（订阅后新事件）",
            got_completed,
            f"-> sse types={[(e['type'], e['run_id'][:12]) for e in sse_events]}",
        )

        # 5) 续跑终态 + 笔记落盘
        run = poll_run(gw, key_headers, resume_run_id, {"completed", "failed"})
        note_written = (
            notes_file.exists()
            and "异步冒烟笔记Q7" in notes_file.read_text(encoding="utf-8")
        )
        check(
            "续跑 run completed 且笔记落盘",
            run.get("status") == "completed" and note_written,
            f"-> {run.get('status')} notes={note_written}",
        )

        # 6) 同步回归
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "你好"},
        )
        d = r.json() if r.status_code == 200 else {}
        check(
            "同步模式（默认）仍阻塞返回 completed 回复",
            r.status_code == 200 and d.get("status") == "completed"
            and "[mock]" in str(d.get("reply", "")),
            f"-> {d.get('status')} {str(d.get('reply'))[:60]}",
        )

        # 7) mode 非法值
        r = gw.post(
            f"/v1/sessions/{session_id}/messages?mode=bogus",
            headers=key_headers, json={"text": "你好"},
        )
        check("mode 非法值返回 400", r.status_code == 400,
              f"-> {r.status_code}")

    finally:
        pass
    return finish(procs)


def finish(procs: list[subprocess.Popen]) -> int:
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    log("")
    if FAILURES:
        log(f"RESULT: FAIL ({len(FAILURES)}): {FAILURES}")
        code = 1
    else:
        log("RESULT: ALL PASS")
        code = 0
    (ROOT / "smoke_output_async.txt").write_text(
        "\n".join(LINES) + "\n", encoding="utf-8",
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
