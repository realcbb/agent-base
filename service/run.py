# -*- coding: utf-8 -*-
"""以模块方式启动原生服务：python -m service.run"""
import os

import uvicorn

from common.config import load_env

if __name__ == "__main__":
    load_env()
    uvicorn.run(
        "service.app:app",
        host=os.environ.get("SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SERVICE_PORT", "8100")),
    )
