FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY common ./common
COPY service ./service
COPY gateway ./gateway
COPY mcp_demo ./mcp_demo
COPY agent_registry ./agent_registry
COPY skills_store ./skills_store

RUN pip install . && mkdir -p /app/data /app/logs \
    && useradd -r agentbase && chown -R agentbase /app
USER agentbase

# 默认跑网关；service / mcp 在 compose 里用 command 覆盖
CMD ["python", "-m", "gateway.run"]
