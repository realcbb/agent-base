# agent-base Java SDK

通用 AI 基座网关契约 v1 的官方 Java 客户端。业务系统是 Java 时用它接入，不必手写 HTTP、事件流解析和确认回路。

- 坐标：`agentbase:agentbase-java-sdk:1.0.0`（`mvn package` 构建，或直接引用随部署包的预编译 jar）
- 运行时：JDK 17+；唯一依赖 Jackson（`jackson-databind`）
- 契约：与《业务系统对接规范 v1》一一对应，错误统一抛 `GatewayException`（含 status、契约错误码、traceId）

## 快速开始

```java
AgentBaseClient client = AgentBaseClient.builder()
        .baseUrl("http://gateway:8000")
        .projectKey("平台发放的项目密钥")
        .confirmationHandler(request -> {
            // 把确认单桥接到你们自己的确认界面，等用户决定后返回
            return Decision.approved();
        })
        .build();

Session session = client.createSession("kc.assistant", "E9001"); // agent_id、终端用户工号
RunResult result = session.chat(MessageRequest.text("帮我查一下这个客户的最新动态"));
System.out.println(result.reply());
```

## 三种用法

- **`chat(...)`（推荐）**：一站式——发消息、订事件流、撞上 ASK 级工具时回调确认处理器、续跑到本轮结束。写工具的确认回路不用业务侧自己拼。
- **`send(...)` / `sendAsync(...)`**：同步/异步的原始一轮调用。同步返回 `awaiting_confirmation` 时，需要业务侧自己走事件流 + `confirm(...)`。
- **`subscribe(...)`**：页面要实时展示思考/工具过程时，订阅会话事件流（先回放后跟随，断线按事件序号自动补订，不丢事件）。

## 要点

- **确认处理器**：未配置时确认单一律按拒绝处理——SDK 不会静默放行写操作。处理器可以按确认单内容（工具名、入参、影响说明）做规则裁决或桥接人工界面。
- **逐调用决定**：确认卡上有多个工具调用时，用 `Decision.perCall(List.of(CallDecision.approved(tcId), CallDecision.rejected(otherId)))` 逐条表决（id 取自 `ConfirmationRequest.toolCalls()` 每项的 `id`）；`CallDecision.withRules(JsonNode)` 可附带权限规则透传框架（如"同类以后放行"）。未覆盖的调用网关默认拒绝。
- **终端身份断言**：网关开启断言校验的部署，客户端须 `.endUserProof(true)`——SDK 自动为带工号的请求附带断言签名（HMAC-SHA256，见对接规范 2.1）；不开则被 401 `USER_PROOF_*` 拒绝。网关未开启时打开无副作用。
- **审计**：`client.audit(limit)` 读本租户审计账（会话创建/归档、确认决定），原文 JSON。
- **带图消息**：`MessageRequest.builder().addImage(ImageInput.url(...))` 或 `ImageInput.base64(...)`。会话是否切到识图模型由平台策略决定，业务无感；Agent 未配识图模型时抛 `MODEL_NO_VISION`。
- **会话恢复**：会话与历史在基座侧。业务进程重启后用落库的 session_id 调 `client.resumeSession(sessionId, agentId, endUserId)` 续聊；连 session_id 都没存时，用 `client.listSessions(agentId, endUserId)` 按用户查回其会话列表（元数据，新→旧）。
- **展示历史**：`session.history()` 返回基座投影的条目流（user/confirmation/assistant，含确认卡的决定状态），页面重开直接渲染，业务侧无需自存会话历史。
- **线程**：客户端可多线程共享；同一会话的调用网关侧自动排队串行（等待超时才报 `SESSION_BUSY`），业务侧不必自建串行锁。

## 自检

`src/test/java/agentbase/sdk/SdkSmoke.java` 是可运行的集成冒烟（对本地栈跑全链：对话、确认落盘、逐调用决定、同会话排队、异步轮询、事件回放、带图切模型、错误码映射、历史/列表/审计）：

```
mvn package
java -cp target/classes:target/test-classes:<jackson jars> \
  agentbase.sdk.SdkSmoke <网关地址> <项目密钥> <笔记文件路径> [proof-on]
```

第 4 参数传 `proof-on` 时额外严格检查断言负例（无断言客户端被拒），
用于网关已开启断言校验的部署验收。
