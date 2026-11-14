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
| `SERVICE_REDIS_MODE` / `GATEWAY_REDIS_MODE` | Redis 拓扑三模式：`standalone`（默认，HOST/PORT 直连）｜`sentinel`（配 `*_REDIS_SENTINELS` 地址组＋`*_REDIS_SENTINEL_MASTER` 服务名，切主自动跟随）｜`cluster`（配 `*_REDIS_CLUSTER_NODES` 启动节点组）。不写 MODE 时按地址变量推断；网关侧未设回退 `SERVICE_REDIS_*`。另 `*_REDIS_PASSWORD` 为主节点/集群密码。**限制：`SERVICE_STORAGE=redis` 不支持 cluster（框架存储有跨槽多键操作），cluster 下存储用 sql**；总线与网关状态三模式均支持 |
| 协议钉版（代码内） | 服务与网关的全部 Redis 客户端构造均钉 `protocol=2`（RESP2）：redis-py 8 默认 RESP3、每次建连先发 `HELLO 3`，平台 Redis（6.0 以前或兼容引擎）不认 HELLO 会整连失败（2026-11-13 生产实测）。**哨兵模式有个坑**：redis-py 给哨兵节点自身建连接时只继承 `socket_*` 参数，钉在 master_for 上的 protocol 管不到它——哨兵连接照旧发 HELLO、死在 discover_master（同日二轮实测）；三处 Sentinel 构造已另行补 `sentinel_kwargs={"protocol": 2, ...}`。本栈命令面（Streams/Lua/Pub/Sub/Hash）纯 RESP2 语义，钉版无功能损失；换用原生高版本 Redis 也照常工作。注：wstest 的哨兵是真 Redis 7、认 HELLO，此坑只能靠假哨兵线级测试抓 |
| `AGENT_BASE_USAGE_URL` | 计量库连接（生产：`mysql+pymysql://…`，与服务存储同 MySQL 实例即可）；未给时从 `SERVICE_DB_URL` 推导同库；`AGENT_BASE_USAGE_DB` 文件路径仅本地开发 |
| ~~`MCP_URL`~~ | 已退役（2026-11-14）：MCP 地址不再经网关 env，注册表条目的 `config` 整块直配（见第 6 节）。旧条目的 `url_env` 仍兼容，但新登记不要再加此变量 |
| `AGENT_BASE_SKILLS_URL` | 动态技能仓储连接（生产：`mysql+pymysql://…`，可与服务存储同库，表 `gw_skills`/`gw_skill_versions` 首次使用时自动建）；未给时从 `SERVICE_DB_URL` 推导。仓储不可用时动态技能层整体跳过（静态技能不受影响），发布端点回 503 |
| `GATEWAY_PLATFORM_TENANT` | 平台租户名：只有它能发布/管理 global（全租户通用）技能；未配则 global 发布关闭，本租户技能的发布不受影响 |
| `SKILL_BLOB_BACKEND` | 技能包本体后端：`db`（默认，整包 zip 存 LONGBLOB，零新增基础设施）｜`s3`（内网 S3 兼容 OSS 只存工件、索引仍在库；须配 `SKILL_OSS_BUCKET`，可选 `SKILL_OSS_ENDPOINT_URL`/`SKILL_OSS_REGION`/`SKILL_OSS_ACCESS_KEY`/`SKILL_OSS_SECRET_KEY`） |
| `GATEWAY_SKILL_UPLOAD_TIMEOUT_SECS` | 技能上传进工作区的专用超时，默认 600 秒。服务侧安装要把 tar 经 Pod exec 流式送进工作区、多次往返，不得与普通调用共用网关客户端的 30s 预算（2026-11-13 wstest 实测 30s 必爆） |

动态技能发布（2026-11-13）：业务专家经 `POST /v1/admin/skills`（multipart：zip 包＋`visibility=tenant|global`＋`injection=default|optional`）发布技能，对新建会话立即生效，免镜像、免重启；配套端点有列表、版本列表、`activate`（拨版本指针＝激活/回滚）、`disable`。技能名取包内 SKILL.md frontmatter 的 `name`，是唯一身份。语义三条：①版本不可变，每次发布是新版本，会话创建时按当时当前版本把文件快照装进工作区、之后不再变（版本钉住）；②可见性＝global＋本租户，同名覆盖序为 global 静态 < global 动态 < 本租户静态 < 本租户动态；③静态层（镜像内 skills_store＋skills.yaml）保留，与动态层合并读取。注入逐项记账，失败项会以 `[adapter] 注入失败 …` 打进网关日志（注入报告本身不进契约响应）。

活会话按需装技能（2026-11-14）：`POST /v1/sessions/{id}/skills`（body：`name`，可选 `version`）把技能装进**已存在**会话的工作区，走服务侧原生 upload 通道。default/optional 均可装——optional 的定位就是建会话不装、用时再装，技能数涨到几百个时靠这条分档避免每会话全量灌入。动态技能缺省装当前版本、`version` 可指定历史版本（不存在回 404 `SKILL_VERSION_NOT_FOUND`）；静态技能无版本、传 `version` 回 400。重复装同名：先经服务侧 DELETE 按 SKILL.md 的 name 摘除旧副本再装入（框架 add_skill_archive 对重名目录只会加数字后缀、留双份，故由网关先摘）。技能对会话租户不可见回 404 `SKILL_NOT_FOUND`（不泄露他租户技能存在性）。

技能依赖前置检查（2026-11-14）：SKILL.md frontmatter 可用 `requires` 声明直接依赖（`python: [pandas>=2.0, openpyxl]`、`mcp: [browser]` 两类），发布端点逐项核对：python 须被工作区镜像技能公共环境清单满足（清单文件 `skill_env_manifest.txt` 放注册表目录，内容从工作区镜像的 `/opt/skill-python-manifest.txt` 抽取、随 ConfigMap 挂载，网关每次现读、改完热生效），或技能包内自带兼容 wheel（Python 3.11：cp311/abi3/py3 通用轮；平台轮须覆盖的架构由网关 env `GATEWAY_SKILL_REQUIRED_ARCHES` 配置，逗号分隔、认 arm64/amd64 别名，默认 `aarch64`，要双架构配 `aarch64,x86_64`）；mcp 须在注册表中对发布租户可见。超过技能包体积上限（zip 10MB）的大依赖、以及只有源码包没有现成 wheel 的依赖，不走技能包：联系平台加进工作区镜像的技能公共环境层（重打镜像并同步清单），别让专家自己编译。镜像已满足约束又自带 wheel 的，发布响应以 `dependency_check.redundant` 提示多余（不拦截）。不满足回 422 `SKILL_DEPS_UNSATISFIED` 并逐项报明原因，坏包不落库。清单文件缺失时 python 的镜像比对无从核对，未被 wheel 满足的项记 unverified 放行，发布响应以 `dependency_check: {manifest: "missing", unverified: [...]}` 显形，清单就位后自动转严格。声明本身不合法（未知类别、环境标记等）回 422 `SKILL_PACKAGE_INVALID`。activate/disable 不复检（版本在发布时已按当时清单核过）。

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
  `SERVICE_K8S_IMAGE_PULL_SECRETS`）；工作区 Pod 身份/资源/节点池
  经 `SERVICE_K8S_SERVICE_ACCOUNT`、`SERVICE_K8S_RESOURCES`（JSON）、
  `SERVICE_K8S_NODE_SELECTOR`/`SERVICE_K8S_TOLERATIONS`（JSON）透传
  管理器。**共享命名空间部署**（与业务容器同命名空间、无法新建
  命名空间时）：SERVICE_K8S_NAMESPACE 填业务命名空间名；
  SERVICE_ACCOUNT 必配专用零权限账号（default 账号不得继承）；
  命名空间级 ResourceQuota/LimitRange/default SA 设置一律不要
  随本系统 apply（会波及业务容器），工作区出入站策略靠 Pod
  自带标签 `agentscope.workspace=true` 精确收口；
  **技能落盘补丁**（装配层，框架本体不动）：框架 write_stream 以
  空帧当 EOF 喂 `cat` 的写法在现行 kubelet 下死锁（2026-11-14
  kind 与宿主机直跑 k3s 同签名实测——包完整落盘、cat 永不退
  出），装配层已换成服务侧缓冲＋`head -c <字节数>` 定长读，
  上线前在目标集群建一个带技能的会话验落盘即可；
- **RBAC**：基座服务的 ServiceAccount 只需命名空间内的 Role：
  pods（含 pods/exec）、persistentvolumeclaims 的 get/list/watch/
  create/patch/update（PVC 无 delete——装配 delete_pvc_on_close
  =False、卷销毁走人工）。**不需要任何集群级权限**：框架建工作
  区前会先 read_namespace 确认命名空间存在（namespaces 是集群
  作用域、Role 给不了，缺 ClusterRole 时该步 403 会让 provision
  整轮失败，2026-11-08 kind 实测、2026-11-13 wstest 对照复现）；
  共享命名空间场景下 ClusterRole 往往申请不了，装配层已适配：
  `service/app.py` 的 `_patch_k8s_namespace_ensure` 把该步换成
  容忍 403 的版本（命名空间由业务预置、本服务就运行其中，403
  时按部署约定放行并记警告；404 等其余错误照旧抛；永不
  create_namespace）。生产 YAML 因此不含 ClusterRole/Binding；
  服务只应被网关访问（NetworkPolicy 收口），终端用户与业务
  页面永不直达；
- **MySQL/Redis**：集群外置或集群内自建均可，连接信息走 Secret
  注入 `SERVICE_DB_URL` 与 `SERVICE_REDIS_HOST`；
- **MCP 工具服务**：各自独立部署（Deployment＋Service）；连接配置
  （地址/headers/timeout）整块写在注册表条目的 `config` 里随
  ConfigMap 分发，不经网关 env、不与基座同 Pod。

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

注：网关支持 `AGENT_REGISTRY_DIR` 把注册表整体外置（K8s 下用
ConfigMap 挂载到 `/config/registry`，见生产 YAML 的
`agentbase-registry-configmap.yaml`）：外置后改 models/agent/
MCP 登记只需更新 ConfigMap 并滚动重启网关，不必重出镜像；
skills 实体（`skills_store/`）仍随镜像发布。MCP 登记的
`config` 字段是框架 mcp_config 的完整 JSON（http_mcp：
`{type, url, headers?, timeout?}`；stdio_mcp：`{type, command,
args?, env?, cwd?}`），网关建会话时原样透传给基座——加一个
MCP 就是往 `mcp_servers.yaml` 加一条带 config 的登记，
不再需要给网关配任何 env。

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
