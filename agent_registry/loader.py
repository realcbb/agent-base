# -*- coding: utf-8 -*-
"""Agent 注册表加载（YAML）。网关与校验脚本共用。"""
from pathlib import Path

import yaml

REGISTRY_DIR = Path(__file__).resolve().parent
SKILLS_STORE_DIR = REGISTRY_DIR.parent / "skills_store"

# 工具权限三级（2026-11-07 由 L0–L3 四级收敛，设计 8.2 修订口径）：
#   ALLOW = 放行（只读工具经 MCP 注解 readOnlyHint 走框架快速通道）；
#   ASK   = 执行前人工确认（写工具不标注、框架默认 ASK）；
#   DENY  = 禁登（校验直接拒绝任何 Agent 引用，见 validate_registry）。
PERMISSION_ORDER = {"ALLOW": 0, "ASK": 1, "DENY": 2}

# 注入时机标记（设计：注册与绑定分离；default 建会话时自动注入，
# optional 只登记、由会话显式点名才注入——网关默认不注入 optional）。
SCOPES = ("default", "optional")


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_models() -> dict[str, dict]:
    data = _load_yaml(REGISTRY_DIR / "models.yaml")
    return {m["name"]: m for m in data.get("models", [])}


def load_mcp_servers() -> dict[str, dict]:
    data = _load_yaml(REGISTRY_DIR / "mcp_servers.yaml")
    return {s["name"]: s for s in data.get("mcp_servers", [])}


def load_skills() -> list[dict]:
    data = _load_yaml(REGISTRY_DIR / "skills.yaml")
    return data.get("skills", [])


def load_sops() -> list[dict]:
    data = _load_yaml(REGISTRY_DIR / "sops.yaml")
    return data.get("sops", [])


def load_agents() -> list[dict]:
    agents = []
    for path in sorted(REGISTRY_DIR.glob("*.yaml")):
        if path.name in (
            "models.yaml",
            "mcp_servers.yaml",
            "skills.yaml",
            "sops.yaml",
        ):
            continue
        data = _load_yaml(path)
        if isinstance(data, dict) and "id" in data:
            data["_file"] = path.name
            agents.append(data)
    return agents


def load_agent(agent_id: str) -> dict | None:
    for agent in load_agents():
        if agent["id"] == agent_id:
            return agent
    return None
