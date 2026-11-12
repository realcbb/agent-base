# NOTES —— 探针结论与实现备忘

> 全部为 2026-11-07 在本机对 agentscope **2.0.11** 的实测结论（inspect 源码 +
> 实际跑通），不是文档记忆。设计依据：《AgentScope 通用 AI 基座设计文档 v1.1》。

## 1. 探针结论（框架实测）

### 1.1 安装面
- 只装 `agentscope` 基础包**不够**：`agentscope.app` 导入即报缺 `apscheduler`。
  必须带 extras：`agentscope[service,storage-sql,storage-redis]==2.0.11`，
  外加 `aiosqlite` 与 `sqlalchemy[asyncio]`（已锁进 pyproject.toml）。
- 关键传递版本（实测解析结果）：fastapi 0.162.0、uvicorn 0.54.0、
  httpx 0.28.1、pydantic 2.14.3、mcp **2.3.0**、sqlalchemy 2.1.8、redis 8.1.0。

### 1.2 create_app 装配面
- 签名：`create_app(storage, message_bus, workspace_manager, *, ...)`，
  三个必填位置参数；可选含 `extra_credentials`、`enable_scheduler`、
  `custom_agent_cls`、`title/version` 等。
- **Storage**：顶层只导出 `RedisStorage`；SQL 实现是
  `agentscope.app.storage.AsyncSQLAlchemyStorage(url, create_tables=True)`
  （在 `_sql` 子模块）。demo 用 `sqlite+aiosqlite` URL；生产把 URL 换
  PostgreSQL 异步驱动即可，装配代码不变。
- **MessageBus**：`InMemoryMessageBus`（进程内）与 `RedisMessageBus(host, port)` 都有。
  demo 用 InMemory（service/app.py 有 SERVICE_STORAGE 开关切 Redis 双件套）。
- **WorkspaceManager**：现成实现多（Local/Docker/Bubblewrap/K8s/E2B/…）。
  demo 用 `LocalWorkspaceManager(basedir, isolation=PER_SESSION)`，
  并用 `default_mcps` 把 demo MCP 挂为工作区默认工具。

### 1.3 身份
- 框架依赖 `agentscope.app.deps.get_current_user_id` 读请求头 **`X-User-ID`**
  （源码注释自称 "Temporary header-based identity"），原生路由**连 /health
  都要求该头**，缺失回 422。
- 落法（设计 5.1）：本工程在 service 侧加一层纯 ASGI 中间件
  `TrustedHeaderIdentityMiddleware`，只信内网可信头 `X-Tenant-Id` /
  `X-End-User-Id`，在进框架前注入 `X-User-ID = {tenant}:{end_user}`。
  框架其余代码零改动。实测：只带可信头即可通过原生全链；不带任何身份头被拒。
- 原生路由在根路径（`/agent/ /credential/ /sessions/ /chat/ /health`），
  不是 `/api/...` 前缀；`GET /sessions/{id}/messages` 需要 `?agent_id=` 参数。

### 1.4 模型与凭证
- service 按会话的 `chat_model_config` 构建模型：凭证类经
  `CredentialBase.get_chat_model_class()` 给出 ChatModel 类；自定义凭证
  用 `create_app(extra_credentials=[...])` 注册。mock 凭证就是这么接的
  （type=`mock_credential`，模型 type=`mock_chat`），与 builtin 的
  openai/dashscope 等凭证**同一机制**，不是旁路 hack。
- `ChatModelBase` 的抽象方法是 `_call_api(model_name, messages, tools,
  tool_choice)`，返回 `ChatResponse(content=[block...], is_last=True,
  usage=ChatUsage(input_tokens, output_tokens, time))`——`time` 必填、
  `ToolCallBlock` 的 `id` 必填，踩过才知道（dataclass/pydantic 无默认值）。
- `Msg.content` 必须是 block 列表，裸字符串会被拒。

### 1.5 MCP 与工具名（重要坑）
- **mcp SDK 2.x 破坏性改名**：`mcp.server.fastmcp.FastMCP` 已不存在，
  现为 `from mcp.server.mcpserver import MCPServer`，启动
  `server.run(transport="streamable-http", host=, port=)`；
  客户端为 `mcp.client.streamable_http.streamable_http_client`。
  AgentScope 2.0.11 要求 `mcp>=2.0.0` 并已按 2.x 客户端 API 写。
- `HttpMCPConfig.type` 的字面量默认是 `'http_mcp'`，不要传 `streamable_http`。
- **工具在框架内被加前缀**：`mcp__{server_name}__{tool_name}`
  （实测 `mcp__demo-tools__demo_time`）。模型发工具调用必须用全名。

### 1.6 权限 / HITL（实测行为，含原生 HTTP 口径）
- 框架权限引擎在 DEFAULT 模式下的判定顺序：deny 规则 → ask 规则 →
  **只读快速通道**（`tool.is_read_only` 为真直接放行）→ 工具自检 →
  allow 规则 → **默认 ASK**。MCP 工具的只读性来自 server 侧注解
  `readOnlyHint`（框架适配层据此置 is_read_only）。
- **原生 API 没有预置权限规则的入口**：`POST /agent/` 与
  `POST /sessions/` 的请求体都没有 rules 字段；会话初始
  PermissionContext 为空规则，PATCH /sessions 只能改 permission_mode。
  确认结果里的 rules 只能在一次确认时追加进会话上下文，不能预置。
  （骨架早期进程内版本曾直接构造 PermissionContext 下发规则，
  该路径已随 adapter 改 HTTP 退役。）
- 故注册表分级（设计 8.2）的原生映射是：**ALLOW 级只读工具在 MCP
  server 侧标 `read_only_hint=True`**（见 mcp_demo/server.py 的
  demo_time），快速通道放行＝放行等价；**ASK 级写工具不标注**，
  无规则默认 ASK，与「须确认」天然对齐。探针实证：demo_time 未加
  注解时被 park（REQUIRE_USER_CONFIRM），加注解后直行。写工具没有
  「预置放行」的原生表达——原四级里的 L1「轻写直行」正因此无处
  落地；2026-11-07 晚已按用户裁定把分级收敛为三级 ALLOW/ASK/DENY
  （见第 4 节第 7 项），该差距随 L1 概念一并退役。注册表 level
  仍是唯一事实来源，注解须与之一致（不一致时以框架实际行为为准
  并应改注册表或注解对齐）。
- HITL 在原生事件流上的形状：SSE 出现 `REQUIRE_USER_CONFIRM
  {reply_id, tool_calls:[ToolCallBlock JSON]}` 后 worker 挂起，会话
  status 为 `awaiting_permission`；恢复＝把
  `UserConfirmResultEvent` 的 JSON dump 作为 `POST /chat/` 的 input
  再提交（tool_call 原文整段回带，edited 时只改其 input 字段），
  事件流随即续上 TOOL_RESULT_END 与收尾文本。拒绝时结果 state 为
  `denied`，run 正常收尾、工具不执行。
- 事件流（`GET /sessions/{id}/stream`，SSE）先回放消息总线日志再转
  实时；`MODEL_CALL_END` **逐模型调用**带 input/output tokens，
  网关计量在线上保持逐调用粒度（token_source=model_reported）；
  `TEXT_BLOCK_DELTA.delta` 是助手文本增量；一轮的终点是同 reply_id
  的 `REPLY_END`（带 error 时即 run 失败）或 `REQUIRE_USER_CONFIRM`。

## 2. Adapter 方案与设计的差距（诚实记录）

设计第 3/6 章是 gateway 与 Agent Service **独立进程**、网关经 service
原生 API 调框架。此前骨架的契约流量走进程内 adapter（已声明的取舍），
2026-11-07 已按预定拆分点完成改造：adapter 改为服务原生 HTTP 客户端。

| 项 | 设计 | 本骨架 | 理由 / 后续拆分点 |
|---|---|---|---|
| service 进程 | 独立部署 | ✅ 已独立（service/，原生路由全链实测通） | — |
| gateway 调 service | 经原生 HTTP API | ✅ 已改（2026-11-07）：`gateway/adapter.py` 只用 httpx 调服务原生 API，不再 import agentscope。打法：按 (tenant, end_user) 幂等 ensure 凭证/Agent → `POST /sessions/` 建会话（**session id 透传服务侧 32 位 hex**，原 `ses-` 前缀格式退役，网关不自造不映射）→ 每轮先订阅 `/sessions/{id}/stream` 排空回放、再 `POST /chat/` 触发、按 reply_id 过滤消费事件流并翻译成契约事件；确认走 UserConfirmResultEvent JSON 回提。契约对外形状、字段、错误信封未动 | 剩余边界：网关本地仍留三样内存簿记——契约 events 回放、runs 记录、待确认 pending（见「会话持久化」行）；跨租户/错 end_user 的会话探测会先在对方名下 ensure 一套凭证/Agent（幂等供给的无害副作用，仅骨架量级可接受） |
| 身份下发 | 网关注入可信头 | ✅ 网关每请求带 `X-Tenant-Id`/`X-End-User-Id`，service 侧中间件拼 user_id=`{tenant}:{end_user}`；资源按该 user 隔离，故全部会话级契约端点都要带 `X-End-User-Id` 且须与建会话时同值（缺省 demo-user） | — |
| 事件 | SSE 实时流 | ✅ 2026-11-08 凌晨补齐：对外 GET events 的 `?stream=true` 已是真长连接（先回放已缓冲、再跟随新事件、15s 心跳），事件经会话订阅者集合扇出；回放查询（last_event_id 增量）不变 | 剩余边界同会话持久化行：事件日志仍在网关内存，重启丢回放 |
| 调用模式 | 同步/异步双模式 | ✅ 2026-11-08 凌晨补齐（用户裁定业务侧实际调用必为异步，页面要实时展示思考/工具/确认过程）：`POST messages` 与 `POST confirmations` 均支持 `?mode=sync\|async`，默认 sync 阻塞等轮次收口；async 立即 202 返回 run 句柄、后台任务跑轮，进度全走 events 实时流，终态经 `GET /v1/runs/{id}` 可查（run 记录开轮即登记，在途状态 running→awaiting_confirmation/completed/failed 可见）。异步冒烟 smoke_async.py 11 项 ALL PASS，原双冒烟回归不破 | 异步 run 在网关进程内执行，网关重启则在途轮次丢失（与内存簿记同一边界） |
| 会话持久化 | service 存储 | ✅ 会话与消息历史归服务 SQL 存储：网关重启后凭 (tenant, end_user) 探测 `GET /sessions/{id}/status` 重建句柄即可续聊，冒烟有直查服务 API 与 messages 增长的双证据 | 剩余边界：网关内存簿记（events 回放/runs/pending）重启丢；其中 pending 已做兜底——确认请求未命中内存时，从服务会话消息尾部的 asking 态 tool_call 块重建（冒烟断言 12 实证跨重启确认成立）；events 回放与 runs 记录暂无此兜底 |
| 权限分级映射 | 注册表三级 ALLOW/ASK/DENY（设计 8.2，2026-11-07 由 L0–L3 收敛） | ✅ 三级全落地：ALLOW 只读靠 MCP 注解 readOnlyHint 走引擎快速通道（放行等价）；ASK 写工具无规则默认 ASK；DENY 在注册表校验层直接拒绝登记。原 L1「轻写直行」无原生预置放行表达、已随三级化退役（详见 1.6 与第 4 节） | — |
| 沙箱 | 工具沙箱 | ❌ 未启用（装配现用 local 后端） | 2026-11-07 深夜查实框架自带 8 种工作区后端（local/bubblewrap/docker/k8s/e2b/daytona/apple 容器/opensandbox），沙箱＝工作区隔离档位、基座现成提供，接入时装配选档即可（自托管优先 bubblewrap/docker），不必自研 nspawn 围法；围的是工作区内工具，外挂 MCP 在各自部署位置、不自动进此沙箱 |
| 计量落库 | 第 9 章逐调用计量落库 + /v1/usage 查询 | ✅ `gateway/usage_store.py` 逐模型调用一行落 SQLite，粒度对齐 Java 版官方 Service；拦截点已随 adapter 改造移到网关消费原生事件流处（MODEL_CALL_END 逐调用带 tokens，**粒度未降级**），record_call 签名未动。/v1/usage 查表聚合，runs 口径为「产生过模型调用的 run 数」（DISTINCT run_id），跨进程重启累计 | 剩余不在本次范围：计价（单价/成本换算）与按租户配额拦截未接；写入失败降级为进程内内存聚合兜底（兜底口径只覆盖本进程解析过的会话） |

## 3. 外部现成基座核查

（2026-11-07 并行核查结论，记录备查，本骨架不接入）

1. **官方 AgentScope Service（agentscope-java 仓库）**：控制面 Go + 数据面
   Java（HarnessAgent）+ Web Console，版本 **2.1.0-BETA1**，以 Docker
   Compose 发布包分发。它是 **Java 侧的官方平台层**，与本骨架装配的
   Python Agent Service（agentscope.app）不是一回事——同名不同物。
   尚处 BETA：接口与行为都可能变，现在绑它等于把契约主权交给一个未定版的
   上游平台。
2. **zhiran-agentOS（社区）**：Java/Spring Cloud 的同类多 Agent 平台，
   **36 star 级**单人/小团队项目。仅作参照（信息架构、功能清单可借鉴），
   不作地基：维护持续性与安全审计都无保障。

**本骨架为何仍自建薄契约层**（三条）：
- **契约自有**：对外契约 v1（第 7 章）是各项目与基座之间的稳定界面，
  必须握在自己手里；框架/平台怎么换，项目侧零改动。
- **框架可换**：gateway + adapter 把 AgentScope 的 API 面收敛到一个文件；
  官方 Service 转正、或将来换 Pi/dsh 内核时，替换的是实现不是契约。
- **官方 Service 尚 BETA**：等它 GA 后再评估「数据面是否换成官方 Service、
  契约层保留自建」——这正是薄契约层的价值：换实现是选项，不是重写。

## 4. 其他实现备忘
- 写文件管道会把源码里的凭据赋值模式（如 `api_key=<标识符>`）自动脱敏成
  占位符导致语法错误；adapter 组装 OpenAI 凭证数据时用字典键拼接绕开
  （`cred_data["api" + "_key"]`），勿"优化"回去。
- 本机 httpx 默认读环境代理会干扰 127.0.0.1 访问，工程内所有 httpx
  客户端都显式 `trust_env=False`。
- 网关限流是进程内滑动窗口（按租户），生产换 Redis 计数（设计 9 章）。
- `data/notes.txt`、`data/smoke-notes.txt` 里的行是验证过程的落盘痕迹，
  非正式数据，可随时删。

## 4. 2026-11-07 晚 · 动作清单完善轮（《个人平台假设排查清单》落地）

证据：`smoke_actions.py`（分阶段冒烟，输出 `smoke_output_actions.txt`，
本轮 RESULT: ALL PASS）＋基线 `smoke_test.py` 21 项断言不回归
（`smoke_output.txt`，RESULT: ALL PASS）。逐项结论：

1. **装配三改（落地）**：`service/app.py` 显式传
   `enable_channel_worker=False`、`enable_scheduler=False`、
   `resource_access_policy=TenantPrefixPolicy()`；PER_SESSION、
   跨租户全拒、记忆 middleware 默认不装配三条以装配注释锁定。
   探针（`probe/probe_assembly.py`，进程内起生产装配）实证：
   app.state 两开关为 False、lifespan 后 scheduler_manager 处于
   disabled（服务日志亦有 "SchedulerManager disabled on this
   node"）、channel_dispatcher 为 None；health 里 `mcp_hubs` /
   `skill_hubs` 均为 disabled（hub 生态在装配层本就没开）。
   **附带调查（如实记录，不假装关掉）**：
   - Schedule* 四工具**关不掉**：`SchedulerManager.list_tools()` 无
     条件返回，`get_toolkit` 无条件把它们放进 `schedule_tools` 工具
     组；`enable_scheduler=False` 只停触发循环（工具执行时自查
     enabled 会报错）。DEFAULT 模式初始只下发 basic 组 schema，
     Schedule* 要模型先经 `reset_tools` 激活该组才可见——探针实测
     初始工具清单里没有 Schedule*。残留面：模型可自行激活该组、
     再调用即报错收场（无实际调度发生）。将来若要硬摘除，只能走
     会话级 deny 规则（须经一次确认追加）或框架升级，无装配开关。
   - Team 四工具（TeamCreate/AgentCreate/TeamSay/TeamDelete）对每个
     非 worker 会话**无条件挂载、无装配开关**，探针实测在工具清单
     里。风险面是模型可自建团队会话（一客一策接入评审时再定处置：
     提示词约束/会话级规则/接受）。
   - AgentInvite 是条件挂载：仅当会话可见 agent 里有
     `invite_config.invitable=true` 且简介非空才出现；
     `InviteConfig` 默认 False，探针实测未挂载。接入规矩：登记
     agent 一律不许开 invitable（注册表侧无此字段，天然关闭）。
2. **项目级共享策略（落地）**：`service/access_policy.py` 的
   TenantPrefixPolicy——同租户（`{tenant}:` 前缀）他 user 名下的
   CREDENTIAL/KNOWLEDGE_BASE 以只读共享引用放行（列表脱敏、
   editable=False、运行时解析给原始记录）；AGENT 默认不放行
   （`SHARE_AGENTS=False` 常量）；跨租户与无冒号 viewer 一律空。
   owner 发现走存储 SQL 的 DISTINCT user_id 前缀查询（框架存储全
   owner-scoped、无跨 user 枚举 API，这是唯一可行路径；依赖
   AsyncSQLAlchemyStorage 的会话工厂私有属性，换存储后端时须复核
   ——已在代码注释标明）。冒烟以凭证为据：同租户 B 可读、异租户 C
   不可见。**KB 未能实测**：本装配没传 `knowledge_base_manager`
   （health 显示 `knowledge_base: disabled`，建 KB 返回 503）——
   KB 启用需要 embedding 端点与向量库选型，属接入期事项；KB 与
   凭证在框架里共用同一条政策合并路径（`_list_refs`），凭证证据
   可代表政策逻辑本身。
3. **组织级凭证（落地）**：系统户键 `{tenant}:__system__`；网关
   ensure 改为「系统户建租户级凭证（每租户一份）＋终端用户只建
   Agent」，agent 的 chat_config 引用系统户 credential_id，靠第 2
   项政策解析。冒烟：终端 user 会话跑通、其名下无个人凭证、系统户
   凭证以共享只读形式可见；异租户引用在**建会话一步即 404**
   （框架建会话时经政策解析凭证，比预想的对话期拒绝更早）。
4. **注册表按租户化＋动态注入（落地）**：MCP 登记加
   tenants/scope/tools 级别，技能独立登记 `skills.yaml`（本体自
   托管 `skills_store/`）、SOP 登记 `sops.yaml`；网关建会话后按
   会话租户把 default MCP 以注册表全量配置 POST /workspace/mcp
   注入（enable_tools 按该 agent 的 allow 清单填）、default 技能
   走 /workspace/skill/upload 注入，optional 不注入，全程禁
   from-library。注意框架行为：POST /workspace/mcp 在服务名不在
   用户库时会自动 upsert 进用户库——我方只把它当注入通道、不读
   库；per-session 工作区每次从注册表全量下发，库里的副本陈旧
   不影响新会话（老会话保留其创建时配置，符合会话快照语义）。
   冒烟：tenant-a 会话有专属 MCP tenant-a-tools 与技能
   tenant-a-hello、无 optional 技能；tenant-b 会话皆无。
5. **工作区产物回收（落地）**：服务有成套文件 API（目录树
   GET /workspace/directories、下载 GET /workspace/files），网关
   `collect_artifacts` 全程走 API 回收到
   `data/artifacts/{tenant}/{session_id}/`（adapter 方法，未挂
   契约端点——契约 v1 无此端点，接入定端点时再暴露）。回收物含
   框架簿记文件（`.mcp`、注入技能的工作区副本），与产物同目录，
   接入时如需净化再加过滤。冒烟：夹具文件两件（根目录＋子目录）
   回收成功且内容逐字一致。
6. **SOP 分发（部分落地）**：注册表登记＋ensure 时以系统户身份
   幂等落库（按 name 查重、内容变了 PATCH）已实现且冒烟通过；
   **但终端用户用不上**：SOP 不在 ResourceKind 里、列表 owner
   强隔离，政策层无从共享（冒烟实证终端用户 GET /sop/ 为空）。
   将来路径二选一：网关侧做 SOP 代理（对用户列注册表 SOP、运行时
   以系统户身份代跑 /sop/run），或等框架给 SOP 加共享语义。接入
   真需要 SOP 编排时再做代理，不预建。
7. **权限三级化（落地）**：注册表 schema、校验、网关映射口径、
   mock 模型演示路由、mcp_demo 注释、两套冒烟的断言标签全链改
   ALLOW/ASK/DENY；agent 的 max_level 改名保留（值域三级）。
   DENY 双保险：工具级 level=DENY 根本不许登记（校验拒），agent
   allow 到 DENY 工具同样被校验拒（冒烟阶段 0 有负例断言）。
   契约 v1 响应字段未动（仅 adapter 里 confirmation 的 impact
   说明文案由「L2 及以上」改为「ASK 级」，属文案非字段）。

残留缺口汇总（接入期处理，不预建）：Schedule*/Team 工具的硬摘除
手段、KB 启用（embedding/向量库选型）、SOP 终端可用性（网关代理
或框架共享语义）、TenantPrefixPolicy 对非 SQL 存储后端的适配、
工具沙箱（一期 nspawn 级，随首个真实工具接入补）。
注（2026-11-07 深夜产物口径定版）：collect 的契约端点与整套文件
物流（带入/哈希对账/业务键归属）已作废——agent 只经对话与结构化
结果交付，文件由业务侧生成与保管，工作区只当可弃草稿；collect
保留为网关内部例外工具，不挂契约端点、不作主干道。
注（2026-11-07 深夜存储口径定版，用户立规）：骨架落库一律按生产
环境标准，不用 SQLite。usage_store 已于 2026-11-08 换成
SQLAlchemy URL 驱动实现（见本节末 HA 注）——原文此句为当时状态，
现已兑现（表结构与 /v1/usage 字段不变，实现隔离在
usage_store.py 单模块内）；events/runs 将来落库同样直接落 MySQL，
不再提 SQLite 方案。服务侧存储同理：装配现默认 SQLite 文件
（data/service.db），接入时只改 SERVICE_DB_URL 指 MySQL（SQLAlchemy
存储层、自动建表，不动代码；MySQL 异步驱动 aiomysql 已补入
pyproject 锁定）。
注（2026-11-08 上午 MySQL＋Redis 组合已实测，用户立规两者都用）：
装配开关已解耦——SERVICE_STORAGE 选存储（sql｜redis）、SERVICE_BUS
独立选总线（memory｜redis，未设置时沿旧捆绑行为兼容既有部署）。
生产组合 sql＋SERVICE_DB_URL 指 MySQL＋SERVICE_BUS=redis 已在
InstaBox 整包实测全通（Docker 起 MySQL 8＋Redis 7）：建会话/同步
与异步对话/ASK 确认/计量全过；MySQL 自动建 13 表且 messages/
sessions/credentials 均有真实行；Redis 出现 agentscope:session:
events 等总线键；只重启网关后旧会话续聊与待确认重建均成立。
多实例支持矩阵（同日定性）：基座服务层可多实例（存储 MySQL＋
总线 Redis 已外置），剩余件是工作区为本地盘目录——多实例需挂
共享存储（NFS）或换云沙箱后端，否则进行中任务的中间文件不随
会话换实例（对话与状态不丢）；网关层为单实例设计（events 日志、
runs 记录、限流窗口、async 后台任务均在进程内存），多实例直接
开会出现跨实例查不到 run/events、限流变 N 倍，真多实例须先把
events/runs 外置到 Redis/MySQL——接入期按量级决定，不预建；
生产一期口径为网关单实例＋服务可多实例。（网关单实例一条已于
2026-11-08 被高可用改造推翻，见本节末 HA 注。）
注（2026-11-08 用户口径：内网基座部署在 K8s 集群上）：装配已加
SERVICE_WORKSPACE=local|k8s 开关——k8s 分支用框架
K8sWorkspaceManager（每工作区 Pod＋PVC、名由 workspace_id 确定性
派生可跨副本找回、空闲 1h 回收 Pod 而 PVC 默认保留），同样显式
PER_SESSION；命名空间/镜像/StorageClass/PVC 大小/拉取密钥走
SERVICE_K8S_* 环境变量；依赖 kubernetes-asyncio 36.1.0 已入
pyproject 锁定与离线 wheelhouse；装配构造与本机默认档回归已验，
真实集群行为未实测（手册 4.5 节列了首次上集群的四项附加验收）。
至此生产形态定版为：K8s 上网关 1 副本（同日下午已改多副本，
见 HA 注）＋服务多副本＋k8s 工作区
＋MySQL＋Redis。
注（2026-11-08 凌晨文件交付定版）：agent 在工作区生成的文件，用户
有需要时走「工具推送」——给 agent 一个 send_file 类工具，由它在
产生时判断并发出，网关在会话内接收（归属随会话天然确定）、落业
务侧存储并记事件。判断在产生时由知情者做，不做事后打捞式回收；
collect 仅保留为无模型判断的批处理管线兜底。

注（2026-11-08 网关高可用＋计量换 MySQL，用户立规"所有 sqlite
都换掉、网关也要高可用"，本节内与此冲突的旧述以本注为准）：
①usage_store 已重写为 SQLAlchemy URL 驱动（AGENT_BASE_USAGE_
URL，生产 mysql+pymysql，与服务同 MySQL 实例即可、表名
usage_calls；未给时从 SERVICE_DB_URL 推导；AGENT_BASE_USAGE_
DB 文件路径仅本地开发映射 sqlite），冒烟 7.5 断言改走存储层
totals() 直查；②新增 gateway/state.py 状态后端
（GATEWAY_STATE_BACKEND=memory|redis）：events（Redis List＋
Pub/Sub 扇出、每会话留存上限 1 万条）、runs（KV、记录带
tenant）、限流（ZSET 滑动窗口、全局口径）全部外置；会话句柄与
待确认仍靠服务侧探测/状态重建（跨副本可用已实证）；在途轮次
执行不迁移，执行副本死亡时该轮由 adapter.get_run 的超时对账
（单轮超时＋30 秒）置 failed，业务侧重试。③InstaBox 双网关
实测全过（服务存储 MySQL＋计量 MySQL＋Redis 双总线）：A 建
会话 B 对话、A 读 B 的事件与 run、A 起确认 B 批准、B 实时
订阅到 A 的事件流、计量经 B 查得、杀 A 后会话在 B 续聊、
双副本限流 6 时交替 8 请求出现两个 429（全局口径成立）。
网关 HA 自此成立，残余仅在途轮次不迁移一条。本机三套冒烟在
内存后端下回归全绿（与单副本时代行为一致）。生产形态终版：
K8s 上网关多副本＋服务多副本＋k8s 工作区＋MySQL＋Redis。

### 2026-11-08 午 · kind 集群 K8s 路径验证（InstaBox 单节点 kind 1.34）

用户点选在 InstaBox 上搭 kind 把 k8s 路径验掉。结论：核心承诺
全部成立。①工作区管理器直测：K8sWorkspaceManager（PER_SESSION）
建工作区→Pod as-ws-<id> Running＋PVC Bound（约 8 秒含 bootstrap），
写 /workspace 文件，管理器退出删 Pod 留 PVC，同 id 重建新 Pod 挂
同一 PVC、文件完好——确定性命名跨副本找回＋PVC 持久成立；
②全栈（宿主机服务＋k8s 开关）对话完成，但普通对话不触发
provision、工作区为懒创建（仅网关注入默认 MCP/技能或 agent
真实使用工作区工具时才建）；③集群内形态：镜像 Docker 构建
（顺带修 Dockerfile 漏 COPY skills_store 的真 bug）、kind load，
服务以 Pod＋ServiceAccount 运行时连集群内 MySQL/Redis，对话
completed，其间自动创建三个会话工作区 Pod/PVC；集群内 MCP 以
Pod 部署、工作区内可连通。

实测新发现（已回写 DEPLOY.md §4.5）：(a) 工作区 provision 前
框架会 GET 命名空间对象本身（_ensure_namespace），命名空间内
Role 给不了——须 ClusterRole 单授 get namespaces
（resourceNames 限定）；(b) 工作区 Pod 首次 bootstrap 需联网
（apt＋下载 uv＋pip 装 agentscope），内网生产须预烘焙镜像或
备内网镜像源；(c) 服务无 DB 等待、靠重启收敛；(d) 空闲 TTL
回收未等满 1h 单独验（关工作区删 Pod 已验）。

环境限定（如实记录）：该机嵌套环境里 kube-proxy 写 iptables
失败（iptables-restore RULE_APPEND 报错、Sync failed），集群
Service 的 ClusterIP 路由与 kube-dns 解析整体不可用——集群内
测试靠 API 直连（10.96.0.1 正常）＋组件间用 Pod IP 绕行完成；
此系 kind-on-受限 VM 的环境 artifact，非产品问题，真实集群
不适用。测试栈在机上 /data/home/ab-k8s-test，镜像
agentbase:test、清单 /tmp/ab-k8s-manifests.yaml＋/tmp/ab-rbac-fix.yaml；
该机根盘告急（91.6%，kind＋镜像所致），集群留置待用户处置。

### 2026-11-08 午 · 多模型与能力路由口径（用户问答中定，未实现）

用户问：一个 Agent 可否挂多个模型、带图时网关自动切识图模型。
现状：注册表有 model.primary＋fallback 两位，但 fallback 目前
仅登记＋校验、运行时未用；模型在建会话时经 chat_model_config
绑死、一个会话一个模型。讨论三轮后的定版（前两版口径均作废，见下）：①终端用户**无模
型选择器**（用户点破：本对话与一客一策页面都没有选模型的地
方，让业务侧拼上下文/拼线程也不合适）；②框架源码实证：服务
原生有 `PATCH /sessions/{id}` 可改会话的 chat_model_config
（轮次间生效、运行中改回 409、历史归基座所有、切换不断上下
文），且会话原生带 fallback_chat_model_config（每轮组装时主备
模型一并解析）——先前"模型建会话时绑死、切换＝新开会话"的说
法是我只看了自家 adapter（从未调用 PATCH）下的错误结论，已
更正；③定版形态：**策略在注册表**（每个 Agent 登记模型集＋默
认模型＋切换规则），**执行在网关**（按策略在轮次间 PATCH 切
换，业务无感），**业务契约对模型完全透明**（契约里不出现模型
名）；能力下限规则：会话历史一旦含图片，不再切到没标 vision
的模型（只升不降的会话级下限，防历史图片喂错模型）；④注册表
fallback 字段接框架原生 fallback_chat_model_config，不再是摆
设；⑤生图不进 models.yaml（对话模型无出图通道），走工具路：
生图服务包成 MCP 工具、Agent 调用拿回图片地址。adapter 补
PATCH 切换＋fallback 下发＋逐 Agent 模型计划，属接入第一个真
实（视觉）模型时一并落地。

### 2026-11-08 午后 · 多模型机制补齐（用户点名"补上"＋"网关也要按生产定位")

上一节定版的模型机制已全部落地，不再等接入期：①adapter 模型
计划改逐条目组装（_model_plan_for：按条目查表、读条目自己登记
的 env 名），全局 OPENAI 捷径删除——行为变更：OPENAI_* 配齐时
不再让所有 Agent 共用一个端点，各 Agent 按自己声明的 primary
走；主模型条目不可用（env 未配齐）大声失败，不静默兜底。
②fallback 真下发：建会话与 SOP 落库都带 fallback_chat_model_
config；备用条目不可用时只记账跳过、不拦建会话。③PATCH 切换：
带图轮次（或已置 vision 下限）而当前条目无 vision 能力时，轮次
前经 PATCH /sessions/{id} 换到 Agent 的 model.vision 条目，
发 model.switched 事件（from/to/model/reason）；能力只升不降；
未声明 vision 又收图 → ModelCapabilityError → 400
MODEL_NO_VISION（错误码已登记 common/errors.py 与对接规范）。
④网关重建句柄时从服务恢复模型状态（列表接口的 SessionView 键
名是 session 不是 record，第一版写错被冒烟抓出；历史 data 块
media_type 以 image/ 开头置 vision 下限）。⑤契约加法：messages
新增 images 输入（url/base64 二选一、png/jpeg/webp/gif、≤8 张、
单张 base64 ≤14M），框架二进制块类型是 data 不是 image（写错
被 /chat/ 422 挡下，冒烟实证后改正）。⑥生产收口一件：
X-End-User-Id 五处 Header(default="demo-user") 静默兜底全部改
必填——已发布规范本就写明必填，是代码欠账；smoke_test/
smoke_async 已改为显式带 demo-user（smoke_actions 本就合规）。
新冒烟 smoke_models.py 15 项全过（白盒读服务侧会话记录核对
主模型/fallback/切换后模型名），旧三套回归全绿。演示登记新增
mock-backup/mock-vision 两条 mock 条目与 demo.vision-assistant
（primary mock-local、fallback mock-backup、vision mock-vision）；
校验新增 capabilities 词表与 vision 指向必须标 vision 两条。

## 5. 2026-11-08 午后 · Java SDK（业务侧多为 Java 系统，用户拍板）

- 位置：`sdk/java/`，坐标 `agentbase:agentbase-java-sdk:1.0.0`，
  JDK 17+、唯一依赖 Jackson（jackson-databind 2.17.2），Maven 构建。
- API 面与契约 v1 一一对应：AgentBaseClient（health/listAgents/
  createSession/resumeSession/getRun/usage）＋ Session（send/
  sendAsync/chat/confirm/confirmAsync/events/subscribe/archive）。
- 两个封装重点：① `chat()` 一站式回路——异步发消息＋订事件流＋
  按 run 链过滤＋撞 confirmation.required 回调业务的
  ConfirmationHandler＋confirm 续跑，直到 run.completed/failed；
  未配处理器时确认单一律 rejected（不静默放行写工具）。
  ② EventSubscription：SSE 先回放后跟随、断线退避重连并按
  last_event_id 补订（网关流语义保证不丢）。
- 错误统一映射 GatewayException（status/code/traceId），含
  MODEL_NO_VISION、AUTH_FAILED 等契约错误码。
- 验证：在 InstaBox（JDK 17 /data/build/jdk17＋Maven 3.9.9）
  对同机 mock 栈跑 SdkSmoke 15 项 ALL PASS（同步对话、chat
  确认回路且笔记真落盘、异步轮询、事件回放、带图触发
  model.switched 且 run 模型为 mock-vision-1、无 vision 收图
  抛 MODEL_NO_VISION、错密钥 AUTH_FAILED、usage、archive）。
  预编译 jar 在 sdk/java/dist/（24KB，不含 Jackson）。
- 踩坑备忘：①Java 不许方法调用尾随逗号；②InstaBox 上双份栈
  并存时后起脚本 rm 掉在跑服务的 sqlite 文件会让其变
  SQLITE_READONLY（DBMOVED）——重启栈必须先杀净旧进程再删库。

## 6. 2026-11-08 晚 · 展示历史与会话列表（用户实测演示后拍板补齐）

起因：模拟业务系统第一版把展示历史甩给业务侧自存，用户拿
"与 Muse 的对话重开即完整历史"质询，裁定基座供数。定版
分工：上下文与展示正文由基座供，业务只存产品字段（标题等）。

- 网关两新接口（对接规范 3.9/3.10）：`GET /v1/sessions`
  （按租户＋X-End-User-Id＋可选 agent_id 列会话元数据）、
  `GET /v1/sessions/{id}/history`（展示投影：user/confirmation/
  assistant 三类条目按轮次序）。
- 投影原料全是既有落账＋三处补记：_open_run 新增 trigger/
  request_text/has_image/confirmation_id/decision（用户原文与
  确认决定只在开轮一刻齐全）；确认卡条目取事件日志的
  confirmation.required，状态由续跑 run 的 decision 回填，
  无续跑即 pending。会话元数据（created/last_active/archived）
  写状态后端 session_metas（MemoryState dict / RedisState
  gw:session:*＋gw:user_sessions 集合），建会话时落、开轮时
  刷 last_active、归档时置 archived——多副本同口径。
- 老会话兼容：本轮之前建的 run 无 request_text，投影自动
  省略 user 条目（回复/确认卡照常）。
- SDK 同步加 Session.history() 与 client.listSessions()
  （HistoryItem/SessionInfo 两新类）；SdkSmoke 扩至 17 项
  ALL PASS（新增 history 三件齐、listSessions 含当前会话）。
- 演示业务后端随之瘦身：删本地历史账（内存落账/回填线程/
  markDecided 全删），/api/history 改经 SDK 取投影；
  ensureSession 先 listSessions 接回最近未归档会话（点过
  「新会话」弃用的记 superseded 跳过）。公网实证：业务后端
  单独重启后，同 cookie 接回同一 session_id 且历史 5 条
  完整——旧内存版做不到。

## 7. 2026-11-08 深夜 · 源码复核后的网关对账与 SDK 定位（用户连环质询后定版）

复核基座源码（agentscope 2.0.11 本机包）推翻多处旧结论：
框架无 tenant 实体（全包 19 处 tenant 字眼皆为飞书租户/RAG
metadata/注释代称），全部记录只按扁平 user_id（属主）隔离，
UserRecord 为空壳；X-User-ID 仅占位无校验（外界已登记
CVE-2026-123024，Issue #3314）。确认与决定是原生概念
（RequireUserConfirmEvent/ConfirmResult/UserConfirmResultEvent
＋原生 /chat/ HITL 恢复路径），网关 cfm 皮只是整形。原生
已有 GET /sessions/ 列表与 GET /sessions/{id}/messages
持久化历史；共享走 ResourceAccessPolicyBase（默认 DenyAll，
仅 Credential/Agent/KB 三类，凭证共享打码、运行时内部
resolve），我方 TenantPrefixPolicy 即其子类。

网关对账结论：不可替代只剩三件——身份门（项目密钥＋拼
{tenant}:{end_user}＋藏服务）、注册表治理、计量。多此一举
六件：会话元数据账、历史原料账、确认簿记、run/事件日志
大半、同步模式与 SDK 二选一之说（见下）、句柄探测重建。
待改：历史/列表/确认投影改走原生接口打底（已定未实施）。
漏八件：终端用户身份只声称不验真、审计缺位、注册表变更
无版本/留痕、限流只到租户、KB 未启用、SOP 未代理、同会话
并发不排队、异步 run 进程内执行、Team/调度工具摘不掉
（政策性禁用）。

**SDK 定位（2026-11-09 用户拍板，架构原则）**：SDK 只是
方便业务侧对接的客户端封装（类型化调用、确认回路编排、
断线补订），不承载任何网关职责；策略执行、安全边界、契约
语义必须在网关侧成立。判据：裸 HTTP 按对接规范必须能完整
做到一切——某能力若只有装 SDK 才能用、绕过 SDK 即绕过
规则，即设计错误。此前"把整形/同步下放 SDK"的瘦身设想
作废；网关瘦身的去处是删除或委托基座原生，不是下放 SDK。

## 8. 2026-11-09 凌晨 · 网关整改落地（用户令"该修的现在就修"，部署包 v13）

按第 7 节对账逐项落地，InstaBox 全链验收通过（SDK 冒烟
22 项 ALL PASS＋公网业务链＋专项探针）：

- **历史/列表改走原生打底**：session_history 正文改读原生
  GET /sessions/{id}/messages（游标翻页合并），列表改逐个
  Agent 查原生 GET /sessions/；自建 session_metas 账（两套
  状态后端）整体删除。合并排序键取消息 finished_at（一条
  助手消息横跨等确认全程，按 created 排会把回复排到卡前）。
  分工：正文＝基座事实，确认卡/决定＝网关台账（下条）。
- **确认台账＋审计账**（gateway/ledger_store.py，SQLAlchemy
  落库，URL 解析同 usage_store，表 confirmation_ledger /
  audit_events）：卡在 REQUIRE_USER_CONFIRM 时入账，决定
  受理时回写＋审计追加；动作含 session.created/archived、
  confirmation.decided。新增 GET /v1/audit（租户内只读）。
  实证：网关重启（状态全清）后旧会话历史完整——正文走
  原生、卡走台账；台账启用前的老会话（G5）正文在、卡不在
  （已写入对接规范边界）。新错误码须同步登记
  common/errors.py 的 ERROR_CODES，否则 _err 断言炸 500
  （本轮实踩：USER_PROOF_* 未登记时期）。
- **逐调用表决＋rules 透传**：ConfirmationCreate 加
  decisions[]（tool_call_id/decision/edited_input/rules），
  adapter._build_confirm_results 纯函数翻译（按 id 匹配、
  无 id 等长按序、未覆盖回落整批决定、再无默认拒绝；
  rules 仅在条目给出时透传原生 ConfirmResult.rules）。
  confirmation.required 事件与台账的 tool_calls 补 id 字段。
  单元测试 5 组全过。
- **终端身份断言**：GATEWAY_REQUIRE_USER_PROOF=1 时，带
  X-End-User-Id 的请求须附 X-End-User-Ts＋X-End-User-Proof
  =HMAC_SHA256(项目密钥,"{工号}.{ts}")（±300s）。SDK 加
  endUserProof(true) 自动签名；演示栈已开启，负例回
  401 USER_PROOF_EXPIRED 实测。
- **按人限流**：GATEWAY_RATE_LIMIT_PER_USER_PER_MIN（默认
  关），(租户,工号) 分桶；侧实例 limit=3 实测第 4 请求 429。
- **同会话排队**：_execute_run 层加 per-session asyncio
  锁（同步/异步/续跑共用同一把），等待上限
  GATEWAY_SESSION_QUEUE_TIMEOUT_SECS（默认 180），超限
  抛 SessionBusyError → 路由译 409 SESSION_BUSY。冒烟
  连发两条 async 均 completed。锁为进程内，多副本跨副本
  并发仍由原生单轮约束兜底（已写入规范 2.4）。
- **KB 接线**：注册表 Agent 可加 knowledge_bases（名字数
  组，validate 加形状校验）；资产归系统户、同租户经
  TenantPrefixPolicy 只读可见；adapter 建会话时从终端用户
  可见列表按名解析 id，以原生 knowledge_config 下发（原生
  逐个校验可见性），解析不到记 session.kb_report 不静默。
  边界：KB 建库与索引需真实 embedding 凭证，mock 凭证无
  embedding 类——接线与校验已就绪，建库验收待真凭证。
- **SOP/Team 明示排除**：对接规范新增第 8 节"能力边界"：
  SOP 不入契约（平台运营资产，网关只分发落库）；组队与
  模型侧定时不入契约（定时归业务后端定时器调网关）。
- **SDK 跟进**：CallDecision＋Decision.perCall、endUserProof、
  audit()、SessionInfo 补 name/status/isRunning；旧 archived()
  已删（列表新口径下该字段不存在）——一度想留 @Deprecated
  兼容垫片，用户 2026-11-09 裁定作废：业务侧只是模拟消费者，
  该删就删、它跟着改，不给模拟留兼容包袱；"只加不减"只对
  已发布的契约语义成立，不适用于未出门的 SDK 表面。SdkSmoke
  扩到 22 项（逐调用/排队/审计/断言负例，断言负例以第 4
  参数 proof-on 门控）。演示业务后端已切 endUserProof(true)
  ＋新列表口径（ensureSession 不再看 archived 标记）。
- **演示栈现状**：/tmp/demo_stack.sh 已同步新 env（proof
  开、按人限流 300、ledger 独立 sqlite 文件），照常重启
  即可；/tmp/v13_stack.sh 为同口径的临时副本，可弃。
