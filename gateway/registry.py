# -*- coding: utf-8 -*-
"""注册表的租户维度访问层（网关侧）。

agent_registry/loader.py 是全量加载；本模块按租户过滤出「这个租户登记了
什么」，供 adapter 在建会话时做按租户动态注入（MCP/技能）与租户级供给
（SOP 落库）。注册表仍是唯一事实来源，本模块不落任何状态。

分工（注册与绑定分离）：
- 登记在注册表：MCP 的连接模板（url_env 等）、工具级别、技能本体目录、
  SOP 定义，全部带 tenant 归属与 scope（default/optional）标记；
- 绑定在会话：网关建会话后按会话租户把 default 项注入该会话工作区
  （MCP 走 POST /workspace/mcp 全量直加，技能走 POST /workspace/skill/upload，
  均不经服务侧用户库/from-library 通道）。

技能有两层来源，注入时合并（2026-11-13 动态发布定版）：
- 静态层：skills.yaml＋skills_store 目录（随镜像走，demo/内置技能）；
- 动态层：业务专家经管理端点发布进技能仓储（gateway.skill_store），
  免镜像、免重启，对新建会话立即生效。
可见性：技能归属为具体租户或 global（全租户通用；静态层未写 tenant
同此处理，与 MCP 的口径一致）。同名冲突的覆盖顺序（后者胜）：
global 静态 < global 动态 < 本租户静态 < 本租户动态——租户专属可
覆盖通用版，动态发布可覆盖镜像内置版。
"""
from pathlib import Path

from agent_registry.loader import (
    SKILLS_STORE_DIR,
    load_mcp_servers,
    load_skills,
    load_sops,
)


def mcp_servers_for_tenant(tenant: str, scope: str | None = "default") -> list[dict]:
    """该租户登记的 MCP Server；scope=None 表示不限注入时机。

    未声明 tenants 的登记视为全租户可见（兼容早期登记），声明了的
    严格按名单匹配。
    """
    out = []
    for server in load_mcp_servers().values():
        tenants = server.get("tenants")
        if tenants is not None and tenant not in tenants:
            continue
        if scope is not None and server.get("scope", "default") != scope:
            continue
        out.append(server)
    return out


def skills_for_tenant(tenant: str, scope: str | None = "default") -> list[dict]:
    """该租户可见的技能（静态＋动态合并，规则见模块 docstring）。

    静态条目带 skill_path（本体目录）；动态条目带 dynamic=True 与
    version（仓储当前版本号），本体经 skill_package_files 现取。
    """
    from gateway.skill_store import GLOBAL_TENANT, get_skill_store

    static_entries: list[dict] = []
    for skill in load_skills():
        entry = dict(skill)
        entry["tenant"] = entry.get("tenant") or GLOBAL_TENANT
        entry["skill_path"] = SKILLS_STORE_DIR / str(entry.get("dir", ""))
        static_entries.append(entry)

    dynamic_entries: list[dict] = []
    store = get_skill_store()
    if store.healthy:
        for row in store.list_skills([GLOBAL_TENANT, tenant]):
            if row["status"] != "published" or row["current_version"] is None:
                continue
            dynamic_entries.append(
                {
                    "name": row["name"],
                    "tenant": row["tenant"],
                    "scope": row["injection"],
                    "dir": row["name"],
                    "dynamic": True,
                    "version": row["current_version"],
                },
            )

    merged: dict[str, dict] = {}
    for pool in (
        [e for e in static_entries if e["tenant"] == GLOBAL_TENANT],
        [e for e in dynamic_entries if e["tenant"] == GLOBAL_TENANT],
        [e for e in static_entries if e["tenant"] == tenant],
        [e for e in dynamic_entries if e["tenant"] == tenant],
    ):
        for entry in pool:
            merged[entry["name"]] = entry
    out = []
    for entry in merged.values():
        if scope is not None and entry.get("scope", "default") != scope:
            continue
        out.append(entry)
    return out


def skill_package_files(skill: dict) -> list[tuple[str, bytes]]:
    """技能本体 → [(相对路径, 内容)]（路径排序，决定 upload 顺序）。

    静态层从 skills_store 目录现读；动态层按合并时钉住的版本号从
    仓储现取整包解开——两个副本、发布并发下都不会取串版本。
    """
    if skill.get("dynamic"):
        from gateway.skill_store import get_skill_store, unzip_package

        pkg = get_skill_store().get_package(
            skill["tenant"], skill["name"], version=skill.get("version"),
        )
        if pkg is None:
            raise FileNotFoundError(
                f"动态技能 {skill['tenant']}/{skill['name']} "
                f"v{skill.get('version')} 在仓储中不存在",
            )
        return unzip_package(pkg["zip_bytes"])
    skill_path: Path = skill["skill_path"]
    return [
        (p.relative_to(skill_path).as_posix(), p.read_bytes())
        for p in skill_files(skill_path)
    ]


def sops_for_tenant(tenant: str) -> list[dict]:
    return [s for s in load_sops() if s.get("tenant") == tenant]


def skill_files(skill_path: Path) -> list[Path]:
    """技能文件夹内全部文件（相对路径排序，决定 upload 的发送顺序）。"""
    return sorted(
        (p for p in skill_path.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(skill_path)),
    )
