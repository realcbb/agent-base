# -*- coding: utf-8 -*-
"""AgentScope Agent Service 装配入口（设计文档第 3、4 章）。

装配内容（全部为探针实测的真实 API，agentscope 2.0.11）：
- create_app(storage, message_bus, workspace_manager, ...) 三件必填；
- storage：默认 AsyncSQLAlchemyStorage（连接串 SERVICE_DB_URL，
  生产为 MySQL），SERVICE_STORAGE=redis 时换 RedisStorage；
- message_bus：由 SERVICE_BUS 独立选择（memory｜redis），与存储
  解耦——生产组合为「MySQL 存储＋Redis 总线」。SERVICE_BUS 未设置
  时沿旧行为（存储 redis 则总线 redis、否则进程内）兼容既有部署；
- workspace_manager：LocalWorkspaceManager（per_session 隔离）；
  装配不种 default_mcps——MCP 一律走注册表，由网关在建会话后
  按租户注入（agent_registry/mcp_servers.yaml）；
- extra_credentials=[MockCredential] 注册 mock 模型；
- enable_channel_worker=False / enable_scheduler=False：排查清单
  H1/D1/D2 的处置——频道子系统（个人助理的 IM 入口假设＋凭证
  明文落库）不属本基座形态；调度触发不归框架管（基座定时需求由
  业务系统/网关调度）。注意实测边界：关调度只停本节点的定时触发
  循环，Schedule* 工具仍挂在会话工具组里（框架 list_tools 不看
  enabled），详见 NOTES；
- resource_access_policy=TenantPrefixPolicy()：项目级共享正门
  （service/access_policy.py）——同租户 {tenant}: 前缀内 KB/凭证
  只读互通，跨租户全拒；
- 身份：框架原生 deps.get_current_user_id 读 X-User-ID 头。本装配加一层
  ASGI 中间件：当请求带网关可信头 X-Tenant-Id + X-End-User-Id 且无
  X-User-ID 时，按设计 5.1 规则注入 X-User-ID = {tenant}:{end_user}。
  生产环境须以网络策略保证本服务只接受网关流量（设计 5.2）。
"""
import os

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus, RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.storage._sql import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import (
    IsolationPolicy,
    LocalWorkspaceManager,
)

from common.config import env, load_env, resolve_path
from service.access_policy import TenantPrefixPolicy
from service.mock_model import MockCredential


class TrustedHeaderIdentityMiddleware:
    """把网关可信头映射为框架的 X-User-ID（纯 ASGI，不改框架源码）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            tenant = headers.get(b"x-tenant-id", b"").decode()
            end_user = headers.get(b"x-end-user-id", b"").decode()
            if tenant and end_user and b"x-user-id" not in headers:
                user_id = f"{tenant}:{end_user}".encode()
                scope["headers"] = list(headers.items()) + [
                    (b"x-user-id", user_id),
                ]
        await self.app(scope, receive, send)


def build_service_app():
    load_env()
    storage_kind = env("SERVICE_STORAGE", "sql")
    redis_host = env("SERVICE_REDIS_HOST", "127.0.0.1")
    redis_port = int(env("SERVICE_REDIS_PORT", "6379"))
    # 总线与存储独立选择（2026-11-08 解耦）：SERVICE_BUS 未设置时沿
    # 旧捆绑行为，保证既有部署与冒烟的 env 不变、行为不变。
    bus_kind = env("SERVICE_BUS", "redis" if storage_kind == "redis" else "memory")

    if storage_kind == "redis":
        storage = RedisStorage(host=redis_host, port=redis_port)
    else:
        db_url = env(
            "SERVICE_DB_URL",
            "sqlite+aiosqlite:///./data/service.db",
        )
        if db_url.startswith("sqlite"):
            db_path = db_url.split("///", 1)[-1]
            resolve_path(db_path).parent.mkdir(parents=True, exist_ok=True)
        storage = AsyncSQLAlchemyStorage(url=db_url, create_tables=True)

    if bus_kind == "redis":
        message_bus = RedisMessageBus(host=redis_host, port=redis_port)
    else:
        message_bus = InMemoryMessageBus()

    # 【装配锁定 · 勿改】隔离粒度必须是 PER_SESSION：框架管理器默认
    # PER_AGENT（同 agent 新会话继承旧工作区文件＝个人桌面语义＋数据
    # 串扰面，排查清单 W2）；PER_USER 更是直接跟人走。改回任何一档
    # 都要先过注册表/清单评审。local 与 k8s 两个分支都显式传。
    workspace_kind = env("SERVICE_WORKSPACE", "local")
    if workspace_kind == "k8s":
        # K8s 工作区（生产目标形态，内网集群）：每工作区一个 Pod＋
        # PVC（PVC 名由 workspace_id 确定性派生，任一服务副本都能
        # 按名找回——服务多实例的前提）；工具在 Pod 内执行。服务
        # 进程需集群内身份（in-cluster）或 kubeconfig，且其账号须
        # 有在工作区命名空间管理 Pod/PVC 的 RBAC。依赖
        # kubernetes-asyncio 已入 pyproject 锁定。本分支尚未在真实
        # 集群实测，接入时须按 DEPLOY.md 的 K8s 一节验收。
        from agentscope.app.workspace_manager import (
            K8sWorkspaceManager,
        )

        pull_secrets = [
            s.strip()
            for s in env("SERVICE_K8S_IMAGE_PULL_SECRETS", "").split(",")
            if s.strip()
        ]
        workspace_manager = K8sWorkspaceManager(
            isolation=IsolationPolicy.PER_SESSION,
            namespace=env("SERVICE_K8S_NAMESPACE", "agentscope"),
            image=env("SERVICE_K8S_IMAGE", "python:3.11-slim"),
            image_pull_secrets=pull_secrets or None,
            storage_class=env("SERVICE_K8S_STORAGE_CLASS") or None,
            storage_size=env("SERVICE_K8S_STORAGE_SIZE", "1Gi"),
        )
    else:
        workspace_dir = resolve_path(
            env("SERVICE_WORKSPACE_DIR", "./data/workspaces"),
        )
        workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_manager = LocalWorkspaceManager(
            basedir=str(workspace_dir),
            isolation=IsolationPolicy.PER_SESSION,
        )

    # 【装配锁定 · 勿改】跨租户姿态：默认全拒是底线。框架不传策略时
    # 默认 DenyAll；本装配传 TenantPrefixPolicy 是唯一受控例外——只在
    # 同一租户前缀内放行 KB/凭证的 READ，跨租户仍然全拒，AGENT 默认
    # 不放行。不许换成放行面更大的策略，不许给跨 user EDIT。
    # 【装配锁定 · 勿改】长期记忆 middleware 默认不装配：create_app
    # 的 extra_agent_middlewares 有意不传。框架的 AgenticMemory 是
    # 「个人记忆册」模型（排查清单 M1/M2），项目级知识走 KB/注册表
    # 通道；将来真要启用，须在网关接入评审里显式定粒度与检索成本。
    app = create_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_credentials=[MockCredential],
        enable_channel_worker=False,
        enable_scheduler=False,
        resource_access_policy=TenantPrefixPolicy(),
        title="AgentScope (agent-base assembly)",
    )
    app.add_middleware(TrustedHeaderIdentityMiddleware)
    return app


app = build_service_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SERVICE_PORT", "8100")),
    )
