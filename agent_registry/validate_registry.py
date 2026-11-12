# -*- coding: utf-8 -*-
"""注册表校验（设计文档 6.2/6.3 的自动检查最小实现）。

用法：python -m agent_registry.validate_registry
检查项：
1. 必填字段（id/version/tenant/model.primary/system_prompt_file/tools/
   permissions.max_level/limits）；
2. 引用完整性：model.primary/fallback 必须在 models.yaml 中；
   tools.mcp_servers 必须在 mcp_servers.yaml 中，allow 的工具必须存在；
3. 权限越级：allow 中工具的级别不得超过 permissions.max_level；
   级别为 DENY 的工具禁止登记使用（任何 Agent 引用即报错）；
4. system_prompt_file 必须存在；
5. MCP 登记扩展：工具 level 必须是三级枚举（ALLOW/ASK/DENY），
   tenants/scope 若出现必须合法（scope ∈ default/optional）；
6. 技能登记：tenant/scope/dir 合法，本体目录在 skills_store/ 下且
   SKILL.md 存在、frontmatter 的 name 与登记名一致；
7. SOP 登记：name 在同租户内唯一，agent 必须是该租户已登记 Agent，
   steps 非空、每步 subject/description 齐全。
8. 模型登记：capabilities 必须在词表（tool_call/vision/reasoning）
   内；Agent 的 model.vision 若声明，必须在 models.yaml 中且该模型
   必须标 vision（网关切换策略的登记前提）。
退出码：全部通过 0，否则 1。
"""
import sys

import frontmatter

from agent_registry.loader import (
    PERMISSION_ORDER,
    REGISTRY_DIR,
    SCOPES,
    SKILLS_STORE_DIR,
    load_agents,
    load_mcp_servers,
    load_models,
    load_skills,
    load_sops,
)

REQUIRED = [
    "id",
    "version",
    "tenant",
    "display_name",
    "model",
    "system_prompt_file",
    "tools",
    "permissions",
    "limits",
]


CAPABILITY_VOCABULARY = {"tool_call", "vision", "reasoning"}


def validate() -> list[str]:
    errors: list[str] = []
    models = load_models()
    for name, entry in models.items():
        for cap in entry.get("capabilities") or []:
            if cap not in CAPABILITY_VOCABULARY:
                errors.append(f"[models] {name} capabilities 含词表外标记 {cap}")
    servers = load_mcp_servers()
    agents = load_agents()
    if not agents:
        errors.append("注册表中没有任何 Agent 配置")
    seen_ids: set[str] = set()
    for agent in agents:
        tag = agent.get("id", agent.get("_file", "?"))
        for field in REQUIRED:
            if field not in agent:
                errors.append(f"[{tag}] 缺少必填字段 {field}")
        if tag in seen_ids:
            errors.append(f"[{tag}] id 重复")
        seen_ids.add(tag)

        model = agent.get("model") or {}
        primary = model.get("primary")
        if not primary:
            errors.append(f"[{tag}] model.primary 必填")
        elif primary not in models:
            errors.append(f"[{tag}] model.primary 引用未登记模型 {primary}")
        fallback = model.get("fallback")
        if fallback and fallback not in models:
            errors.append(f"[{tag}] model.fallback 引用未登记模型 {fallback}")
        vision = model.get("vision")
        if vision:
            if vision not in models:
                errors.append(f"[{tag}] model.vision 引用未登记模型 {vision}")
            elif "vision" not in (models[vision].get("capabilities") or []):
                errors.append(f"[{tag}] model.vision 指向的模型 {vision} 未标 vision 能力")
        # 禁止在 Agent 配置中直写端点/密钥（设计 6.2）
        for bad in ("base_url", "api_key"):
            if bad in model:
                errors.append(f"[{tag}] model 中禁止直写 {bad}")

        prompt_file = agent.get("system_prompt_file")
        if prompt_file and not (REGISTRY_DIR / prompt_file).exists():
            errors.append(f"[{tag}] system_prompt_file 不存在：{prompt_file}")

        # knowledge_bases：以名字声明租户知识库（资产归系统户，
        # 网关建会话时按名解析、原生校验可见性）。此处只验形状；
        # 名字是否存在属运行时解析（kb_report 记 missing）。
        kb_names = agent.get("knowledge_bases")
        if kb_names is not None:
            if not isinstance(kb_names, list) or not all(
                isinstance(n, str) and n.strip() for n in kb_names
            ):
                errors.append(
                    f"[{tag}] knowledge_bases 须为非空字符串数组",
                )
            elif len(set(kb_names)) != len(kb_names):
                errors.append(f"[{tag}] knowledge_bases 含重复名字")

        max_level = (agent.get("permissions") or {}).get("max_level")
        if max_level not in PERMISSION_ORDER:
            errors.append(f"[{tag}] permissions.max_level 非法：{max_level}")

        tools = agent.get("tools") or {}
        for entry in tools.get("mcp_servers") or []:
            name = entry.get("name")
            server = servers.get(name)
            if server is None:
                errors.append(f"[{tag}] 引用未登记 MCP Server：{name}")
                continue
            known = {t["name"]: t for t in server.get("tools", [])}
            for tool_name in entry.get("allow") or []:
                tool = known.get(tool_name)
                if tool is None:
                    errors.append(
                        f"[{tag}] allow 的工具 {tool_name} 不在 {name} 登记清单中",
                    )
                elif tool["level"] == "DENY":
                    errors.append(
                        f"[{tag}] 工具 {tool_name} 级别为 DENY（禁登），"
                        "禁止登记使用",
                    )
                elif (
                    max_level in PERMISSION_ORDER
                    and PERMISSION_ORDER[tool["level"]] > PERMISSION_ORDER[max_level]
                ):
                    errors.append(
                        f"[{tag}] 工具 {tool_name} 级别 {tool['level']} "
                        f"超过 max_level {max_level}（权限越级）",
                    )

    # ---- MCP 登记本身：级别枚举与租户/注入标记 ----
    for name, server in servers.items():
        scope = server.get("scope", "default")
        if scope not in SCOPES:
            errors.append(f"[mcp:{name}] scope 非法：{scope}")
        tenants = server.get("tenants")
        if tenants is not None and (
            not isinstance(tenants, list) or not tenants
        ):
            errors.append(f"[mcp:{name}] tenants 必须是非空列表")
        for tool in server.get("tools", []):
            if tool.get("level") not in PERMISSION_ORDER:
                errors.append(
                    f"[mcp:{name}] 工具 {tool.get('name')} level 非法："
                    f"{tool.get('level')}（须为 ALLOW/ASK/DENY）",
                )

    # ---- 技能登记：本体自托管校验 ----
    seen_skills: set[tuple[str, str]] = set()
    for skill in load_skills():
        tag = f"[skill:{skill.get('name', '?')}]"
        for field in ("name", "tenant", "scope", "dir"):
            if field not in skill:
                errors.append(f"{tag} 缺少必填字段 {field}")
        if skill.get("scope") not in SCOPES:
            errors.append(f"{tag} scope 非法：{skill.get('scope')}")
        key = (skill.get("tenant"), skill.get("name"))
        if key in seen_skills:
            errors.append(f"{tag} 同租户内技能名重复")
        seen_skills.add(key)
        skill_dir = SKILLS_STORE_DIR / str(skill.get("dir", ""))
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir():
            errors.append(f"{tag} 本体目录不存在：skills_store/{skill.get('dir')}")
        elif not skill_md.exists():
            errors.append(f"{tag} 本体缺少 SKILL.md：skills_store/{skill.get('dir')}")
        else:
            try:
                post = frontmatter.load(str(skill_md))
                if post.get("name") != skill.get("name"):
                    errors.append(
                        f"{tag} SKILL.md 的 name 与登记名不一致："
                        f"{post.get('name')}",
                    )
            except Exception as exc:  # noqa: BLE001 - 校验脚本如实报错
                errors.append(f"{tag} SKILL.md 解析失败：{exc}")

    # ---- SOP 登记 ----
    agents_by_tenant: dict[str, set[str]] = {}
    for a in agents:
        agents_by_tenant.setdefault(a.get("tenant"), set()).add(a.get("id"))
    seen_sops: set[tuple[str, str]] = set()
    for sop in load_sops():
        tag = f"[sop:{sop.get('name', '?')}]"
        for field in ("name", "tenant", "agent", "steps"):
            if field not in sop:
                errors.append(f"{tag} 缺少必填字段 {field}")
        key = (sop.get("tenant"), sop.get("name"))
        if key in seen_sops:
            errors.append(f"{tag} 同租户内 SOP 名重复")
        seen_sops.add(key)
        if sop.get("agent") not in agents_by_tenant.get(sop.get("tenant"), set()):
            errors.append(
                f"{tag} agent {sop.get('agent')} 不是租户 "
                f"{sop.get('tenant')} 的已登记 Agent",
            )
        steps = sop.get("steps") or []
        if not steps:
            errors.append(f"{tag} steps 不能为空")
        for i, step in enumerate(steps):
            if not step.get("subject") or not step.get("description"):
                errors.append(f"{tag} steps[{i}] 缺 subject/description")
    return errors


def main() -> int:
    errors = validate()
    agents = load_agents()
    if errors:
        print("注册表校验 FAILED：")
        for e in errors:
            print(" -", e)
        return 1
    print(
        f"注册表校验 OK：{len(agents)} 个 Agent、"
        f"{len(load_skills())} 个技能、{len(load_sops())} 个 SOP",
    )
    for a in agents:
        print(f" - {a['id']} v{a['version']} tenant={a['tenant']} model={a['model']['primary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
