# -*- coding: utf-8 -*-
"""多模型策略冒烟：验证逐 Agent 模型计划＋fallback 下发＋PATCH 切换。

用法：  python smoke_models.py
行为：  与其他冒烟相同的拉起方式（独立 demo key 与数据文件），验证：
        1) 建会话后服务侧会话记录：主模型=mock-1、原生 fallback
           =mock-backup-1（注册表 fallback 真下发，不再是摆设）
        2) 纯文本轮次不切换（服务侧仍 mock-1）
        3) 带图轮次完成且事件含 model.switched（to_entry=mock-vision），
           服务侧会话模型已换成 mock-vision-1（PATCH 真生效）
        4) 其后纯文本轮次不降回（服务侧仍 mock-vision-1，能力只升不降）
        5) 无 vision 声明的 Agent 收到图片 → 400 MODEL_NO_VISION
        6) 会话类接口缺 X-End-User-Id → 422（生产口径：必填，
           不再静默兜底 demo-user）
        全程输出写入 smoke_output_models.txt；退出码 0 = 全部通过。
"""
import json
import os
import secrets
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

# 1x1 透明 PNG（仅用于走通图片输入管道，mock 模型只读文本）
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


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
        pass


def service_session_config(svc: httpx.Client, headers: dict,
                           agent_name: str, session_id: str) -> dict:
    """白盒核对：经服务原生 API 读会话记录的 config（事实来源）。"""
    r = svc.get("/agent/", headers=headers)
    r.raise_for_status()
    service_agent_id = None
    for agent in r.json().get("agents", []):
        data = agent.get("data") or {}
        if data.get("name") == agent_name:
            service_agent_id = agent.get("id") or data.get("id")
            break
    if not service_agent_id:
        return {}
    r = svc.get(
        "/sessions/", headers=headers,
        params={"agent_id": service_agent_id},
    )
    r.raise_for_status()
    for view in r.json().get("sessions", []):
        record = view.get("session") or view.get("record") or view
        if record.get("id") == session_id:
            return record.get("config") or {}
    return {}


def main() -> int:
    log("=" * 64)
    log("agent-base model-strategy smoke test")
    log("=" * 64)

    for port in (8000, 8100, 8200):
        free_port(port)
    time.sleep(1)

    demo_key = secrets.token_urlsafe(32)
    notes_file = ROOT / "data" / "models-notes.txt"
    service_db = ROOT / "data" / "models-service.db"
    usage_db = ROOT / "data" / "models-usage.db"
    for f in (notes_file, service_db, usage_db):
        if f.exists():
            f.unlink()

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
        fh = open(log_dir / f"smoke-models-{name}.log", "w", encoding="utf-8")
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
        svc = httpx.Client(base_url=SERVICE, timeout=60, trust_env=False)
        key_headers = {
            "X-Project-Key": demo_key, "X-End-User-Id": "demo-user",
        }
        svc_headers = {
            "X-Tenant-Id": "demo-dev", "X-End-User-Id": "demo-user",
        }

        check("gateway health", wait_health(f"{GATEWAY}/health", {}))
        check("service health", wait_health(
            f"{SERVICE}/health",
            {"X-Tenant-Id": "demo-dev", "X-End-User-Id": "smoke"},
        ))

        r = gw.post(
            "/v1/sessions", headers=key_headers,
            json={"agent_id": "demo.vision-assistant"},
        )
        check("POST /v1/sessions (vision-assistant)",
              r.status_code == 201, f"-> {r.status_code}")
        if r.status_code != 201:
            return finish(procs)
        session_id = r.json()["session_id"]

        # 1) 主模型 + fallback 真下发
        config = service_session_config(
            svc, svc_headers, "demo.vision-assistant", session_id,
        )
        main_model = (config.get("chat_model_config") or {}).get("model")
        fb_model = (config.get("fallback_chat_model_config") or {}).get("model")
        check("主模型按注册表组装", main_model == "mock-1",
              f"-> {main_model}")
        check("fallback 原生下发", fb_model == "mock-backup-1",
              f"-> {fb_model}")

        # 2) 纯文本轮次不切换
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "你好"},
        )
        d = r.json() if r.status_code == 200 else {}
        check("纯文本轮次 completed",
              r.status_code == 200 and d.get("status") == "completed",
              f"-> {r.status_code} {d.get('status')}")
        config = service_session_config(
            svc, svc_headers, "demo.vision-assistant", session_id,
        )
        check("纯文本轮次未切换",
              (config.get("chat_model_config") or {}).get("model") == "mock-1")

        # 3) 带图轮次：切换发生且服务侧真生效
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={
                "text": "看看这张图",
                "images": [{
                    "data_base64": TINY_PNG_B64, "media_type": "image/png",
                }],
            },
        )
        d = r.json() if r.status_code == 200 else {}
        check("带图轮次 completed",
              r.status_code == 200 and d.get("status") == "completed",
              f"-> {r.status_code} {d.get('status')} {str(d)[:120]}")
        r = gw.get(
            f"/v1/sessions/{session_id}/events", headers=key_headers,
        )
        events = r.json().get("events", []) if r.status_code == 200 else []
        switched = [
            e for e in events if e["type"] == "model.switched"
        ]
        check(
            "事件含 model.switched -> mock-vision",
            len(switched) >= 1
            and switched[-1]["payload"].get("to_entry") == "mock-vision",
            f"-> types={[e['type'] for e in events]}",
        )
        config = service_session_config(
            svc, svc_headers, "demo.vision-assistant", session_id,
        )
        check("服务侧会话模型已切换",
              (config.get("chat_model_config") or {}).get("model")
              == "mock-vision-1")

        # 4) 其后纯文本不降回
        r = gw.post(
            f"/v1/sessions/{session_id}/messages", headers=key_headers,
            json={"text": "再说一句"},
        )
        d = r.json() if r.status_code == 200 else {}
        check("切换后纯文本轮次 completed",
              r.status_code == 200 and d.get("status") == "completed")
        config = service_session_config(
            svc, svc_headers, "demo.vision-assistant", session_id,
        )
        check("能力只升不降（仍 mock-vision-1）",
              (config.get("chat_model_config") or {}).get("model")
              == "mock-vision-1")

        # 5) 无 vision 声明的 Agent 收图 → 400 MODEL_NO_VISION
        r = gw.post(
            "/v1/sessions", headers=key_headers,
            json={"agent_id": "demo.echo-assistant"},
        )
        check("POST /v1/sessions (echo-assistant)",
              r.status_code == 201, f"-> {r.status_code}")
        echo_session = r.json()["session_id"]
        r = gw.post(
            f"/v1/sessions/{echo_session}/messages", headers=key_headers,
            json={
                "text": "看看这张图",
                "images": [{
                    "data_base64": TINY_PNG_B64, "media_type": "image/png",
                }],
            },
        )
        code = ""
        try:
            code = r.json().get("code", "")
        except ValueError:
            pass
        check("无 vision 模型收图 -> 400 MODEL_NO_VISION",
              r.status_code == 400 and code == "MODEL_NO_VISION",
              f"-> {r.status_code} {code}")

        # 6) 缺 X-End-User-Id -> 422（必填，不再兜底）
        r = gw.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"X-Project-Key": demo_key},
            json={"text": "你好"},
        )
        check("缺 X-End-User-Id -> 422", r.status_code == 422,
              f"-> {r.status_code}")

        return finish(procs)
    finally:
        if procs and any(p.poll() is None for p in procs):
            pass  # finish 已收口；异常路径由 finally 兜底杀进程
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()


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
        log(f"RESULT: FAILED ({len(FAILURES)}): {FAILURES}")
    else:
        log("RESULT: ALL PASS")
    (ROOT / "smoke_output_models.txt").write_text(
        "\n".join(LINES) + "\n", encoding="utf-8",
    )
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
