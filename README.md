# 通用 AI 基座 v1 骨架（agent-base）

基于 **AgentScope 2.0.11** 的自建薄契约层骨架：一个对外契约稳定的薄网关
+ AgentScope Agent Service 装配 + MCP 工具供给 + YAML 注册表。
设计依据：《AgentScope 通用 AI 基座设计文档 v1.1》
（`~/workspace/your_files/AI基座设计/`）。探针实测与差距记录见 `NOTES.md`。

## 架构

```
项目方 ──X-Project-Key──▶ gateway (:8000)  对外契约 v1（第 7 章子集）
                            │  鉴权 / 租户 / 限流 / 统一错误信封 / 计量
                            │  adapter 层：框架接触只在此、且只经原生 HTTP API
                            ▼  每请求带可信头 X-Tenant-Id / X-End-User-Id
                     service (:8100)  AgentScope 原生 Agent Service（独立进程）
                            框架原生路由；存储 SQL(SQLite demo) / 总线 InMemory demo
                            身份：可信头中间件 → X-User-ID={tenant}:{end_user}
                            Agent ──MCP──▶ mcp_demo (:8200)
                                              demo_time (ALLOW) / demo_note_write (ASK)
```

契约流量全部经 adapter 走服务原生 HTTP API（供给 ensure → 建会话 →
订阅事件流后触发 chat → 按 reply_id 翻译事件；确认走
UserConfirmResultEvent 回提），会话与消息历史归 service 存储，
网关重启不丢会话。契约可见的 session_id **直接透传服务侧会话 id**
（32 位 hex；早期骨架的 `ses-` 前缀格式已退役）。映射细节与剩余
边界见 NOTES.md 第 2 节。

## 目录

```
pyproject.toml        依赖全锁定（agentscope[service,storage-sql,storage-redis]==2.0.11 等）
.env.example          配置模板（密钥占位；复制为 .env 使用，.env 不入库）
common/               配置加载（.env/环境变量）、契约错误码与信封
service/              AgentScope Agent Service 装配（create_app）
  app.py              存储/总线/工作区装配 + 可信头身份中间件
  mock_model.py       mock 凭证 + mock 模型（确定性演示逻辑）
  native_check.py     原生服务全链检查（凭证→Agent→会话→对话）
  run.py              uvicorn 入口（默认 :8100）
gateway/              对外契约 v1 薄网关（FastAPI，默认 :8000）
  app.py              鉴权/限流/租户 + 8 个契约端点
  adapter.py          ★ 唯一的框架接触面（经服务原生 HTTP API）：供给 ensure/
                        会话/事件流翻译/确认提交/计量拦截；不 import agentscope
  usage_store.py      usage 逐调用流水落库（SQLite，与调用方式无关的独立模块）
  run.py
agent_registry/       YAML 注册表（设计第 6 章）
  models.yaml         模型注册（密钥只写 env 引用名）
  mcp_servers.yaml    MCP 服务器与工具等级（三级 ALLOW/ASK/DENY、按租户登记）
  skills.yaml         技能登记（按租户、default/optional，本体在 skills_store/）
  sops.yaml           SOP 登记（ensure 时以租户系统户落库分发）
  demo.echo-assistant.yaml / tenant-a.assistant.yaml / tenant-b.assistant.yaml
  prompts/            系统提示词
  loader.py / validate_registry.py   加载与校验（必填/引用完整性/权限越级/DENY 禁登）
skills_store/         技能本体自托管目录（每技能一个文件夹，含 SKILL.md）
service/
  app.py              create_app 装配（关 channel worker/scheduler、PER_SESSION）
  access_policy.py    TenantPrefixPolicy：同租户凭证/KB 只读共享、跨租户全拒
mcp_demo/server.py    最小 MCP Server（mcp SDK 2.x streamable-http，:8200）
scripts/gen_demo_env.py   生成本地 .env（随机 demo key）
smoke_test.py         基线冒烟（见下）
smoke_actions.py      动作清单分阶段冒烟（见下）
probe/probe_assembly.py   装配探针（进程内起生产装配查开关与工具清单）
docker-compose.yml    gateway/service/mcp + redis/postgres（infra profile）
Dockerfile
NOTES.md              探针结论、adapter 差距、外部现成基座核查
```

## 本地运行

```bash
cd ~/workspace/agent-base
export PATH="$HOME/.local/bin:$PATH"

# 1) 建 venv 并安装（版本已锁定）
uv venv && uv sync            # 或 uv pip install -e .

# 2) 生成本地 .env（随机生成 demo 租户 demo-dev 的 Project Key）
.venv/bin/python scripts/gen_demo_env.py
cat .env                      # PROJECT_KEYS=demo-dev:<随机 key>（勿提交）

# 3) 分别起三个进程（三个终端，或 nohup 后台）
.venv/bin/python -m mcp_demo.server     # :8200
.venv/bin/python -m service.run         # :8100
.venv/bin/python -m gateway.run         # :8000

# 4) 冒烟（一键，会自己拉起三个进程并写 smoke_output.txt）
.venv/bin/python smoke_test.py
```

计量流水默认落 `data/usage.db`（已被 .gitignore 覆盖，不入库），
可用 `.env` 的 `AGENT_BASE_USAGE_DB` 改路径。

手动体验（KEY 从 .env 读）：

```bash
KEY=$(grep '^PROJECT_KEYS=' .env | sed 's/.*demo-dev=//')
curl -H "X-Project-Key: $KEY" localhost:8000/v1/agents
SID=$(curl -s -X POST -H "X-Project-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo.echo-assistant"}' localhost:8000/v1/sessions \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
# 普通回复
curl -X POST -H "X-Project-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"你好"}' localhost:8000/v1/sessions/$SID/messages
# 触发 ALLOW 工具（直接执行）
curl -X POST -H "X-Project-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"现在几点？请查一下时间"}' localhost:8000/v1/sessions/$SID/messages
# 触发 ASK 工具（返回 awaiting_confirmation，再到 events 里取 confirmation_id 批准）
curl -X POST -H "X-Project-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"帮我记一条笔记：你好基座"}' localhost:8000/v1/sessions/$SID/messages
curl -H "X-Project-Key: $KEY" localhost:8000/v1/sessions/$SID/events
curl -X POST -H "X-Project-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"decision":"approved"}' localhost:8000/v1/sessions/$SID/confirmations/<cid>
curl -H "X-Project-Key: $KEY" localhost:8000/v1/usage
```

Docker（为 InstaBox 部署准备，本机未实测 docker 路径）：

```bash
cp .env.example .env   # 填好 PROJECT_KEYS
docker compose up -d gateway service mcp
docker compose --profile infra up -d   # 加 redis + postgres（生产形态）
```

## 模型：demo 默认 mock，接真实模型只改 .env

- 默认 `MODEL_MODE` 未配真实端点 → mock 模型：确定性回复。含「时间/time」
  触发 ALLOW 工具 demo_time、含「笔记/note」触发 ASK 工具 demo_note_write、
  其余回 `[mock] 收到：…`。
- 接 OpenAI 兼容端点（含内网模型）：在 `.env` 填
  `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` 三项并把示例 Agent
  的 `model` 换成注册表里的 `openai-compatible-default`（或新增模型条目），
  无需改代码。

## 冒烟结果（2026-11-07 实测，全过）

`smoke_output.txt` 为脚本实际输出存档。覆盖：无 key 被拒（AUTH_FAILED）→
列 Agent → 建会话（服务侧 session id 透传）→ mock 回复 → ALLOW 工具直行 →
ASK 确认暂停/批准/落盘 → events 回放 → usage 计量 → usage 落库持久化
（直查 SQLite 流水 + 重启 gateway 后数值不变）→ 会话落服务存储
（直查服务 API 见消息历史与 awaiting_permission 状态）→ 重启 gateway
后批准重启前 park 的确认（pending 从服务状态重建、工具落盘）→
重启后旧会话续聊且历史完整 → 原生 service 全链（可信头身份）。

动作清单分阶段冒烟：`.venv/bin/python smoke_actions.py`（独立
key/数据目录，输出存档 `smoke_output_actions.txt`，2026-11-07
实测全过）。覆盖：注册表三级校验（含 DENY 负例）→ 装配探针
（开关/运行时状态/模型实收工具清单）→ 同租户共享与跨租户隔离
→ 组织级凭证全链 → 按租户注入 MCP/技能 → 工作区回收 → SOP
落库与终端不可见缺口实证。

## 演示范围与已知差距（对设计文档 v1.1）

**已做（真实实现且实测）**
- 第 7 章契约核心子集 8 端点：agents / sessions 建删 / messages /
  events（回放 + 回放式 SSE）/ confirmations / runs / usage / health
- 7.4 错误码与统一信封、trace_id；网关鉴权（X-Project-Key）、租户映射、
  按租户限流（进程内滑动窗口）
- 第 5 章身份规则 `user_id={tenant}:{end_user}`：网关每请求下发可信头、
  service 中间件拼装；凭证/Agent/会话按该 user 隔离（全部会话级
  端点须带 `X-End-User-Id` 且与建会话时同值，缺省 demo-user）
- 第 6 章注册表 YAML + 校验脚本（必填、引用完整性、权限越级拦截）；
  服务侧凭证与 Agent 由网关按注册表幂等 ensure（已存在则复用/校对）
- 第 8 章工具分级（三级 ALLOW/ASK/DENY，2026-11-07 由 L0–L3 收敛）：
  ALLOW 只读经 MCP 注解 readOnlyHint 走权限引擎快速通道（放行
  等价）、ASK 写工具无规则默认 ASK、走框架原生 HITL 暂停-确认-
  恢复全链（全程 HTTP）；DENY 在注册表校验层直接拒绝登记
- 装配收口（排查清单落地）：create_app 显式关 channel worker 与
  scheduler、装配 TenantPrefixPolicy（同租户凭证/KB 只读共享、
  AGENT 不共享、跨租户全拒）；工作区 PER_SESSION 锁定；记忆
  middleware 默认不装配（装配注释锁定，见 service/app.py）
- 组织级凭证：模型凭证建在租户系统户 `{tenant}:__system__` 名下
  一份、同租户共用，终端用户键下不建个人凭证；异租户引用建会话
  即 404
- 按租户动态注入：网关建会话后按会话租户把注册表里 default 的
  MCP（全量配置直加工作区）与技能（upload 直装）注入，optional
  不注入；技能本体在仓库 `skills_store/` 自托管
- 工作区产物回收：adapter `collect_artifacts` 经服务文件 API 把
  会话工作区回收到 `data/artifacts/{tenant}/{session_id}/`（暂未
  挂契约端点）
- SOP 分发（部分）：注册表 SOP 在租户首次供给时以系统户落库；
  框架无 SOP 共享通道，终端用户暂不可见/不可跑（缺口见 NOTES）
- service 装配：SQL 存储（SQLite demo）/InMemory 总线/Local 工作区 +
  Redis 切换开关；mock 凭证走框架 `extra_credentials` 正规机制
- 第 9 章计量落库：逐模型调用一行流水落 SQLite（`gateway/usage_store.py`，
  粒度对齐官方 Java 版 Service 的逐调用计量），/v1/usage 按租户查表聚合；
  口径：runs＝产生过至少一次模型调用的 run 数（DISTINCT run_id），
  token 为流水求和，数值跨 gateway 进程重启累计不清零（模型自报）

**骨架/等价实现**
- 网关本地仍留三样内存簿记：契约 events 回放、runs 记录、待确认
  pending——网关重启会丢；其中 pending 已做兜底（确认时未命中则从
  服务会话消息尾部的 asking 态 tool_call 重建），events/runs 暂无
- events 对外是事件日志回放（`?stream=true` 为回放式 SSE），非实时
  长连接推送（网关对内消费的已是服务原生 SSE 实时流）
- usage 已落库（逐调用流水）；账单口径（单价/成本）与按租户配额拦截未接；
  落库不可用/写入失败时自动降级为进程内内存聚合（原行为兜底）
- 限流为单机内存版，生产应换 Redis

**没做（一期后续）**
- 知识库启用（装配未传 knowledge_base_manager，待 embedding
  端点与向量库选型；共享政策已预留同一路径）
- SOP 的终端用户可用性（已落库分发，网关侧 SOP 代理未做）
- 第 10 章记忆中间件装配（Agentic Memory 一期默认仅在设计层定版，未接）
- 第 11 章工具沙箱（nspawn/容器隔离）、凭证保险库
- 第 12 章可观测（OTel/结构化日志汇聚）、评测集运行器（eval_set 仅登记）
- 注册/管理 API 与管理台（按定版「暂收」）；多 Agent 编排（Team/A2A）
- docker 路径未在本机实测（本机无 docker 验证条件），compose 仅静态校验
