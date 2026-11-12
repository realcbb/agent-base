# -*- coding: utf-8 -*-
"""启动网关：python -m gateway.run（默认 127.0.0.1:8000）"""
import os

import uvicorn

from common.config import load_env

if __name__ == "__main__":
    load_env()
    uvicorn.run(
        "gateway.app:app",
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", "8000")),
    )
