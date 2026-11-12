# -*- coding: utf-8 -*-
"""生成本地 demo .env（随机 demo key）。已存在 .env 时不覆盖。

用法：python scripts/gen_demo_env.py
真实密钥只写入本地 .env（.gitignore 排除），不进仓库文件。
"""
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    env_path = ROOT / ".env"
    if env_path.exists():
        print(f".env 已存在，不覆盖：{env_path}")
        return 0
    key = secrets.token_urlsafe(32)
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    content = template.replace("REPLACE_WITH_RANDOM_KEY", key)
    env_path.write_text(content, encoding="utf-8")
    print(f"已生成 {env_path}")
    print(f"demo 租户 demo-dev 的 X-Project-Key：{key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
