# -*- coding: utf-8 -*-
"""Adapter 层：把网关契约 v1 映射到 AgentScope Agent Service 原生 HTTP API。

隔离原则（设计文档 1.2 目标 3）：与框架的接触全部收敛在本文件——
且只以 HTTP 客户端形式出现（不再 import agentscope）。服务是独立进程
（service/，默认 :8100），会话与消息历史归服务存储管；网关本地只保留
契约事件回放、run 记录与待确认簿记（进程重启丢，属已声明边界）。

原生打法（agentscope 2.0.11 源码 + 2026-11-07 探针实测）：
- 身份：每请求带可信头 X-Tenant-Id / X-End-User-Id，服务侧中间件拼
  user_id={tenant}:{end_user}；凭证/Agent/会话都是该 user 名下的资源，
  故供给（ensure）与会话操作都按 (tenant, end_user) 维度进行。
- 供给：GET /credential/、GET /agent/ 按 type+name / name 幂等查找，
  缺了才 POST 创建；注册表 YAML 仍是唯一事实来源。
- 会话：POST /sessions/ {agent_id, chat_model_config} → session_id
  透传为契约 session_id（32 位 hex，网关不自造、不维护映射表）；
  网关重启后凭 (tenant, end_user) 逐个注册 Agent 探测
  GET /sessions/{id}/status 即可重建句柄、继续旧会话。
- 对话：先订阅 GET /sessions/{id}/stream（SSE，先回放日志再直播），
  排空回放后 POST /chat/ 触发，再按 reply_id 过滤消费事件流，
  翻译成契约事件与同步回复语义；REPLY_END / REQUIRE_USER_CONFIRM
  为一轮的终点。
- 确认：把 UserConfirmResultEvent JSON 作为 /chat/ 的 input 提交，
  工具调用原文（事件流里的 ToolCallBlock JSON）整段回带；
  网关重启丢 pending 时，可从服务会话消息尾部的 asking 态
  tool_call 块重建。
- 计量：事件流上的 MODEL_CALL_END 逐调用带 input/output tokens，
  在此拦截后只调用 gateway/usage_store.record_call（与调用方式无关）。
- 权限分级（注册表三级 ALLOW/ASK/DENY，2026-11-07 由 L0–L3 收敛）
  的原生表达：原生创建 Agent/会话的 API 没有权限规则字段；框架权限
  引擎在 DEFAULT 模式下的原生语义是「只读调用快速通道放行、其余无
  规则默认 ASK」——只读性来自 MCP 工具注解 readOnlyHint（见
  mcp_demo/server.py）。故 ALLOW（只读）经注解获得放行等价、ASK
  （写工具）不标注自然落到默认 ASK；DENY 由注册表校验直接拒绝登记
  使用（validate_registry），根本进不了运行时。旧四级里的 L1（轻写
  直行）对写工具在原生 API 下本就无预置放行表达、且注册表从无 L1
  工具，收敛时直接取消（见 NOTES.md 第 2 节）。
- 组织级凭证（第 3 项）：模型凭证不再建在终端用户名下，而是建在
  租户系统户 {tenant}:__system__ 名下、同租户共用一份；终端用户的
  会话经服务侧 TenantPrefixPolicy（同租户只读放行）解析该凭证。
  Agent 仍按 (tenant, end_user) 建在终端用户名下。
- 多模型（2026-11-08 补齐，口径见 NOTES 同日条）：模型计划按
  Agent 在注册表声明的条目逐个组装（primary/fallback/vision 各自
  查 models.yaml、各自读自己登记的 env 名），不再有"全局一个
  OPENAI 端点所有 Agent 共用"的捷径。建会话时把 fallback 作为
  服务原生 fallback_chat_model_config 一并下发（主模型故障时框架
  自动顶上）。切换策略：消息带图（或本会话历史已含图）而当前模型
  无 vision 能力时，轮次开始前经服务原生 PATCH /sessions/{id}
  换到 Agent 声明的 vision 模型，历史归服务存储、切换不断上下文；
  能力只升不降——会话一旦需要过 vision 就粘住，不再降回去；
  Agent 未声明 vision 模型又收到图片时，明确以 MODEL_NO_VISION
  报错，不静默吞图。切换留 model.switched 契约事件。
- 按租户动态注入（第 4 项）：建会话后按会话租户查注册表，把
  scope=default 的 MCP（全量配置直加 POST /workspace/mcp，禁用
  from-library）与技能（POST /workspace/skill/upload 本体直传）注入
  该会话工作区；optional 只登记不注入。注入结果记在句柄的
  injection_report（网关内部簿记，不进契约响应）。
- SOP 分发（第 6 项）：租户首次供给时，把注册表 SOP 定义以系统户
  身份落库（POST /sop/，同名 PATCH 更新）。框架 ResourceKind 不含
  SOP，终端用户不可见——只是单点登记/更新的分发落库（见 NOTES）。
- 工作区产物回收（第 5 项）：collect_artifacts 走服务原生
  /workspace/directories + /workspace/files 把会话工作区文件收回
  data/artifacts/{tenant}/{session_id}/（只经服务 API，不读本地盘）。
"""
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agent_registry.loader import REGISTRY_DIR, load_agent, load_agents, load_models
from common.config import env, resolve_path
from gateway.registry import (
    mcp_servers_for_tenant,
    skill_files,
    skills_for_tenant,
    sops_for_tenant,
)
from gateway.state import build_state
from gateway.ledger_store import LedgerStore
from gateway.usage_store import UsageStore


class SessionBusyError(Exception):
    """同会话排队超时：已有轮次在跑且等待超过上限（契约 409）。"""

SERVICE_URL = env("SERVICE_URL", "http://127.0.0.1:8100")
# 租户系统户的 end_user 段：组织级凭证与 SOP 定义挂在这个键名下，
# 与 service/access_policy.py 的 SYSTEM_END_USER 必须保持一致。
SYSTEM_END_USER = "__system__"
RUN_TIMEOUT_SECS = float(env("GATEWAY_RUN_TIMEOUT_SECS", "120"))


class ModelCapabilityError(Exception):
    """当前 Agent 的模型名单满足不了本轮输入所需能力（如带图但无
    vision 模型）。网关 HTTP 层映射为 400 MODEL_NO_VISION。"""
_DRAIN_SILENCE_SECS = 0.6
_DRAIN_CAP_SECS = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PendingConfirmation:
    cid: str
    reply_id: str
    tool_calls: list  # list[dict]：原生 ToolCallBlock 的 JSON 原样保存
    created_at: str = field(default_factory=_now)

    def public_view(self) -> dict:
        return {
            "confirmation_id": self.cid,
            "tool_calls": [
                {
                    "id": tc.get("id"),
                    "name": tc.get("name"),
                    "input": tc.get("input"),
                }
                for tc in self.tool_calls
            ],
            "created_at": self.created_at,
        }


@dataclass
class AdapterSession:
    """网关侧会话句柄：只含路由与簿记信息，对话历史在服务存储里。"""

    session_id: str  # == 服务侧会话 id（透传）
    tenant: str
    end_user: str
    agent_config: dict
    service_agent_id: str
    model_name: str
    # 网关状态后端（gateway/state.py）：events/runs/限流的共享存储，
    # emit 与 run 记录都经它——多副本时跨实例可见。构造时由 adapter
    # 注入（必填，None 仅为 dataclass 默认占位）。
    state: object = None
    pending: dict[str, PendingConfirmation] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    archived: bool = False
    # 当前会话绑定的注册表模型条目名与 vision 下限标记：切换成功后
    # 更新；vision_floor 一旦置位（本会话出现过图片）不再清除。
    current_model_entry: str = ""
    vision_floor: bool = False
    # 建会话时的按租户注入结果（MCP/技能各条的成败），仅网关内部簿记。
    injection_report: dict | None = None
    kb_report: dict | None = None
    # 异步模式的后台 run 任务（执行中的任务属本进程，不迁移）。
    tasks: set = field(default_factory=set)

    def emit(self, run_id: str, type_: str, payload: dict) -> dict:
        event = {
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "session_id": self.session_id,
            "run_id": run_id,
            "type": type_,
            "ts": _now(),
            "payload": payload,
        }
        self.state.append_event(event)
        return event


@dataclass
class _Provisioned:
    service_agent_id: str
    chat_model_config: dict
    fallback_chat_model_config: dict | None = None


class AgentScopeAdapter:
    def __init__(self) -> None:
        # 状态后端先建：会话句柄构造时要注入它。
        self.state = build_state()
        self.sessions: dict[str, AdapterSession] = {}
        self._provisioned: dict[tuple[str, str, str], _Provisioned] = {}
        # 租户系统户凭证缓存：(tenant, cred_type, cred_name) -> id
        self._system_credentials: dict[tuple[str, str, str], str] = {}
        self._sops_ensured: set[str] = set()
        self._sop_errors: dict[str, list[str]] = {}
        self.models = load_models()
        # 模型口径与改造前一致：.env 齐备 OPENAI_BASE_URL+OPENAI_MODEL
        # 时走 OpenAI 兼容端点，否则 mock。
        if env("OPENAI_BASE_URL") and env("OPENAI_MODEL"):
            self.model_name = env("OPENAI_MODEL")
        else:
            self.model_name = "mock-1"
        self.client = httpx.AsyncClient(
            base_url=SERVICE_URL,
            timeout=httpx.Timeout(30.0),
            trust_env=False,
        )
        # usage 逐调用流水落库（gateway/usage_store.py）；不可用时
        # usage() 自动回退到句柄内存聚合（见 usage 方法）。
        self.usage_store = UsageStore()
        # 确认台账＋审计账（gateway/ledger_store.py）：确认卡与
        # 决定是原生消息不保存的契约层事实，必须持久化在网关侧
        # 的库里，不能放可淘汰的状态后端。
        self.ledger = LedgerStore()
        # 同会话串行锁：一会话同时只跑一轮，新消息排队等而不是
        # 吃原生 409（进程内锁；多副本由原生单轮约束兜底）。
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- 基础
    @staticmethod
    def _headers(tenant: str, end_user: str) -> dict:
        return {"X-Tenant-Id": tenant, "X-End-User-Id": end_user}

    def _model_plan_for(self, entry_name: str) -> dict | None:
        """按注册表条目名组装模型计划（不含 credential_id）。

        返回 {entry_name, cred_type, cred_name, cred_data, model}；
        条目是 openai_compatible 但其登记的 env（base_url/model）
        不齐时返回 None（该条目在当前部署不可用）；条目未登记
        直接抛错——注册表校验应已拦下，运行时再兜底一道。
        密钥只从 env 读，绝不进注册表文件。
        """
        entry = self.models.get(entry_name)
        if entry is None:
            raise ValueError(f"模型条目未登记：{entry_name}")
        protocol = entry.get("protocol")
        if protocol == "mock":
            return {
                "entry_name": entry_name,
                "cred_type": "mock_credential",
                "cred_name": "mock",
                "cred_data": {"type": "mock_credential", "name": "mock"},
                "model": entry.get("model") or "mock-1",
            }
        if protocol == "openai_compatible":
            base_url = env(entry.get("base_url_env") or "OPENAI_BASE_URL")
            model = env(entry.get("model_env") or "OPENAI_MODEL")
            if not base_url or not model:
                return None
            cred_data = {
                "type": "openai_credential",
                "name": f"openai-{entry_name}",
                "base_url": base_url,
            }
            cred_data["api" + "_key"] = env(
                entry.get("api_key_env") or "OPENAI_API_KEY", "no-key",
            )
            return {
                "entry_name": entry_name,
                "cred_type": "openai_credential",
                "cred_name": f"openai-{entry_name}",
                "cred_data": cred_data,
                "model": model,
            }
        raise ValueError(f"模型条目 {entry_name} 的 protocol 不支持：{protocol}")

    def _primary_plan(self, agent_config: dict) -> dict:
        """Agent 主模型的计划；主模型不可用是配置错误，大声失败，
        不静默换到别的模型（生产口径：静默兜底即 demo 做派）。"""
        primary = agent_config["model"]["primary"]
        plan = self._model_plan_for(primary)
        if plan is None:
            raise RuntimeError(
                f"Agent {agent_config['id']} 的主模型 {primary} 在当前部署不可用"
                "（其登记的环境变量未配齐）",
            )
        return plan

    def _model_plan(self, agent_config: dict) -> dict:
        """兼容旧调用点：等价于主模型计划。"""
        return self._primary_plan(agent_config)

    async def _ensure_credential(
        self,
        tenant: str,
        end_user: str,
        plan: dict,
    ) -> str:
        """幂等确保 (tenant, end_user) 名下有 plan 描述的凭证，返回 id。"""
        headers = self._headers(tenant, end_user)
        r = await self.client.get("/credential/", headers=headers)
        r.raise_for_status()
        for cred in r.json().get("credentials", []):
            data = cred.get("data") or {}
            if data.get("type") == plan["cred_type"] and data.get("name") == plan["cred_name"]:
                return cred.get("id") or data.get("id")
        r = await self.client.post(
            "/credential/", headers=headers, json={"data": plan["cred_data"]},
        )
        r.raise_for_status()
        return r.json()["credential_id"]

    async def _ensure_system_credential(
        self,
        tenant: str,
        agent_config: dict,
    ) -> str:
        """组织级凭证：建在租户系统户 {tenant}:__system__ 名下、同租户
        共用一份（第 3 项）。终端用户经 TenantPrefixPolicy 只读解析。"""
        return await self._ensure_system_credential_for_plan(
            tenant, self._primary_plan(agent_config),
        )

    async def _ensure_system_credential_for_plan(
        self,
        tenant: str,
        plan: dict,
    ) -> str:
        """按模型计划确保系统户凭证（同租户同计划共用一份）。"""
        key = (tenant, plan["cred_type"], plan["cred_name"])
        if key not in self._system_credentials:
            self._system_credentials[key] = await self._ensure_credential(
                tenant, SYSTEM_END_USER, plan,
            )
        return self._system_credentials[key]

    async def _session_model_configs(
        self,
        tenant: str,
        agent_config: dict,
    ) -> tuple[dict, dict | None]:
        """组装建会话用的 (主模型配置, 备用模型配置|None)。

        备用模型：Agent 未声明、与主模型同条目、或该条目在当前
        部署不可用（env 未配齐）时为 None——备用缺位只记账不拦
        建会话；主模型不可用已在 _primary_plan 大声失败。
        """
        model_cfg = agent_config.get("model") or {}
        primary = model_cfg["primary"]
        primary_plan = self._primary_plan(agent_config)
        primary_cred = await self._ensure_system_credential_for_plan(
            tenant, primary_plan,
        )
        main_config = self._chat_model_config(
            agent_config, primary_cred, plan=primary_plan,
        )
        fallback_config = None
        fallback = model_cfg.get("fallback")
        if fallback and fallback != primary:
            fallback_plan = self._model_plan_for(fallback)
            if fallback_plan is None:
                print(
                    f"[adapter] 备用模型 {fallback} 在当前部署不可用"
                    f"（env 未配齐），本次会话不下发 fallback"
                    f" agent={agent_config['id']}",
                    file=sys.stderr,
                )
            else:
                fallback_cred = await self._ensure_system_credential_for_plan(
                    tenant, fallback_plan,
                )
                fallback_config = self._chat_model_config(
                    agent_config, fallback_cred, plan=fallback_plan,
                )
        return main_config, fallback_config

    async def _ensure_agent(
        self,
        tenant: str,
        end_user: str,
        agent_config: dict,
    ) -> str:
        """幂等确保 (tenant, end_user) 名下有注册表 Agent，返回服务侧 id。"""
        headers = self._headers(tenant, end_user)
        prompt_path = REGISTRY_DIR / agent_config["system_prompt_file"]
        system_prompt = prompt_path.read_text(encoding="utf-8")
        r = await self.client.get("/agent/", headers=headers)
        r.raise_for_status()
        for agent in r.json().get("agents", []):
            data = agent.get("data") or {}
            if data.get("name") == agent_config["id"]:
                service_agent_id = agent.get("id") or data.get("id")
                if data.get("system_prompt") != system_prompt:
                    rp = await self.client.patch(
                        f"/agent/{service_agent_id}",
                        headers=headers,
                        json={"system_prompt": system_prompt},
                    )
                    rp.raise_for_status()
                return service_agent_id
        r = await self.client.post(
            "/agent/",
            headers=headers,
            json={"name": agent_config["id"], "system_prompt": system_prompt},
        )
        r.raise_for_status()
        return r.json()["agent_id"]

    def _chat_model_config(
        self,
        agent_config: dict,
        credential_id: str,
        plan: dict | None = None,
    ) -> dict:
        plan = plan or self._primary_plan(agent_config)
        parameters: dict = {}
        if agent_config.get("model", {}).get("temperature") is not None:
            parameters["temperature"] = agent_config["model"]["temperature"]
        if agent_config.get("model", {}).get("max_tokens") is not None:
            parameters["max_tokens"] = agent_config["model"]["max_tokens"]
        return {
            "type": plan["cred_type"],
            "credential_id": credential_id,
            "model": plan["model"],
            "parameters": parameters,
        }

    async def _ensure_provisioned(
        self,
        tenant: str,
        end_user: str,
        agent_config: dict,
    ) -> _Provisioned:
        """幂等供给：组织凭证（系统户）+ Agent（终端用户名下）。

        改造前凭证建在终端用户名下；现在终端用户键下不建个人凭证，
        会话的 chat_model_config 引用系统户凭证 id，运行时由服务侧
        策略放行解析（第 3 项）。Agent 归属不变。
        """
        key = (tenant, end_user, agent_config["id"])
        if key in self._provisioned:
            return self._provisioned[key]
        main_config, fallback_config = await self._session_model_configs(
            tenant, agent_config,
        )
        service_agent_id = await self._ensure_agent(tenant, end_user, agent_config)
        prov = _Provisioned(
            service_agent_id=service_agent_id,
            chat_model_config=main_config,
            fallback_chat_model_config=fallback_config,
        )
        self._provisioned[key] = prov
        await self._ensure_tenant_sops(tenant)
        return prov

    async def _system_provisioned(
        self,
        tenant: str,
        agent_config: dict,
    ) -> _Provisioned:
        """系统户名下的供给（凭证 + Agent 都在系统户），供 SOP 落库用。"""
        key = (tenant, SYSTEM_END_USER, agent_config["id"])
        if key not in self._provisioned:
            main_config, fallback_config = await self._session_model_configs(
                tenant, agent_config,
            )
            service_agent_id = await self._ensure_agent(
                tenant, SYSTEM_END_USER, agent_config,
            )
            self._provisioned[key] = _Provisioned(
                service_agent_id=service_agent_id,
                chat_model_config=main_config,
                fallback_chat_model_config=fallback_config,
            )
        return self._provisioned[key]

    async def _ensure_tenant_sops(self, tenant: str) -> None:
        """SOP 分发最小版（第 6 项）：注册表 SOP 以系统户身份落库。

        同名已有则 PATCH 更新（单点更新源）；失败只记账不抛错——
        SOP 落库是分发面，不能拖垮会话主链。注意框架 ResourceKind
        不含 SOP，终端用户对系统户 SOP 不可见、不能直接发起 run，
        这只是登记/落库，运行分发缺口见 NOTES。
        """
        if tenant in self._sops_ensured:
            return
        self._sops_ensured.add(tenant)
        headers = self._headers(tenant, SYSTEM_END_USER)
        for sop in sops_for_tenant(tenant):
            try:
                agent_config = load_agent(sop["agent"])
                if agent_config is None:
                    raise ValueError(f"SOP 引用未登记 Agent：{sop['agent']}")
                prov = await self._system_provisioned(tenant, agent_config)
                session_key = sop.get("session_key", "main")
                data = {
                    "name": sop["name"],
                    "description": sop.get("description", ""),
                    "steps": [
                        {
                            "version": "v1",
                            "subject": step["subject"],
                            "description": step["description"],
                            "executor": {
                                "agent_id": prov.service_agent_id,
                                "session_key": session_key,
                            },
                        }
                        for step in sop["steps"]
                    ],
                    "session_settings": {
                        session_key: {
                            "chat_model_config": prov.chat_model_config,
                            "fallback_chat_model_config": (
                                prov.fallback_chat_model_config
                            ),
                        },
                    },
                }
                r = await self.client.get("/sop/", headers=headers)
                r.raise_for_status()
                existing_id = None
                for record in r.json().get("sops", []):
                    if (record.get("data") or {}).get("name") == sop["name"]:
                        existing_id = record.get("id")
                        break
                if existing_id is None:
                    r = await self.client.post(
                        "/sop/", headers=headers, json={"data": data},
                    )
                else:
                    r = await self.client.patch(
                        f"/sop/{existing_id}",
                        headers=headers,
                        json={"data": data},
                    )
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - 分发面失败不进主链
                self._sop_errors.setdefault(tenant, []).append(
                    f"{sop.get('name')}: {type(exc).__name__}: {exc}",
                )
                print(
                    f"[adapter] SOP 落库失败 tenant={tenant} "
                    f"sop={sop.get('name')}: {exc}",
                    file=sys.stderr,
                )

    # ------------------------------------------------------------- 会话
    async def create_session(
        self,
        tenant: str,
        end_user: str,
        agent_config: dict,
    ) -> AdapterSession:
        prov = await self._ensure_provisioned(tenant, end_user, agent_config)
        body = {
            "agent_id": prov.service_agent_id,
            "chat_model_config": prov.chat_model_config,
        }
        if prov.fallback_chat_model_config is not None:
            body["fallback_chat_model_config"] = prov.fallback_chat_model_config
        # 知识库接线：Agent 在注册表以名字声明 knowledge_bases
        #（资产归租户系统户，经 TenantPrefixPolicy 同租户只读
        # 共享对终端用户可见），建会话时从该用户可见列表按名
        # 解析为 id，以原生 knowledge_config 下发——原生建会话
        # 会逐个校验可见性。声明了却解析不到的名字记 kb_report
        #（句柄簿记，不进契约响应），不静默吞。
        kb_ids, kb_report = await self._resolve_knowledge_bases(
            tenant, end_user, agent_config,
        )
        if kb_ids:
            body["knowledge_config"] = {"knowledge_base_ids": kb_ids}
        r = await self.client.post(
            "/sessions/",
            headers=self._headers(tenant, end_user),
            json=body,
        )
        r.raise_for_status()
        session = AdapterSession(
            session_id=r.json()["session_id"],
            tenant=tenant,
            end_user=end_user,
            agent_config=agent_config,
            service_agent_id=prov.service_agent_id,
            model_name=prov.chat_model_config["model"],
            state=self.state,
            current_model_entry=agent_config["model"]["primary"],
        )
        self.sessions[session.session_id] = session
        session.kb_report = kb_report
        self.ledger.append_audit(
            tenant=tenant, end_user=end_user, action="session.created",
            session_id=session.session_id,
            detail={"agent_id": agent_config["id"]},
        )
        await self._inject_tenant_assets(session)
        return session

    async def _resolve_knowledge_bases(
        self, tenant: str, end_user: str, agent_config: dict,
    ) -> tuple[list[str], dict]:
        names = agent_config.get("knowledge_bases") or []
        if not names:
            return [], {}
        report: dict = {}
        visible: dict[str, str] = {}
        try:
            r = await self.client.get(
                "/knowledge_bases/",
                headers=self._headers(tenant, end_user),
                params={"page_size": 128},
            )
            if r.status_code == 200:
                for kb in r.json().get("knowledge_bases") or []:
                    if kb.get("name") and kb.get("id"):
                        visible[kb["name"]] = kb["id"]
        except httpx.HTTPError:
            pass
        ids: list[str] = []
        for name in names:
            kid = visible.get(name)
            report[name] = "resolved" if kid else "missing"
            if kid:
                ids.append(kid)
        return ids, report

    # ------------------------------------------------- 按租户动态注入
    async def _inject_tenant_assets(self, session: AdapterSession) -> None:
        """建会话后按租户注入 default 的 MCP 与技能（第 4 项）。

        MCP：注册表全量配置直加 POST /workspace/mcp（禁用
        from-library）；工作区已有同名（重复注入、或会话中已手动
        加过）则跳过，不重复加。技能：本体从 skills_store
        经 POST /workspace/skill/upload 直传（不回写用户库）。
        scope=optional 的一律不注入。逐项记账、失败不抛错（会话已
        建成，注入是增强面），结果落在 session.injection_report。
        """
        report: dict = {"mcp": {}, "skills": {}}
        headers = self._headers(session.tenant, session.end_user)
        params = {
            "agent_id": session.service_agent_id,
            "session_id": session.session_id,
        }

        present_mcps: set[str] = set()
        try:
            r = await self.client.get(
                "/workspace/mcp", headers=headers, params=params,
            )
            if r.status_code == 200:
                present_mcps = {c.get("name") for c in r.json()}
        except httpx.HTTPError:
            pass

        agent_mcp = {
            entry.get("name"): entry
            for entry in (session.agent_config.get("tools") or {}).get(
                "mcp_servers",
            )
            or []
        }
        for server in mcp_servers_for_tenant(session.tenant, scope="default"):
            name = server["name"]
            if name in present_mcps:
                report["mcp"][name] = "already_present"
                continue
            client_config: dict = {
                "name": name,
                "is_stateful": False,
                "mcp_config": {
                    "type": "http_mcp",
                    "url": env(server.get("url_env", "")),
                },
            }
            allow = (agent_mcp.get(name) or {}).get("allow") or []
            if allow:
                client_config["enable_tools"] = list(allow)
            try:
                r = await self.client.post(
                    "/workspace/mcp",
                    headers=headers,
                    params=params,
                    json=client_config,
                )
                report["mcp"][name] = (
                    "injected" if r.status_code == 201 else f"http_{r.status_code}"
                )
            except httpx.HTTPError as exc:
                report["mcp"][name] = f"error:{type(exc).__name__}"

        for skill in skills_for_tenant(session.tenant, scope="default"):
            name = skill["name"]
            try:
                report["skills"][name] = await self._upload_skill(
                    session, skill, headers, params,
                )
            except Exception as exc:  # noqa: BLE001 - 逐项记账不抛错
                report["skills"][name] = f"error:{type(exc).__name__}"
        session.injection_report = report

    async def _upload_skill(
        self,
        session: AdapterSession,
        skill: dict,
        headers: dict,
        params: dict,
    ) -> str:
        """把 skills_store 里的技能文件夹经 upload 通道装进会话工作区。

        manifest 的 path 是相对所选文件夹的父目录（即
        "<dir>/<相对路径>"），files 按 manifest 顺序逐个发送——与
        服务侧 UploadManifest 的约定一致（探针见 _router/_workspace）。
        """
        skill_path: Path = skill["skill_path"]
        root_name = skill_path.name
        files = skill_files(skill_path)
        entries = []
        parts = []
        for f in files:
            rel = f.relative_to(skill_path).as_posix()
            data = f.read_bytes()
            entries.append({"path": f"{root_name}/{rel}", "size": len(data)})
            parts.append(("files", (rel, data)))
        r = await self.client.post(
            "/workspace/skill/upload",
            headers=headers,
            params=params,
            data={"manifest": json.dumps({"entries": entries})},
            files=parts,
        )
        return "injected" if r.status_code == 201 else f"http_{r.status_code}"

    # ------------------------------------------------- 工作区产物回收
    async def collect_artifacts(self, session: AdapterSession) -> dict:
        """把会话工作区文件回收到 data/artifacts/{tenant}/{session}/。

        只经服务原生 API（GET /workspace/directories 逐层列、
        GET /workspace/files 逐个下），不读服务本地盘——工作区在
        别的部署形态（容器工作区）下本地路径根本不存在。返回
        {"dir": 落盘目录, "files": [相对路径...]}。
        """
        headers = self._headers(session.tenant, session.end_user)
        params = {
            "agent_id": session.service_agent_id,
            "session_id": session.session_id,
        }
        base = (
            resolve_path(env("ARTIFACTS_DIR", "./data/artifacts"))
            / session.tenant
            / session.session_id
        )
        collected: list[str] = []

        async def _walk(rel_dir: str) -> None:
            r = await self.client.get(
                "/workspace/directories",
                headers=headers,
                params={**params, "path": rel_dir},
            )
            r.raise_for_status()
            for entry in r.json().get("entries", []):
                rel = f"{rel_dir}/{entry['name']}" if rel_dir else entry["name"]
                if entry.get("is_dir"):
                    await _walk(rel)
                    continue
                rf = await self.client.get(
                    "/workspace/files",
                    headers=headers,
                    params={**params, "path": rel},
                )
                rf.raise_for_status()
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(rf.content)
                collected.append(rel)

        await _walk("")
        return {"dir": str(base), "files": sorted(collected)}

    async def resolve_session(
        self,
        session_id: str,
        tenant: str,
        end_user: str,
    ) -> AdapterSession | None:
        """按 id 找句柄；内存没有（网关重启后）就去服务侧探测重建。

        探测方式：逐个该租户的注册 Agent 先 ensure（该 user 名下），
        再 GET /sessions/{id}/status——200 即命中。服务侧资源按
        user_id={tenant}:{end_user} 隔离，租户或用户对不上自然 404。
        """
        handle = self.sessions.get(session_id)
        if handle is not None:
            return handle
        for agent_config in load_agents():
            if agent_config["tenant"] != tenant:
                continue
            try:
                prov = await self._ensure_provisioned(tenant, end_user, agent_config)
                r = await self.client.get(
                    f"/sessions/{session_id}/status",
                    headers=self._headers(tenant, end_user),
                    params={"agent_id": prov.service_agent_id},
                )
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                handle = AdapterSession(
                    session_id=session_id,
                    tenant=tenant,
                    end_user=end_user,
                    agent_config=agent_config,
                    service_agent_id=prov.service_agent_id,
                    model_name=prov.chat_model_config["model"],
                    state=self.state,
                    current_model_entry=agent_config["model"]["primary"],
                )
                await self._restore_session_model_state(handle)
                self.sessions[session_id] = handle
                return handle
        return None

    async def _restore_session_model_state(
        self,
        session: AdapterSession,
    ) -> None:
        """重建句柄时从服务侧恢复模型状态（服务是事实来源）：

        - 当前绑定模型：查会话列表里的会话记录 config，按模型名
          反查注册表条目，恢复 current_model_entry/model_name；
        - vision 下限：扫会话消息，用户消息含 image 块即置位。
        任一查询失败都保守保持默认值（主模型条目、下限未置位），
        后续带图轮次仍会触发切换，只是防降级的下限可能迟置。
        """
        headers = self._headers(session.tenant, session.end_user)
        try:
            r = await self.client.get(
                "/sessions/",
                headers=headers,
                params={"agent_id": session.service_agent_id},
            )
            r.raise_for_status()
            for view in r.json().get("sessions", []):
                record = view.get("session") or view.get("record") or view
                if record.get("id") != session.session_id:
                    continue
                cfg = (record.get("config") or {}).get("chat_model_config") or {}
                bound_model = cfg.get("model")
                if bound_model:
                    session.model_name = bound_model
                    for name in self.models:
                        plan = self._model_plan_for(name)
                        if plan is not None and plan["model"] == bound_model:
                            session.current_model_entry = name
                            break
                break
            r = await self.client.get(
                f"/sessions/{session.session_id}/messages",
                headers=headers,
                params={"agent_id": session.service_agent_id, "limit": 100},
            )
            r.raise_for_status()
            for msg in r.json().get("messages", []):
                if msg.get("role") != "user":
                    continue
                if any(
                    isinstance(b, dict)
                    and b.get("type") == "data"
                    and str((b.get("source") or {}).get("media_type", ""))
                    .startswith("image/")
                    for b in msg.get("content") or []
                ):
                    session.vision_floor = True
                    break
        except httpx.HTTPError:
            pass

    async def _switch_session_model(
        self,
        session: AdapterSession,
        entry_name: str,
        run_id: str,
        reason: str,
    ) -> None:
        """经服务原生 PATCH 把会话的 chat_model_config 换到目标条目。

        调用时机在轮次开始前（无 run 持有会话，不会撞 409）；成功
        后同步句柄簿记并发 model.switched 契约事件。失败抛
        httpx.HTTPError，由 send_message 按 run 失败收口。
        """
        plan = self._model_plan_for(entry_name)
        if plan is None:
            raise ModelCapabilityError(
                f"目标模型 {entry_name} 在当前部署不可用（env 未配齐）",
            )
        credential_id = await self._ensure_system_credential_for_plan(
            session.tenant, plan,
        )
        config = self._chat_model_config(
            session.agent_config, credential_id, plan=plan,
        )
        r = await self.client.patch(
            f"/sessions/{session.session_id}",
            headers=self._headers(session.tenant, session.end_user),
            params={"agent_id": session.service_agent_id},
            json={"chat_model_config": config},
        )
        r.raise_for_status()
        previous = session.current_model_entry
        session.current_model_entry = entry_name
        session.model_name = plan["model"]
        session.emit(
            run_id,
            "model.switched",
            {
                "from_entry": previous,
                "to_entry": entry_name,
                "model": plan["model"],
                "reason": reason,
            },
        )

    def _desired_model_entry(
        self,
        session: AdapterSession,
        has_image: bool,
    ) -> str:
        """按切换策略算本轮应使用的注册表条目名。

        需要 vision（本轮带图或本会话已置下限）时：当前条目有
        vision 能力就留用；否则切 Agent 声明的 vision 条目；
        都没有 → ModelCapabilityError。不需要时留用当前条目
        （只升不降：已升到 vision 条目后，纯文本轮次也不降回）。
        """
        current = session.current_model_entry
        if not (has_image or session.vision_floor):
            return current
        current_caps = (self.models.get(current) or {}).get("capabilities") or []
        if "vision" in current_caps:
            return current
        vision_entry = (session.agent_config.get("model") or {}).get("vision")
        if vision_entry:
            return vision_entry
        raise ModelCapabilityError(
            f"Agent {session.agent_config['id']} 的模型名单里没有可用的"
            " vision 模型，无法处理图片输入",
        )

    def get_session(self, session_id: str) -> AdapterSession | None:
        return self.sessions.get(session_id)

    async def delete_session(self, session: AdapterSession) -> None:
        try:
            await self.client.delete(
                f"/sessions/{session.session_id}",
                headers=self._headers(session.tenant, session.end_user),
                params={"agent_id": session.service_agent_id},
            )
        finally:
            session.archived = True
            self.ledger.append_audit(
                tenant=session.tenant, end_user=session.end_user,
                action="session.archived",
                session_id=session.session_id,
                detail={"agent_id": session.agent_config["id"]},
            )

    # ------------------------------------------------------------- 事件流
    async def _consume_run(
        self,
        session: AdapterSession,
        run_id: str,
        trigger,
        known_reply_id: str | None = None,
    ) -> dict:
        """订阅事件流 → 排空回放 → 触发 → 按 reply_id 过滤消费一轮。

        trigger 是无参 async 回调（POST /chat/ 在排空后才发），
        保证回放与本轮事件不混。返回 {"status", "reply"}。
        """
        reply_text = ""
        status = "completed"
        target_reply_id = known_reply_id
        headers = self._headers(session.tenant, session.end_user)
        queue: asyncio.Queue = asyncio.Queue()

        async with self.client.stream(
            "GET",
            f"/sessions/{session.session_id}/stream",
            headers=headers,
            params={"agent_id": session.service_agent_id},
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
        ) as resp:
            resp.raise_for_status()

            async def _reader() -> None:
                try:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            try:
                                queue.put_nowait(json.loads(line[5:].strip()))
                            except (ValueError, TypeError):
                                pass
                except (httpx.HTTPError, asyncio.CancelledError):
                    pass

            reader = asyncio.create_task(_reader())
            try:
                # 排空回放：回放是订阅建立时一次性涌入的历史事件，
                # 触发前没有任何新事件来源，静默 0.6s 即认为排空。
                drain_deadline = asyncio.get_running_loop().time() + _DRAIN_CAP_SECS
                while asyncio.get_running_loop().time() < drain_deadline:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=_DRAIN_SILENCE_SECS)
                    except asyncio.TimeoutError:
                        break

                await trigger()

                deadline = asyncio.get_running_loop().time() + RUN_TIMEOUT_SECS
                locked = False
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        status = "failed"
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        status = "failed"
                        break
                    ev_type = ev.get("type")
                    ev_reply = ev.get("reply_id")
                    if ev_type == "REPLY_START":
                        if target_reply_id is None or (
                            ev_reply != target_reply_id and not locked
                        ):
                            target_reply_id = ev_reply
                        continue
                    if (
                        target_reply_id is not None
                        and ev_reply is not None
                        and ev_reply != target_reply_id
                    ):
                        continue
                    if ev_reply == target_reply_id and ev_type != "REPLY_START":
                        locked = True

                    if ev_type == "TEXT_BLOCK_DELTA":
                        delta = ev.get("delta") or ""
                        reply_text += delta
                        session.emit(run_id, "message.delta", {"text": delta})
                    elif ev_type == "TOOL_CALL_START":
                        session.emit(
                            run_id,
                            "tool.calling",
                            {
                                "tool": ev.get("tool_call_name"),
                                "tool_call_id": ev.get("tool_call_id"),
                            },
                        )
                    elif ev_type == "TOOL_RESULT_END":
                        session.emit(
                            run_id,
                            "tool.result",
                            {
                                "tool_call_id": ev.get("tool_call_id"),
                                "state": str(ev.get("state")),
                            },
                        )
                    elif ev_type == "MODEL_CALL_END":
                        in_tok = ev.get("input_tokens") or 0
                        out_tok = ev.get("output_tokens") or 0
                        session.input_tokens += in_tok
                        session.output_tokens += out_tok
                        # 逐模型调用落一行流水（设计 9 章计量）；拦截点
                        # 在事件流消费处，模块与调用方式无关（见
                        # usage_store）。写失败只降级不抛错。
                        self.usage_store.record_call(
                            tenant=session.tenant,
                            end_user=session.end_user,
                            agent_id=session.agent_config["id"],
                            session_id=session.session_id,
                            run_id=run_id,
                            model=session.model_name,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                        )
                    elif ev_type == "REQUIRE_USER_CONFIRM":
                        cid = f"cfm-{uuid.uuid4().hex[:12]}"
                        pending = PendingConfirmation(
                            cid=cid,
                            reply_id=ev.get("reply_id") or target_reply_id or "",
                            tool_calls=list(ev.get("tool_calls") or []),
                        )
                        session.pending[cid] = pending
                        impact = "ASK 级工具执行前需人工确认（设计 7.3）"
                        session.emit(
                            run_id,
                            "confirmation.required",
                            {
                                **pending.public_view(),
                                "impact": impact,
                            },
                        )
                        # 确认台账落库：卡与之后的决定是原生消息
                        # 不保存的契约事实，靠它拼历史、做审计。
                        self.ledger.put_confirmation(
                            confirmation_id=cid,
                            session_id=session.session_id,
                            tenant=session.tenant,
                            end_user=session.end_user,
                            agent_id=session.agent_config["id"],
                            run_id=run_id,
                            tool_calls=pending.public_view()["tool_calls"],
                            impact=impact,
                        )
                        status = "awaiting_confirmation"
                        break
                    elif ev_type == "REPLY_END":
                        if ev.get("error"):
                            status = "failed"
                        break
                    # 其余原生事件（HINT_BLOCK/CUSTOM/块起止等）不翻译。
            finally:
                reader.cancel()

        if status == "completed" and not reply_text:
            reply_text = await self._fetch_final_reply(session)
        return {"status": status, "reply": reply_text}

    async def _fetch_final_reply(self, session: AdapterSession) -> str:
        """兜底：增量没攒到文本时，从服务会话消息取尾部 assistant 文本。"""
        try:
            r = await self.client.get(
                f"/sessions/{session.session_id}/messages",
                headers=self._headers(session.tenant, session.end_user),
                params={"agent_id": session.service_agent_id, "limit": 10},
            )
            r.raise_for_status()
            for msg in reversed(r.json().get("messages", [])):
                if msg.get("role") != "assistant":
                    continue
                texts = [
                    str(b.get("text", ""))
                    for b in msg.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if texts:
                    return "".join(texts)
        except httpx.HTTPError:
            pass
        return ""

    def _open_run(
        self,
        session: AdapterSession,
        run_id: str,
        *,
        trigger: str = "message",
        request_text: str | None = None,
        has_image: bool = False,
        confirmation_id: str | None = None,
        decision: str | None = None,
    ) -> dict:
        """建 run 记录并登记（status=running）：在途即可经契约查询。

        记录写状态后端（多副本共享）；_started_epoch 为内部字段，
        供 get_run 的在途超时对账用，对外经 _public_run 剥除。
        trigger/request_text/confirmation_id/decision 是展示历史
        投影的原料：用户原文与确认决定只在开轮这一刻齐全，错过
        就再也拼不回来，故随 run 一起落账。
        """
        run = {
            "run_id": run_id,
            "session_id": session.session_id,
            "tenant": session.tenant,
            "agent_id": session.agent_config["id"],
            "agent_version": session.agent_config["version"],
            "model": session.model_name,
            "status": "running",
            "reply": "",
            "ts": _now(),
            "trigger": trigger,
            "request_text": request_text,
            "has_image": has_image,
            "confirmation_id": confirmation_id,
            "decision": decision,
            "_started_epoch": time.time(),
        }
        self.state.put_run(run)
        return run

    def _finish_run(self, session: AdapterSession, run: dict, result: dict) -> dict:
        run["status"] = result["status"]
        run["reply"] = result["reply"]
        run["ts"] = _now()
        self.state.update_run(
            run["run_id"],
            status=run["status"], reply=run["reply"], ts=run["ts"],
        )
        if result["status"] == "completed":
            session.emit(run["run_id"], "run.completed", {"reply": result["reply"]})
        elif result["status"] == "failed":
            session.emit(run["run_id"], "run.failed", {})
        return self._public_run(run)

    @staticmethod
    def _public_run(run: dict) -> dict:
        return {k: v for k, v in run.items() if not k.startswith("_")}

    def get_run(self, run_id: str) -> dict | None:
        """查 run 记录（状态后端）。在途超时对账：执行副本死亡时该轮
        会永远停在 running——超过单轮超时＋余量仍为 running 的，
        在此置 failed 并回写（业务侧据此重试）。"""
        run = self.state.get_run(run_id)
        if run is None:
            return None
        if run.get("status") == "running":
            timeout = float(env("GATEWAY_RUN_TIMEOUT_SECS", "120"))
            started = run.get("_started_epoch") or 0
            if started and time.time() - started > timeout + 30:
                run = self.state.update_run(
                    run_id, status="failed", ts=_now(),
                ) or run
        return self._public_run(run)

    # ------------------------------------------------- 历史与会话列表
    # ------------------------------------------------- 历史与会话列表
    async def _fetch_all_messages(
        self, session: AdapterSession, cap: int = 2000,
    ) -> list[dict]:
        """取原生会话消息（持久化正文）：游标向前翻页合并为时间序。

        原生口径：不带 before 取最新一页，has_more 表示还有更早
        的页，before 传该页最早一条消息 id 继续向前翻。
        """
        pages: list[list[dict]] = []
        before: str | None = None
        total = 0
        while total < cap:
            params: dict = {
                "agent_id": session.service_agent_id, "limit": 200,
            }
            if before:
                params["before"] = before
            r = await self.client.get(
                f"/sessions/{session.session_id}/messages",
                headers=self._headers(session.tenant, session.end_user),
                params=params,
            )
            r.raise_for_status()
            body = r.json()
            msgs = body.get("messages") or []
            if not msgs:
                break
            pages.append(msgs)
            total += len(msgs)
            if not body.get("has_more"):
                break
            before = msgs[0].get("id")
            if not before:
                break
        out: list[dict] = []
        for page in reversed(pages):
            out.extend(page)
        return out

    @staticmethod
    def _ts_key(ts: str | None) -> float:
        if not ts:
            return 0.0
        try:
            return datetime.fromisoformat(
                ts.replace("Z", "+00:00"),
            ).timestamp()
        except ValueError:
            return 0.0

    async def session_history(self, session: AdapterSession) -> dict:
        """展示历史：正文走基座原生持久化消息，确认卡走网关台账。

        分工（2026-11-09 源码复核后定）：用户/助手正文是基座
        存储里的事实，网关只做翻译；确认卡与决定是原生消息
        不保存的契约事实（决定落定后原生只剩 finished 的工具
        调用），由确认台账（落库，非状态后端）供数。两者按
        时间戳合并排序——网关状态后端（Redis/内存）全丢，
        本投影照样完整。
        """
        sid = session.session_id
        messages = await self._fetch_all_messages(session)
        # run_id 关联（尽力而为）：状态日志里回复文本相同的
        # 最近一条 run，供页面与实时事件按 run 去重；对不上
        # 就只给消息 id，页面按序渲染不受影响。
        reply_to_run: dict[str, str] = {}
        for run in self.state.list_session_runs(sid):
            if run.get("reply"):
                reply_to_run[run["reply"]] = run["run_id"]
        tagged: list[tuple[float, int, dict]] = []
        seq = 0
        for msg in messages:
            blocks = [
                b for b in msg.get("content") or []
                if isinstance(b, dict)
            ]
            ts = msg.get("created_at")
            # 排序键取 finished_at：一条助手消息横跨"等确认"
            # 全程（created 在确认前、finished 在决定后），按
            # created 排会把回复排到确认卡前面，显示顺序就反了。
            sort_ts = msg.get("finished_at") or ts
            if msg.get("role") == "user":
                text = "".join(
                    str(b.get("text", ""))
                    for b in blocks if b.get("type") == "text"
                )
                has_image = any(b.get("type") == "data" for b in blocks)
                if text or has_image:
                    tagged.append((self._ts_key(sort_ts), seq, {
                        "kind": "user", "text": text,
                        "image": has_image, "ts": ts,
                    }))
                    seq += 1
            elif msg.get("role") == "assistant":
                text = "".join(
                    str(b.get("text", ""))
                    for b in blocks if b.get("type") == "text"
                )
                if text:
                    item = {
                        "kind": "assistant", "text": text,
                        "id": msg.get("id"), "ts": ts,
                    }
                    run_id = reply_to_run.get(text)
                    if run_id:
                        item["run_id"] = run_id
                    tagged.append((self._ts_key(sort_ts), seq, item))
                    seq += 1
        for conf in self.ledger.list_confirmations(sid):
            status = conf.get("status") or "pending"
            item = {
                "kind": "confirmation",
                "confirmation_id": conf["confirmation_id"],
                "tool_calls": conf.get("tool_calls") or [],
                "impact": conf.get("impact") or "",
                # 台账 status 三态：pending / decided（附 decision
                # 字段）——对外收敛为页面口径：未决 pending，
                # 已决取整批决定（逐调用明细在 decisions 字段）。
                "status": (
                    status if status == "pending"
                    else (conf.get("decision") or status)
                ),
                "ts": conf.get("created_at"),
            }
            if conf.get("decisions"):
                item["decisions"] = conf["decisions"]
            tagged.append(
                (self._ts_key(conf.get("created_at")), seq, item),
            )
            seq += 1
        tagged.sort(key=lambda t: (t[0], t[1]))
        return {"session_id": sid, "items": [t[2] for t in tagged]}

    async def list_sessions(
        self, tenant: str, end_user: str, agent_id: str | None = None,
    ) -> dict:
        """会话列表：逐个本租户 Agent 向基座原生 GET /sessions/
        查该用户名下会话，翻译为契约形状。不再自存元数据账。
        已归档（原生已删除）的会话不出现在列表里。
        """
        out: list[dict] = []
        for cfg in load_agents():
            if cfg["tenant"] != tenant:
                continue
            if agent_id is not None and cfg["id"] != agent_id:
                continue
            prov = await self._ensure_provisioned(tenant, end_user, cfg)
            r = await self.client.get(
                "/sessions/",
                headers=self._headers(tenant, end_user),
                params={"agent_id": prov.service_agent_id},
            )
            r.raise_for_status()
            for view in r.json().get("sessions") or []:
                rec = view.get("session") or {}
                config = rec.get("config") or {}
                model_cfg = config.get("chat_model_config") or {}
                out.append({
                    "session_id": rec.get("id"),
                    "agent_id": cfg["id"],
                    "agent_version": cfg["version"],
                    "name": config.get("name"),
                    "model": model_cfg.get("model"),
                    "created_at": rec.get("created_at"),
                    "last_active_at": rec.get("updated_at"),
                    "status": view.get("status"),
                    "is_running": bool(view.get("is_running")),
                })
        out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        return {"sessions": out}

    async def _execute_run(
        self,
        session: AdapterSession,
        run: dict,
        trigger,
        known_reply_id: str | None = None,
    ) -> dict:
        """消费一轮并收口 run 记录（同步/异步两模式共用）。

        同会话串行：先拿本会话的锁再触发——前一轮在跑时新
        消息排队等待，而不是把原生 409 透给业务侧；等待超过
        GATEWAY_SESSION_QUEUE_TIMEOUT_SECS（默认 180）抛
        SessionBusyError，路由层译为契约 409 SESSION_BUSY。
        锁在 _execute_run 层持有，异步模式下由后台任务持有到
        轮次收口，口径一致。注意：锁是进程内的，网关多副本
        时跨副本并发仍由基座原生单轮约束兜底。
        """
        lock = self._session_locks.setdefault(
            session.session_id, asyncio.Lock(),
        )
        timeout = float(env("GATEWAY_SESSION_QUEUE_TIMEOUT_SECS", "180"))
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise SessionBusyError(
                f"session {session.session_id} busy beyond {timeout}s",
            ) from None
        try:
            try:
                result = await self._consume_run(
                    session, run["run_id"], trigger,
                    known_reply_id=known_reply_id,
                )
            except httpx.HTTPError:
                # 触发被拒或服务不可达：按 run 失败收口，不让
                # HTTP 层裸抛 500（契约事件里留 run.failed 痕迹）。
                result = {"status": "failed", "reply": ""}
            return self._finish_run(session, run, result)
        finally:
            lock.release()
            if not lock.locked() and len(self._session_locks) > 512:
                # 防无界增长：空闲时顺手清掉本锁（尽力而为）。
                self._session_locks.pop(session.session_id, None)

    def _launch_run(
        self,
        session: AdapterSession,
        run: dict,
        trigger,
        known_reply_id: str | None = None,
    ) -> dict:
        """异步模式：后台任务跑这一轮，立即返回 run 句柄。"""
        task = asyncio.create_task(
            self._execute_run(session, run, trigger, known_reply_id),
        )
        session.tasks.add(task)
        task.add_done_callback(session.tasks.discard)
        return {
            "run_id": run["run_id"],
            "session_id": session.session_id,
            "status": "running",
        }

    async def _trigger_chat(self, session: AdapterSession, payload) -> None:
        r = await self.client.post(
            "/chat/",
            headers=self._headers(session.tenant, session.end_user),
            json={
                "agent_id": session.service_agent_id,
                "session_id": session.session_id,
                "input": payload,
            },
        )
        r.raise_for_status()

    # ------------------------------------------------------------- 运行
    async def send_message(
        self,
        session: AdapterSession,
        text: str,
        mode: str = "sync",
        images: list[dict] | None = None,
    ) -> dict:
        """发一轮用户消息。images 是框架 data 块的 source 列表
        （{type: url|base64, ...}），由契约层校验后转换传入。

        模型策略先行：先算本轮目标条目——满足不了能力要求时抛
        ModelCapabilityError（在开 run 之前，业务侧收到明确的
        400 而不是一个失败的 run）；需要切换时在开轮前 PATCH
        换模型，切换失败按 run 失败收口并留事件痕迹。
        """
        has_image = bool(images)
        desired = self._desired_model_entry(session, has_image)
        if has_image:
            session.vision_floor = True
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        run = self._open_run(
            session, run_id,
            trigger="message", request_text=text, has_image=has_image,
        )
        session.emit(run_id, "run.started", {"agent_id": session.agent_config["id"]})
        if desired != session.current_model_entry:
            try:
                await self._switch_session_model(
                    session, desired, run_id, reason="vision_required",
                )
            except httpx.HTTPError:
                return self._finish_run(
                    session, run, {"status": "failed", "reply": ""},
                )
            run["model"] = session.model_name
            self.state.update_run(run_id, model=session.model_name)
        content: list[dict] = [{"type": "text", "text": text}]
        for source in images or []:
            # 框架的二进制内容块叫 data（DataBlock），没有 image 类型，
            # 写错会在 /chat/ 被 422 挡下（2026-11-08 冒烟实证）。
            content.append({"type": "data", "source": source})
        msg = {
            "name": session.end_user,
            "role": "user",
            "content": content,
        }

        async def _trigger() -> None:
            await self._trigger_chat(session, msg)

        if mode == "async":
            return self._launch_run(session, run, _trigger)
        return await self._execute_run(session, run, _trigger)

    async def _rebuild_pending(
        self,
        session: AdapterSession,
        cid: str,
    ) -> PendingConfirmation | None:
        """网关重启丢 pending 时，从服务会话消息尾部重建待确认状态。

        服务侧 parked 的会话，其持久化上下文尾部 assistant 消息里
        带着 state=asking 的 tool_call 块，消息 id 即 reply_id。
        """
        try:
            r = await self.client.get(
                f"/sessions/{session.session_id}/messages",
                headers=self._headers(session.tenant, session.end_user),
                params={"agent_id": session.service_agent_id, "limit": 10},
            )
            r.raise_for_status()
        except httpx.HTTPError:
            return None
        for msg in reversed(r.json().get("messages", [])):
            if msg.get("role") != "assistant":
                continue
            asking = [
                b
                for b in msg.get("content", [])
                if isinstance(b, dict)
                and b.get("type") == "tool_call"
                and b.get("state") in ("asking", "submitted")
            ]
            if asking:
                pending = PendingConfirmation(
                    cid=cid,
                    reply_id=msg.get("id") or "",
                    tool_calls=asking,
                )
                session.pending[cid] = pending
                return pending
            return None  # 尾部 assistant 消息没有待确认调用，即无 parked
        return None

    @staticmethod
    def _build_confirm_results(
        pending: PendingConfirmation,
        decision: str | None,
        edited_input: str | None,
        decisions: list[dict] | None,
    ) -> tuple[list[dict], list[dict], str]:
        """把契约决定翻译成原生 ConfirmResult 列表（纯函数）。

        两种契约形状：
        - 整批决定：decision(+edited_input) 套给全部工具调用；
        - 逐调用决定：decisions 数组逐条表决（按 tool_call_id
          匹配，无 id 且条数与调用数相等时按序匹配），条目可
          附带 rules 透传原生 ConfirmResult.rules；未被覆盖的
          调用回落整批决定，再无则默认拒绝（保守）。
        返回 (confirm_results, 归一化逐条记录, 整批决定口径)。
        """
        by_id: dict[str, dict] = {}
        positional: list[dict] = []
        for entry in decisions or []:
            if entry.get("tool_call_id"):
                by_id[entry["tool_call_id"]] = entry
            else:
                positional.append(entry)
        use_positional = len(positional) == len(pending.tool_calls)
        results: list[dict] = []
        normalized: list[dict] = []
        for idx, tc in enumerate(pending.tool_calls):
            entry = by_id.get(tc.get("id") or "")
            if entry is None and use_positional:
                entry = positional[idx]
            if entry is not None:
                d = entry["decision"]
                e_in = entry.get("edited_input")
                rules = entry.get("rules")
            else:
                d = decision or "rejected"
                e_in = edited_input
                rules = None
            tc_payload = dict(tc)
            if e_in and d == "edited":
                tc_payload["input"] = e_in
            result = {
                "confirmed": d in ("approved", "edited"),
                "tool_call": tc_payload,
            }
            if rules is not None:
                result["rules"] = rules
            results.append(result)
            record = {"tool_call_id": tc.get("id"), "decision": d}
            if e_in and d == "edited":
                record["edited_input"] = e_in
            if rules is not None:
                record["rules"] = rules
            normalized.append(record)
        values = {r["decision"] for r in normalized}
        batch = values.pop() if len(values) == 1 else "mixed"
        return results, normalized, batch

    async def confirm(
        self,
        session: AdapterSession,
        cid: str,
        decision: str | None = None,
        edited_input: str | None = None,
        mode: str = "sync",
        decisions: list[dict] | None = None,
    ) -> dict | None:
        pending = session.pending.get(cid)
        if pending is None:
            pending = await self._rebuild_pending(session, cid)
        if pending is None:
            return None
        session.pending.pop(cid, None)

        confirm_results, normalized, batch = self._build_confirm_results(
            pending, decision, edited_input, decisions,
        )
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        run = self._open_run(
            session, run_id,
            trigger="confirmation", confirmation_id=cid, decision=batch,
        )
        session.emit(
            run_id, "run.started",
            {"resume_of": cid, "decision": batch},
        )
        # 决定自被网关受理即成事实：台账回写＋审计落账（执行
        # 结果如何不影响"谁在何时做了什么决定"这条记录）。
        self.ledger.mark_decided(
            cid, status="decided", decision=batch,
            decisions=normalized, decided_by=session.end_user,
        )
        self.ledger.append_audit(
            tenant=session.tenant, end_user=session.end_user,
            action="confirmation.decided",
            session_id=session.session_id, ref_id=cid,
            detail={"decision": batch, "decisions": normalized},
        )
        event = {
            "type": "USER_CONFIRM_RESULT",
            "reply_id": pending.reply_id,
            "confirm_results": confirm_results,
        }

        async def _trigger() -> None:
            await self._trigger_chat(session, event)

        if mode == "async":
            return self._launch_run(
                session, run, _trigger, known_reply_id=pending.reply_id,
            )
        return await self._execute_run(
            session, run, _trigger, known_reply_id=pending.reply_id,
        )

    # ------------------------------------------------------------- 计量
    def usage(self, tenant: str) -> dict:
        # 首选流水表聚合：跨进程重启累计（口径见 usage_store 模块说明，
        # runs = 产生过模型调用的 run 数）。落库降级时回退到下面的
        # 句柄内存聚合，行为与落库前一致（注：句柄只覆盖本进程
        # 解析过的会话，重启后回退口径会偏小，属已声明边界）。
        if self.usage_store.healthy:
            return {"tenant": tenant, **self.usage_store.aggregate(tenant)}
        runs = 0
        input_tokens = 0
        output_tokens = 0
        by_agent: dict[str, int] = {}
        for session in self.sessions.values():
            if session.tenant != tenant:
                continue
            session_runs = self.state.list_session_runs(session.session_id)
            runs += len(session_runs)
            input_tokens += session.input_tokens
            output_tokens += session.output_tokens
            for run in session_runs:
                by_agent[run["agent_id"]] = by_agent.get(run["agent_id"], 0) + 1
        return {
            "tenant": tenant,
            "runs": runs,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "token_source": "model_reported" if (input_tokens or output_tokens) else "none",
            "by_agent": by_agent,
        }
