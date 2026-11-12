# -*- coding: utf-8 -*-
"""项目级共享策略：按 user_id 的租户前缀放行同租户资源（只读）。

背景（排查清单 P1）：框架自带资源访问策略的扩展点
（ResourceAccessPolicyBase），create_app 不传时默认 DenyAll——跨 user
一律不可见。我们的 user_id 形如 {tenant}:{end_user}（装配中间件拼接），
租户信息已经编码在键里，本策略据此实现项目级共享：

- viewer 的 user_id 含冒号（租户键）时，对同一 {tenant}: 前缀的**其他**
  user 名下的 KNOWLEDGE_BASE 与 CREDENTIAL 资源返回 READ 引用；
- AGENT 默认不放行（SHARE_AGENTS 开关常量，置 True 才开）；
- 跨租户一律不放行；viewer 无冒号（非租户键）返回空；
- 只给 READ：基类 can_edit 默认实现只认 EDIT 引用，故跨 user 只读、
  不可改，编辑仍归 owner。

同租户其他 user 的发现：存储层是 owner-scoped 的，没有跨 user 枚举
接口。本策略两条路：
1. 确定性 owner：租户系统户 {tenant}:__system__（组织级凭证/SOP 的
   持有者）恒纳入候选——只用公开的 owner-scoped list 接口即可枚举；
2. 其余 end user：当存储是 AsyncSQLAlchemyStorage 时，用其 session
   工厂对目标表做一次只读 SELECT DISTINCT user_id（substr 前缀匹配）；
   非 SQL 存储（Redis）无此发现通道，降级为仅系统户，属已声明边界。

性能口径：list_accessible 会被每次 list/get/resolve 调用，含一次
DISTINCT 查询 + 每 owner 一次 list。骨架量级可接受；生产放大时应给
租户成员关系建独立索引，而不是在这里扫表。
"""
from sqlalchemy import text

from agentscope.app.access import (
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourcePermission,
    ResourceRef,
)

# AGENT 跨 user 放行开关：默认关。开启前先想清楚——放行后同租户他人
# 的 Agent 可被 resolve 并用于建会话，治理面比 KB/凭证大得多。
SHARE_AGENTS = False

SYSTEM_END_USER = "__system__"

_TABLE_BY_KIND = {
    ResourceKind.CREDENTIAL: "credentials",
    ResourceKind.KNOWLEDGE_BASE: "knowledge_bases",
    ResourceKind.AGENT: "agents",
}


def tenant_of(user_id: str) -> str | None:
    """{tenant}:{end_user} -> tenant；无冒号（非租户键）-> None。"""
    tenant, sep, _ = user_id.partition(":")
    return tenant if sep else None


def system_user_id(tenant: str) -> str:
    return f"{tenant}:{SYSTEM_END_USER}"


class TenantPrefixPolicy(ResourceAccessPolicyBase):
    """同租户前缀只读共享策略。"""

    async def list_accessible(
        self,
        viewer_id: str,
        kind: ResourceKind,
        storage,
    ) -> list[ResourceRef]:
        tenant = tenant_of(viewer_id)
        if tenant is None:
            return []
        if kind is ResourceKind.AGENT and not SHARE_AGENTS:
            return []
        if kind not in _TABLE_BY_KIND:
            return []
        refs: list[ResourceRef] = []
        for owner in await self._candidate_owners(storage, tenant, kind):
            if owner == viewer_id:
                continue
            for record in await self._list_owned(storage, owner, kind):
                refs.append(
                    ResourceRef(
                        kind=kind,
                        owner_id=owner,
                        resource_id=record.id,
                        permission=ResourcePermission.READ,
                    ),
                )
        return refs

    async def _candidate_owners(
        self,
        storage,
        tenant: str,
        kind: ResourceKind,
    ) -> set[str]:
        owners = {system_user_id(tenant)}
        factory = getattr(storage, "_session_factory", None)
        table = _TABLE_BY_KIND[kind]
        if factory is None:
            return owners
        prefix = f"{tenant}:"
        try:
            async with factory() as session:
                rows = await session.execute(
                    text(
                        f"SELECT DISTINCT user_id FROM {table} "
                        "WHERE substr(user_id, 1, :n) = :prefix",
                    ),
                    {"n": len(prefix), "prefix": prefix},
                )
                owners.update(row[0] for row in rows)
        except Exception:
            # 发现通道失败时降级为仅系统户，不许因此放大或阻断访问路径。
            pass
        return owners

    @staticmethod
    async def _list_owned(storage, owner: str, kind: ResourceKind) -> list:
        if kind is ResourceKind.CREDENTIAL:
            return await storage.list_credentials(owner)
        if kind is ResourceKind.KNOWLEDGE_BASE:
            return await storage.list_knowledge_bases(owner)
        return await storage.list_agents(owner)
