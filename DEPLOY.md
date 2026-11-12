# agent-base 部署手册（骨架＋AgentScope 基座）

给部署执行者（人或 agent）的实际部署步骤。设计原理看 `docs/` 里的设计文档 v1.1，
实现细节与已知边界看 `NOTES.md`，本手册只讲怎么装、怎么配、怎么验。

## 0. 组成与端口

| 进程 | 端口 | 角色 | 暴露面 |
|---|---|---|---|
| gateway（本包 `gateway/`） | 8000 | 对外契约 v1 网关：鉴权/租户/限流/计量/确认中继 | **只对业务后端**开放，终端用户页面永不直连 |
| service（本包 `service/` 装配） | 8100 | AgentScope 2.0.11 原生 Agent Service：推理循环、会话与历史存储、工具执行 | 只供 gateway 调用，不对业务侧开放 |
| mcp_demo（本包 `mcp_demo/`） | 8200 | 演示 MCP 工具（时间/记笔记） | 可选，仅演示与冒烟需要 |

基座本体是 pip 包 `agentscope[service,storage-sql,storage-redis]==2.0.11`，
版本已在 `pyproject.toml` 全锁定，不要擅自升级；升级前必须双冒烟全绿（见第 5 节）。

## 1. 前置条件

- Linux x86_64；Python 3.11 或 3.12（`requires-python >=3.11,<3.13`），或 Docker。
  机器上只有 3.13 时不要改锁定：用 uv 装一个 3.12 并建 venv——
  `uv python install 3.12 && uv venv --python 3.12 .venv`（2026-11-08
  在 InstaBox 实测，该机只有系统 Python 3.13.5，此路走通）。
- 模型端点：一个 OpenAI 兼容接口（地址＋模型名＋key）。没有就只能用内置 mock
  模型验管道（确定性假回复），不能验模型效果。
- 生产环境：MySQL 8.x（服务存储；先建一个空库，表由服务首启自动建）。
  Redis 7 可选（见第 3 节存储开关说明）。
- 网络：部署机能访问模型端点、MySQL（、Redis）；业务后端能访问 gateway:8000。
- 离线部署：包同级目录 `wheelhouse/` 已含全部锁定依赖的 wheel，安装命令带
  `--no-index --find-links` 即可断网安装。若 wheelhouse 缺失，在有网机器上
  进 `agent-base/` 执行 `python -m pip download . "setuptools>=68" wheel -d ../wheelhouse` 重新生成。

## 2. 安装

方式 A：裸机/虚机（内网推荐）

```bash
cd agent-base
python3.12 -m venv .venv
. .venv/bin/activate
pip install --no-index --find-links ../wheelhouse .   # 离线
# 用 uv 建的 venv 默认不带 pip：建时加 --seed，或改用
# uv pip install --python .venv/bin/python --no-index --find-links ../wheelhouse .
# 在线环境则：pip install .
```

方式 B：Docker

```bash
docker compose build
docker compose up -d gateway service mcp
```

纯离线环境用 Docker 时，先在有网机器 build 后 `docker save` 导出镜像再导入，
不要指望部署机能拉 `python:3.12-slim` 基础镜像。compose 里的 `infra` profile
（redis/postgres）是备选件：用外部 MySQL 时不要起其中的 postgres。

## 3. 配置（.env）

```bash
cp .env.example .env
python scripts/gen_demo_env.py   # 自动生成带随机 PROJECT_KEYS 的 .env，可再手工改
chmod 600 .env                   # .env 永不入代码库、永不入包
```

关键项（`.env.example` 有全量注释）：

| 变量 | 说明 |
|---|---|
| `PROJECT_KEYS` | `租户=随机密钥` 逗号分隔，网关鉴权用。每个接入项目一个租户，密钥必须随机生成，不许用示例值 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` | 真实模型三件套。三者留空＝走 mock 模型（仅验管道） |
| `SERVICE_DB_URL` | 服务存储连接串。生产改 MySQL：`mysql+aiomysql://用户:密码@主机:3306/库名?charset=utf8mb4`（驱动 aiomysql 已锁定在依赖里）。默认的 SQLite 文件仅供试跑 |
| `SERVICE_STORAGE` | `sql`（默认；存储走 `SERVICE_DB_URL`）或 `redis`（存储走 Redis） |
| `SERVICE_BUS` | 消息总线：`memory`（进程内）或 `redis`，与存储独立选择。**生产组合（已实测）：`SERVICE_STORAGE=sql`＋`SERVICE_DB_URL` 指 MySQL＋`SERVICE_BUS=redis`**，Redis 连接看 `SERVICE_REDIS_HOST/PORT`。不设置时沿旧行为（存储 redis 则总线 redis、否则 memory） |
| `AGENT_BASE_USAGE_DB` | 计量流水库的本地文件路径，仅开发/冒烟用；生产走 `AGENT_BASE_USAGE_URL`（MySQL），见下行 |
| `GATEWAY_RATE_LIMIT_PER_MIN` | 网关限流，默认 120/分钟/租户 |
| `GATEWAY_RATE_LIMIT_PER_USER_PER_MIN` | 按终端用户限流（0=关）；开启后单个工号独立分桶 |
| `GATEWAY_REQUIRE_USER_PROOF` | `1`=开启终端身份断言校验：带 `X-End-User-Id` 的请求须附 HMAC 断言（公式见对接规范 2.1），业务后端/SDK 须同步开启，否则 401 |
| `GATEWAY_SESSION_QUEUE_TIMEOUT_SECS` | 同会话消息排队等待上限，默认 180 秒，超限回 409 `SESSION_BUSY` |
| `AGENT_BASE_LEDGER_URL` | 确认台账＋审计账连接（生产：`mysql+pymysql://…`，可与服务存储同实例）；未给时从 `SERVICE_DB_URL` 推导。**本账是确认卡历史与审计的数据来源，生产必须可用**，不可用时网关降级运行但历史缺确认条目、审计缺失 |
| `GATEWAY_STATE_BACKEND` | 网关状态后端：`memory`（默认，进程内、单副本）｜`redis`（生产高可用：events/runs/限流外置共享，多副本可扩，2026-11-08 双副本实测）；Redis 连接取 `GATEWAY_REDIS_HOST/PORT`，未设回退 `SERVICE_REDIS_*` |
| `AGENT_BASE_USAGE_URL` | 计量库连接（生产：`mysql+pymysql://…`，与服务存储同 MySQL 实例即可）；未给时从 `SERVICE_DB_URL` 推导同库；`AGENT_BASE_USAGE_DB` 文件路径仅本地开发 |
| `MCP_URL` | 全局默认 MCP 地址。不要 demo 工具时留空，并注意注册表里 demo 工具随之不可用 |

## 4. 启动

裸机按顺序拉起（mcp_demo 可跳过）：

```bash
. .venv/bin/activate
python -m mcp_demo.server    # :8200，可选
python -m service.run        # :8100
python -m gateway.run        # :8000
```

生产环境用 systemd 或 supervisor 托管三个进程：WorkingDirectory 指到
`agent-base/`、EnvironmentFile 指 `.env`（或确保环境变量已注入）、
Restart=on-failure。Docker 方式见第 2 节方式 B。

## 4.5 K8s 部署形态（内网生产目标）

内网生产部署在 K8s 集群上时，形态如下（本包镜像三进程共用，按
command 区分；配置经 ConfigMap/Secret 注入同一套 .env 变量）：

- **网关**：Deployment 可多副本，前提是 `GATEWAY_STATE_BACKEND=
  redis`（events/runs/限流在 Redis 共享、SSE 经 Pub/Sub 跨副本
  扇出、限流全局口径；双副本已实测：跨副本对话/确认/事件流、
  杀一副本会话续用）。业务后端经集群内 Service 访问 8000，无需
  会话粘性。残余边界：在途轮次的执行不迁移——执行副本死亡时
  该轮停在 running，网关读取时按超时对账置 failed，业务侧重试；
- **基座服务**：Deployment 可多副本。前提三件：`SERVICE_DB_URL`
  指 MySQL、`SERVICE_BUS=redis`、`SERVICE_WORKSPACE=k8s`；
- **工作区**：k8s 后端每工作区在 `SERVICE_K8S_NAMESPACE`（默认
  agentscope）里起一个 Pod＋一个 PVC（默认 1Gi，StorageClass 按
  集群填 `SERVICE_K8S_STORAGE_CLASS`），Pod 名/PVC 名由工作区 id
  确定性派生，任一服务副本可按名找回；空闲 1 小时 Pod 被回收、
  PVC 默认保留（文件不丢）。工作区镜像默认 `python:3.11-slim`，
  内网须预先同步到内部镜像仓并改 `SERVICE_K8S_IMAGE`（＋拉取密钥
  `SERVICE_K8S_IMAGE_PULL_SECRETS`）；
- **RBAC**：基座服务的 ServiceAccount 须在工作区命名空间有
  pods（含 pods/exec）、persistentvolumeclaims 的 get/list/watch/
  create/delete/patch/update 权限，**外加一条集群级权限**：
  工作区代码建工作区前会先 GET 命名空间对象本身确认存在，
  namespaces 是集群作用域资源、命名空间内的 Role 给不了——须
  ClusterRole 单独给 `get namespaces`（resourceNames 限定为工作
  区命名空间那一个）＋ClusterRoleBinding（2026-11-08 kind 实测
  踩到：缺这条时工作区 provision 报 403、整轮对话失败）；
  服务只应被网关访问（NetworkPolicy 收口），终端用户与业务
  页面永不直达；
- **MySQL/Redis**：集群外置或集群内自建均可，连接信息走 Secret
  注入 `SERVICE_DB_URL` 与 `SERVICE_REDIS_HOST`；
- **MCP 工具服务**：各自独立部署（Deployment＋Service），地址经
  注册表分发，不与基座同 Pod。

**集群实测（2026-11-08，InstaBox 上 kind 单节点集群，k8s
1.34）**：原定四项附加验收已全部通过——①工作区 Pod/PVC 自动
创建（对话触发 provision、关工作区删 Pod、PVC 保留）；②换
副本续聊：服务侧由 MySQL＋Redis＋工作区确定性命名支撑，工作
区层实测同一 workspace_id 换新 Pod 按名找回同一 PVC；③文件
持久：Pod 删除后重建、文件完好读回；④最小 RBAC 全链：服务以
Pod 形态 in-cluster 运行（SA＋上述权限），经网关完成一轮对话、
其间自动创建会话工作区 Pod/PVC、集群内 MCP 服务被工作区连
接。上集群另有三条实测注意事项：

- **工作区镜像首启需联网**：默认 python:3.11-slim 镜像的工作区
  Pod 首次 bootstrap 会在 Pod 内 apt-get 并从公网下载 uv 与
  agentscope 依赖（仅新 PVC 首次）。内网无外网时必须预烘焙
  工作区镜像（把 gateway venv 打进镜像）或保证 Pod 可达内网
  apt/PyPI 镜像，否则工作区起不来；
- **服务启动不等待数据库**：MySQL 未就绪时服务直接退出，靠
  K8s 重启收敛即可（CrashLoopBackOff 几次后自愈），或加
  initContainer 等待，二选一、别改装配代码；
- **空闲 TTL 回收未单独验**：关工作区删 Pod 已验；框架默认的
  空闲 1 小时回收本次未等满 1 小时验证，机制与关工作区同路。

## 5. 验证

**冒烟（管道与治理验收，与模型无关）：** 在未跑生产实例的机器/时段执行
（冒烟会占用 8000/8100/8200 并杀掉占用这些端口的进程）：

```bash
python smoke_test.py        # 全链 13 组检查，结果落 smoke_output.txt
python smoke_actions.py     # 租户隔离/组织凭证/动态注入/权限三级等检查
python smoke_async.py       # 同步/异步双模式与 events 实时流检查
python smoke_models.py      # 多模型：逐 Agent 组装/fallback 下发/带图切换/能力下限
```

三条命令退出码为 0、输出末尾 ALL PASS 即通过。冒烟自带独立 demo key 与
独立数据目录，不污染正式数据。冒烟用 `fuser` 清端口，无 psmisc 的机器
上会自动跳过该步（或 `apt-get install -y psmisc`）。

**真实模型验收：** `.env` 填好 `OPENAI_*` 后重启 service 与 gateway，
用业务后端同样的调用方式建会话、发一句话，应收到真实模型回复；
再查网关 `GET /v1/usage`（带租户 key），应出现该次调用的计量行。

## 6. 接入一个新项目（新租户）

1. `.env` 的 `PROJECT_KEYS` 追加 `新租户=新随机密钥`，重启 gateway；
2. `agent_registry/` 里按样例加该租户的 agent 配置（参照
   `tenant-a.assistant.yaml`），模型登记在 `models.yaml`；MCP 工具登记
   `mcp_servers.yaml`、技能登记 `skills.yaml`（本体放 `skills_store/`）、
   SOP 登记 `sops.yaml`，均按其中的 `tenants/scope` 字段圈定归属；
3. 改完先跑 `python agent_registry/validate_registry.py` 校验通过再重启；
4. 注册表变更对**新建会话**即时生效，老会话不热换，属预期行为。

## 7. 生产清单（试部署转正式前逐项过）

- [ ] `SERVICE_DB_URL` 已指 MySQL、`SERVICE_BUS=redis`（生产组合，
      2026-11-08 已在 InstaBox 整包实测），两个库都有备份策略
- [ ] 多实例：网关与基座服务均可多副本——网关须
      `GATEWAY_STATE_BACKEND=redis`、服务须 MySQL＋Redis 总线＋
      工作区 k8s 后端（或共享存储）；前置负载均衡无需会话粘性
- [ ] `PROJECT_KEYS` 全部为随机值；`.env` 权限 600，未入任何代码库
- [ ] 8000 只对业务后端网段开放；8100、8200 不对业务侧开放
- [ ] 模型 key 只出现在 `.env`/部署机密钥管理里，未写进注册表 yaml
- [ ] 计量库：`AGENT_BASE_USAGE_URL` 已指 MySQL（实现已换
      SQLAlchemy，多网关共写同库；2026-11-08 实测）。仍用文件
      路径（sqlite）的部署不得转生产
- [ ] 网关状态：`GATEWAY_STATE_BACKEND=redis` 已开（events/runs/
      限流外置）；仍为 memory 的部署只能单副本、重启丢事件回放，
      不得转生产
- [ ] 沙箱：当前装配用本地工作区后端（**无隔离**）。若上线后要执行
      不可信工具，按 `NOTES.md` 沙箱条把 `service/app.py` 的工作区
      管理器换成 bubblewrap 或 docker 后端（框架自带，装配选档）
- [ ] 知识库未装配（建库接口返回 503 属预期）。需要知识库时按设计
      文档第 10 章装配 KB 管理器，并先定 embedding 模型与向量库

## 8. 口径与边界速览（细节查 NOTES.md）

- **文件交付**：agent 只经对话与结构化结果交付；文件由业务侧生成与
  保管。agent 在工作区生成的文件如需给用户，走「给 agent 一个
  send_file 类工具、由它在产生时发出」的推送模式（工具按接入项目
  在注册表登记）；工作区只当可弃草稿，不做回收物流。
- **身份**：业务后端在请求头里带租户与终端用户，网关拼成基座的
  `{租户}:{终端用户}` 隔离键；基座服务必须藏在网关之后，不可直面
  不可信调用方。
- **确认**：ASK 级工具的确认由业务界面呈现、业务后端中继回网关，
  契约见设计文档第 7 章。
- **调用模式**：`POST .../messages` 与 `POST .../confirmations` 支持
  `?mode=sync|async`（默认 sync）。业务页面应使用 async：接口立即
  返回 202 与 run 号，思考过程、工具调用、确认卡、最终回复全部经
  `GET .../events?stream=true` 的实时事件流推送（先回放、后跟随），
  页面据此渲染；sync 只适合脚本与简单服务端调用。
- 框架残留（Team 工具政策性禁用、Schedule 工具默认不可见、SOP 终端
  代理未做等）的完整清单与处置在 `NOTES.md` 第 4 节；
  服务层个人平台假设的系统排查在 `docs/` 的排查清单里。

## 9. 故障排查速查

| 现象 | 先查 |
|---|---|
| 网关返回 401 / AUTH_FAILED | 请求头 `X-Project-Key` 与 `.env` 的 `PROJECT_KEYS` 是否一致 |
| service 起不来 | `SERVICE_DB_URL` 连通性与库权限；redis 模式查 `SERVICE_REDIS_HOST/PORT` |
| 建会话报 404 / 凭证错误 | 看 gateway 日志里的 injection_report（租户注入结果），多为系统户凭证供给失败 |
| 工具在会话里不出现 | 注册表该条目的 `tenants/scope` 是否含当前租户；目标 MCP 服务是否在跑 |
| 回复是固定套话 | 在走 mock 模型：`OPENAI_*` 没填或没重启进程 |
| 端口被占 | 8000/8100/8200 逐个查占用进程 |
