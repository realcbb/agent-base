# -*- coding: utf-8 -*-
"""极简 .env 加载与配置读取（避免额外依赖）。

密钥只从环境变量/.env 读，代码与仓库文件中不出现真实密钥。
"""
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env(env_file: str | Path | None = None) -> None:
    path = Path(env_file) if env_file else _PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def project_root() -> Path:
    return _PROJECT_ROOT


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def parse_project_keys(raw: str) -> dict[str, str]:
    """'tenant1=key1,tenant2=key2' -> {tenant: key}"""
    result: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        tenant, _, key = item.partition("=")
        result[tenant.strip()] = key.strip()
    return result
