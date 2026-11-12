# -*- coding: utf-8 -*-
"""动作清单分阶段冒烟：验证《个人平台假设排查清单》动作清单的落地。

用法：  python smoke_actions.py
行为：  用独立 key/数据目录拉起 mcp_demo / service / gateway 三进程，
        按《清单》动作顺序逐项验证（与 smoke_test.py 的基线冒烟互补，
        基线断言在那边、本文件不重复）：
        阶段 0) 注册表三级化：validate 通过 + DENY 工具被校验拒绝（第 7 项）
        阶段 1) 装配三改：探针进程内起生产装配，channel worker /
                scheduler 未启用（app.state + lifespan 后运行时状态），
                并记录一轮对话里模型实际收到的工具清单（第 1 项）
        阶段 2) 项目级共享策略：同租户 A 的凭证 B 可见、异租户 C 不
                可见（直查服务 API）；KB 同路尝试（第 2 项）
        阶段 3) 组织级凭证：网关会话跑通且凭证在系统户名下、终端
                用户键下无个人凭证；异租户引用系统户凭证跑不出回复
                （第 3 项）
        阶段 4) 按租户动态注入：tenant-a 新会话工作区有专属 MCP 与
                默认技能、无 optional 技能；tenant-b 会话都没有
                （第 4 项）
        阶段 5) 工作区产物回收：API 列目录/下文件回收到
                data/artifacts/{tenant}/{session}/，内容对得上（第 5 项）
        阶段 6) SOP 分发：注册表 SOP 已以系统户落库；终端用户不可见
                （框架无 SOP 共享通道，缺口如实记录）（第 6 项）
        全程输出写入 smoke_output_actions.txt；退出码 0 = 全部通过。
"""
import asyncio
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


def log(msg: str = "") -> None:
    print(msg, flush=True)
    LINES.append(msg)


def check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    log(f"[{mark}] {name} {detail}".rstrip())
    if not cond:
        FAILURES.append(name)
    return cond


def info(msg: str) -> None:
    log(f"     .. {msg}")


def wait_health(url: str, headers: dict, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    client = httpx.Client(timeout=5, trust_env=False)
    while time.time() < deadline:
        try:
            if client.get(url, headers=headers).status_code == 200:
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


def svc_headers(tenant: str, end_user: str) -> dict:
    return {"X-Tenant-Id": tenant, "X-End-User-Id": end_user}


def main() -> int:
    log("=" * 64)
    log("agent-base actions smoke test（动作清单落地验证）")
    log("=" * 64)

    for port in (8000, 8100, 8101, 8200):
        free_port(port)
    time.sleep(1)

    keys = {
        "demo-dev": secrets.token_urlsafe(24),
        "tenant-a": secrets.token_urlsafe(24),
        "tenant-b": secrets.token_urlsafe(24),
    }
    for name in ("actions-notes.txt",):
        p = ROOT / "data" / name
        if p.exists():
            p.unlink()
    for name in ("actions-service.db", "actions-usage.db"):
        p = ROOT / "data" / name
        if p.exists():
            p.unlink()
    import shutil

    for d in ("actions-workspaces", "artifacts"):
        p = ROOT / "data" / d
        if p.exists():
            shutil.rmtree(p)

    env = dict(os.environ)
    env.update({
        "PROJECT_KEYS": ",".join(f"{t}={k}" for t, k in keys.items()),
        "OPENAI_BASE_URL": "",
        "OPENAI_API_KEY": "",
        "OPENAI_MODEL": "",
        "MCP_DEMO_NOTES_FILE": str(ROOT / "data" / "actions-notes.txt"),
        "SERVICE_DB_URL": f"sqlite+aiosqlite:///{ROOT / 'data' / 'actions-service.db'}",
        "SERVICE_STORAGE": "sql",
        "SERVICE_WORKSPACE_DIR": "./data/actions-workspaces",
        "AGENT_BASE_USAGE_DB": str(ROOT / "data" / "actions-usage.db"),
        "PYTHONUNBUFFERED": "1",
    })
    os.environ["AGENT_BASE_USAGE_DB"] = env["AGENT_BASE_USAGE_DB"]

    procs: list[subprocess.Popen] = []
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    def spawn(name: str, module: str) -> subprocess.Popen:
        fh = open(log_dir / f"actions-{name}.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", module], cwd=ROOT, env=env,
            stdout=fh, stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    try:
        spawn("mcp", "mcp_demo.server")

        # ---------------- 阶段 0：注册表三级化 ----------------
        log("-" * 64)
        log("阶段 0：注册表校验（三级 ALLOW/ASK/DENY）")
        from agent_registry import validate_registry

        errors = validate_registry.validate()
        check("注册表校验通过（三级枚举+技能/SOP 登记）", errors == [], f"-> {errors}")

        orig_servers = validate_registry.load_mcp_servers
        orig_agents = validate_registry.load_agents

        def patched_servers():
            servers = dict(orig_servers())
            servers["deny-probe"] = {
                "name": "deny-probe",
                "tools": [{"name": "nuke", "level": "DENY"}],
            }
            return servers

        def patched_agents():
            agents = [dict(a) for a in orig_agents()]
            probe = dict(agents[0])
            probe["id"] = "deny-probe-agent"
            probe["tools"] = {
                "mcp_servers": [
                    {"name": "deny-probe", "allow": ["nuke"], "deny": []},
                ],
            }
            agents.append(probe)
            return agents

        validate_registry.load_mcp_servers = patched_servers
        validate_registry.load_agents = patched_agents
        try:
            deny_errors = validate_registry.validate()
        finally:
            validate_registry.load_mcp_servers = orig_servers
            validate_registry.load_agents = orig_agents
        check(
            "DENY 工具被校验直接拒绝登记使用",
            any("DENY" in e and "nuke" in e for e in deny_errors),
            f"-> {deny_errors}",
        )

        # ---------------- 阶段 1：装配探针 ----------------
        log("-" * 64)
        log("阶段 1：装配三改（进程内生产装配探针）")
        probe_env = dict(env)
        probe_env.update({
            "SERVICE_DB_URL": f"sqlite+aiosqlite:///{ROOT / 'data' / 'actions-probe.db'}",
            "SERVICE_WORKSPACE_DIR": "./data/actions-probe-workspaces",
            "PROBE_PORT": "8101",
            "PROBE_OUT": str(ROOT / "data" / "actions-probe-result.json"),
        })
        probe_db = ROOT / "data" / "actions-probe.db"
        if probe_db.exists():
            probe_db.unlink()
        probe = subprocess.run(
            [sys.executable, "-m", "probe.probe_assembly"],
            cwd=ROOT, env=probe_env, capture_output=True, text=True, timeout=300,
        )
        try:
            result = json.loads(probe.stdout)
            pc = result.get("checks", {})
        except (ValueError, TypeError):
            pc, result = {}, {}
            info(f"探针输出不可解析：stdout={probe.stdout[:300]} stderr={probe.stderr[-300:]}")
        check(
            "app.state：channel worker 与 scheduler 均显式关闭",
            pc.get("state_enable_channel_worker") is False
            and pc.get("state_enable_scheduler") is False,
            f"-> channel={pc.get('state_enable_channel_worker')} scheduler={pc.get('state_enable_scheduler')}",
        )
        check(
            "lifespan 后运行时：scheduler 未启用、channel dispatcher 未起",
            pc.get("scheduler_manager_enabled") is False
            and pc.get("channel_dispatcher_is_none") is True,
            f"-> scheduler_enabled={pc.get('scheduler_manager_enabled')} dispatcher_none={pc.get('channel_dispatcher_is_none')}",
        )
        check(
            "探针装配下对话全链跑通（mock 回复）",
            pc.get("chat_reply_ok") is True,
            f"-> chat_status={pc.get('chat_status')}",
        )
        check(
            "模型工具清单含装配默认 MCP 的 demo_time",
            pc.get("tool_demo_time_present") is True,
            f"-> tools={result.get('tools_seen')}",
        )
        info(
            "Team 工具实测"
            f"：{pc.get('tool_team_present')}（框架对非 worker 会话无条件挂载，"
            "无装配开关——见 NOTES）；"
            f"AgentInvite 在场={pc.get('tool_agent_invite_present')}"
            "（需有 invitable 的 agent 才挂，demo 默认不挂）；"
            f"Schedule* 初始 schema 在场={pc.get('tool_schedule_present')}"
            "（以 schedule_tools 工具组形式挂载、默认不激活，"
            "enable_scheduler=False 只停触发不摘工具——见 NOTES）",
        )
        check(
            "AgentInvite 未挂载（demo agent 未开 invitable）",
            pc.get("tool_agent_invite_present") is False,
            f"-> invite={pc.get('tool_agent_invite_present')}",
        )

        # ---------------- 主栈 ----------------
        spawn("service", "service.run")
        spawn("gateway", "gateway.run")
        gw = httpx.Client(base_url=GATEWAY, timeout=60, trust_env=False)
        svc = httpx.Client(base_url=SERVICE, timeout=30, trust_env=False)
        check("gateway health", wait_health(f"{GATEWAY}/health", {}))
        check(
            "service health",
            wait_health(f"{SERVICE}/health", svc_headers("tenant-a", "alice")),
        )

        def gw_headers(tenant: str, end_user: str | None = None) -> dict:
            h = {"X-Project-Key": keys[tenant]}
            if end_user:
                h["X-End-User-Id"] = end_user
            return h

        def svc_agent_id(tenant: str, end_user: str, name: str) -> str | None:
            r = svc.get("/agent/", headers=svc_headers(tenant, end_user))
            for a in r.json().get("agents", []):
                if (a.get("data") or {}).get("name") == name:
                    return a.get("id")
            return None

        # ---------------- 阶段 2：项目级共享策略 ----------------
        log("-" * 64)
        log("阶段 2：TenantPrefixPolicy（直查服务 API）")
        r = svc.post(
            "/credential/", headers=svc_headers("tenant-a", "alice"),
            json={"data": {"type": "mock_credential", "name": "policy-probe"}},
        )
        check("alice 建凭证", r.status_code == 201, f"-> {r.status_code}")
        probe_cred_id = r.json().get("credential_id") if r.status_code == 201 else None

        r = svc.get("/credential/", headers=svc_headers("tenant-a", "bob"))
        bob_creds = r.json().get("credentials", []) if r.status_code == 200 else []
        shared = [c for c in bob_creds if c.get("id") == probe_cred_id]
        check(
            "同租户 bob 能经政策读到 alice 的凭证（只读、脱敏）",
            len(shared) == 1
            and shared[0].get("user_id") == "tenant-a:alice"
            and shared[0].get("editable") is False
            and set((shared[0].get("data") or {}).keys()) <= {"type", "name"},
            f"-> shared={shared}",
        )
        r = svc.get("/credential/", headers=svc_headers("tenant-b", "carol"))
        carol_creds = r.json().get("credentials", []) if r.status_code == 200 else []
        check(
            "异租户 carol 读不到该凭证（跨租户全拒）",
            all(c.get("id") != probe_cred_id for c in carol_creds),
            f"-> carol_creds={[c.get('id') for c in carol_creds]}",
        )
        # KB 同路尝试：创建需 embedding 配置（维度由调用方声明）。
        kb_id = None
        try:
            r = svc.post(
                "/knowledge_bases/", headers=svc_headers("tenant-a", "alice"),
                json={
                    "name": "policy-probe-kb",
                    "embedding_model_config": {
                        "type": "mock_credential",
                        "credential_id": probe_cred_id,
                        "model": "mock-1",
                        "dimensions": 8,
                    },
                },
            )
            if r.status_code == 201:
                kb_id = r.json().get("knowledge_base_id")
            else:
                info(f"KB 创建未成（{r.status_code}：{r.text[:160]}）——本阶段以凭证为据，KB 与凭证共用同一政策合并路径")
        except Exception as exc:  # noqa: BLE001
            info(f"KB 创建异常（{exc}）——同上，以凭证为据")
        if kb_id:
            r = svc.get("/knowledge_bases/", headers=svc_headers("tenant-a", "bob"))
            bob_kbs = r.json().get("knowledge_bases", []) if r.status_code == 200 else []
            r2 = svc.get("/knowledge_bases/", headers=svc_headers("tenant-b", "carol"))
            carol_kbs = r2.json().get("knowledge_bases", []) if r2.status_code == 200 else []
            check(
                "同租户 bob 能读到 alice 的 KB、异租户 carol 不能",
                any(k.get("id") == kb_id for k in bob_kbs)
                and all(k.get("id") != kb_id for k in carol_kbs),
                f"-> bob={[k.get('id') for k in bob_kbs]} carol={[k.get('id') for k in carol_kbs]}",
            )

        # ---------------- 阶段 3：组织级凭证 ----------------
        log("-" * 64)
        log("阶段 3：组织级凭证（系统户持有、同租户共用）")
        r = gw.post(
            "/v1/sessions", headers=gw_headers("tenant-a", "dave"),
            json={"agent_id": "tenant-a.assistant"},
        )
        check("tenant-a 网关建会话", r.status_code == 201, f"-> {r.status_code}")
        sess_a3 = r.json()["session_id"] if r.status_code == 201 else None
        if sess_a3:
            r = gw.post(
                f"/v1/sessions/{sess_a3}/messages",
                headers=gw_headers("tenant-a", "dave"), json={"text": "你好"},
            )
            d = r.json() if r.status_code == 200 else {}
            check(
                "终端 user 会话引用系统户凭证跑通 mock 对话",
                r.status_code == 200 and d.get("status") == "completed"
                and "[mock]" in str(d.get("reply", "")),
                f"-> {str(d.get('reply'))[:60]}",
            )
        r = svc.get("/credential/", headers=svc_headers("tenant-a", "__system__"))
        sys_creds = r.json().get("credentials", []) if r.status_code == 200 else []
        sys_cred = next(
            (c for c in sys_creds
             if (c.get("data") or {}).get("type") == "mock_credential"
             and (c.get("data") or {}).get("name") == "mock"),
            None,
        )
        sys_cred_id = sys_cred.get("id") if sys_cred else None
        check(
            "组织凭证建在系统户 tenant-a:__system__ 名下",
            sys_cred is not None and sys_cred.get("user_id") == "tenant-a:__system__",
            f"-> {sys_cred}",
        )
        r = svc.get("/credential/", headers=svc_headers("tenant-a", "dave"))
        dave_creds = r.json().get("credentials", []) if r.status_code == 200 else []
        check(
            "终端用户 dave 名下无个人凭证、系统户凭证以共享只读形式可见",
            all(c.get("user_id") != "tenant-a:dave" for c in dave_creds)
            and any(
                c.get("id") == sys_cred_id and c.get("editable") is False
                for c in dave_creds
            ),
            f"-> dave_creds={[(c.get('id'), c.get('user_id'), c.get('editable')) for c in dave_creds]}",
        )
        # 异租户引用系统户凭证：框架在建会话时即经政策解析凭证，
        # 跨租户应 404；退一步即使建会话成功，对话也不许跑出回复。
        cross_ok = False
        cross_detail = "未执行（缺系统户凭证 id）"
        if sys_cred_id:
            r = svc.post(
                "/agent/", headers=svc_headers("tenant-b", "carol"),
                json={"name": "cross-probe", "system_prompt": "x"},
            )
            cross_agent = r.json().get("agent_id") if r.status_code == 201 else None
            if cross_agent:
                r = svc.post(
                    "/sessions/", headers=svc_headers("tenant-b", "carol"),
                    json={
                        "agent_id": cross_agent,
                        "chat_model_config": {
                            "type": "mock_credential",
                            "credential_id": sys_cred_id,
                            "model": "mock-1",
                            "parameters": {},
                        },
                    },
                )
                if r.status_code == 404:
                    cross_ok = True
                    cross_detail = f"-> 建会话即 404 拒绝：{r.text[:100]}"
                elif r.status_code == 201:
                    cross_session = r.json().get("session_id")
                    svc.post(
                        "/chat/", headers=svc_headers("tenant-b", "carol"),
                        json={
                            "agent_id": cross_agent,
                            "session_id": cross_session,
                            "input": {
                                "name": "carol", "role": "user",
                                "content": [{"type": "text", "text": "你好"}],
                            },
                        },
                    )
                    got_mock = False
                    for _ in range(15):
                        time.sleep(1)
                        r = svc.get(
                            f"/sessions/{cross_session}/messages",
                            headers=svc_headers("tenant-b", "carol"),
                            params={"agent_id": cross_agent},
                        )
                        text_all = json.dumps(
                            r.json().get("messages", []), ensure_ascii=False,
                        )
                        if "[mock]" in text_all:
                            got_mock = True
                            break
                    cross_ok = not got_mock
                    cross_detail = (
                        "-> 会话建成但 15s 内无 [mock] 回复" if cross_ok
                        else "-> 竟然拿到了 mock 回复（真泄漏）"
                    )
                else:
                    cross_detail = f"-> 建会话返回意外状态 {r.status_code}"
        check(
            "异租户引用系统户 credential_id 被拒（建会话 404 或对话无回复）",
            cross_ok,
            cross_detail,
        )

        # ---------------- 阶段 4：按租户动态注入 ----------------
        log("-" * 64)
        log("阶段 4：按租户动态注入 MCP/技能")
        r = gw.post(
            "/v1/sessions", headers=gw_headers("tenant-a", "alice"),
            json={"agent_id": "tenant-a.assistant"},
        )
        sess_a = r.json()["session_id"] if r.status_code == 201 else None
        check("tenant-a 建会话（触发注入）", r.status_code == 201, f"-> {r.status_code}")
        agent_a = svc_agent_id("tenant-a", "alice", "tenant-a.assistant")
        if sess_a and agent_a:
            r = svc.get(
                "/workspace/mcp", headers=svc_headers("tenant-a", "alice"),
                params={"agent_id": agent_a, "session_id": sess_a},
            )
            mcps = r.json() if r.status_code == 200 else []
            names = [c.get("name") for c in mcps]
            a_tools = next((c for c in mcps if c.get("name") == "tenant-a-tools"), None)
            check(
                "A 会话工作区有专属 MCP tenant-a-tools（含 demo_time）",
                a_tools is not None
                and any(
                    str(t.get("name", "")).endswith("__demo_time")
                    for t in a_tools.get("tools", [])
                ),
                f"-> mcps={names} tools={[t.get('name') for t in (a_tools or {}).get('tools', [])]}",
            )
            r = svc.get(
                "/workspace/skill", headers=svc_headers("tenant-a", "alice"),
                params={"agent_id": agent_a, "session_id": sess_a},
            )
            skill_names = [s.get("name") for s in (r.json() if r.status_code == 200 else [])]
            check(
                "A 会话有 default 技能 tenant-a-hello、无 optional 技能",
                "tenant-a-hello" in skill_names
                and "tenant-a-optional" not in skill_names,
                f"-> skills={skill_names}",
            )
        r = gw.post(
            "/v1/sessions", headers=gw_headers("tenant-b", "bob"),
            json={"agent_id": "tenant-b.assistant"},
        )
        sess_b = r.json()["session_id"] if r.status_code == 201 else None
        check("tenant-b 建会话", r.status_code == 201, f"-> {r.status_code}")
        agent_b = svc_agent_id("tenant-b", "bob", "tenant-b.assistant")
        if sess_b and agent_b:
            r = svc.get(
                "/workspace/mcp", headers=svc_headers("tenant-b", "bob"),
                params={"agent_id": agent_b, "session_id": sess_b},
            )
            names_b = [c.get("name") for c in (r.json() if r.status_code == 200 else [])]
            r = svc.get(
                "/workspace/skill", headers=svc_headers("tenant-b", "bob"),
                params={"agent_id": agent_b, "session_id": sess_b},
            )
            skills_b = [s.get("name") for s in (r.json() if r.status_code == 200 else [])]
            check(
                "B 会话无 A 的专属 MCP 与技能",
                "tenant-a-tools" not in names_b and "tenant-a-hello" not in skills_b,
                f"-> mcps={names_b} skills={skills_b}",
            )

        # ---------------- 阶段 5：工作区产物回收 ----------------
        log("-" * 64)
        log("阶段 5：工作区产物回收（经服务 API）")
        collect_ok = False
        detail = ""
        if sess_a and agent_a:
            r = svc.get(
                "/workspace/status", headers=svc_headers("tenant-a", "alice"),
                params={"agent_id": agent_a, "session_id": sess_a},
            )
            workdir = r.json().get("workdir") if r.status_code == 200 else None
            if workdir:
                fixture_text = "collect-probe-回收验证-12345"
                Path(workdir, "collect-probe.txt").write_text(
                    fixture_text, encoding="utf-8",
                )
                sub = Path(workdir) / "sub"
                sub.mkdir(exist_ok=True)
                (sub / "nested.txt").write_text("nested-嵌套文件", encoding="utf-8")

                async def _collect() -> dict:
                    from gateway.adapter import AgentScopeAdapter

                    ad = AgentScopeAdapter()
                    handle = await ad.resolve_session(sess_a, "tenant-a", "alice")
                    if handle is None:
                        return {"files": []}
                    return await ad.collect_artifacts(handle)

                result = asyncio.run(_collect())
                got = Path(result["dir"]) / "collect-probe.txt"
                nested = Path(result["dir"]) / "sub" / "nested.txt"
                collect_ok = (
                    "collect-probe.txt" in result["files"]
                    and "sub/nested.txt" in result["files"]
                    and got.exists()
                    and got.read_text(encoding="utf-8") == fixture_text
                    and nested.exists()
                )
                detail = f"-> dir={result['dir']} files={result['files']}"
            else:
                detail = f"-> /workspace/status 未给出 workdir：{r.status_code} {r.text[:120]}"
        check("工作区文件经 API 回收到 data/artifacts 且内容一致", collect_ok, detail)

        # ---------------- 阶段 6：SOP 分发 ----------------
        log("-" * 64)
        log("阶段 6：SOP 分发落库（系统户持有）")
        r = gw.post(
            "/v1/sessions", headers=gw_headers("demo-dev", "demo-user"),
            json={"agent_id": "demo.echo-assistant"},
        )
        check("demo-dev 建会话（触发租户 SOP ensure）", r.status_code == 201, f"-> {r.status_code}")
        r = svc.get("/sop/", headers=svc_headers("demo-dev", "__system__"))
        sys_sops = r.json().get("sops", []) if r.status_code == 200 else []
        sop_names = [(s.get("data") or {}).get("name") for s in sys_sops]
        check(
            "注册表 SOP demo-onboarding 已落库到系统户",
            "demo-onboarding" in sop_names,
            f"-> system sops={sop_names}",
        )
        r = svc.get("/sop/", headers=svc_headers("demo-dev", "demo-user"))
        user_sops = r.json().get("sops", []) if r.status_code == 200 else []
        user_sop_names = [(s.get("data") or {}).get("name") for s in user_sops]
        check(
            "终端用户看不到系统户 SOP（无共享通道，缺口如实记录）",
            "demo-onboarding" not in user_sop_names,
            f"-> user sops={user_sop_names}",
        )

        return finish(procs)
    except Exception as exc:  # noqa: BLE001
        import traceback

        log(f"[FAIL] 冒烟中途异常：{exc}")
        log(traceback.format_exc())
        FAILURES.append("exception")
        return finish(procs)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


def finish(procs: list[subprocess.Popen]) -> int:
    log("-" * 64)
    if FAILURES:
        log(f"RESULT: FAIL（{len(FAILURES)} 项未过：{', '.join(FAILURES)}）")
        code = 1
    else:
        log("RESULT: ALL PASS")
        code = 0
    (ROOT / "smoke_output_actions.txt").write_text(
        "\n".join(LINES) + "\n", encoding="utf-8",
    )
    for p in procs:
        p.terminate()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
