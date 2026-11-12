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
    """该租户登记的技能（含本体目录绝对路径 skill_path）。"""
    out = []
    for skill in load_skills():
        if skill.get("tenant") != tenant:
            continue
        if scope is not None and skill.get("scope", "default") != scope:
            continue
        entry = dict(skill)
        entry["skill_path"] = SKILLS_STORE_DIR / str(skill.get("dir", ""))
        out.append(entry)
    return out


def sops_for_tenant(tenant: str) -> list[dict]:
    return [s for s in load_sops() if s.get("tenant") == tenant]


def skill_files(skill_path: Path) -> list[Path]:
    """技能文件夹内全部文件（相对路径排序，决定 upload 的发送顺序）。"""
    return sorted(
        (p for p in skill_path.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(skill_path)),
    )
