package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * 通用 AI 基座网关客户端（契约 v1）。
 *
 * <pre>{@code
 * AgentBaseClient client = AgentBaseClient.builder()
 *         .baseUrl("http://gateway.internal:8000")
 *         .projectKey("……")
 *         .build();
 * Session session = client.createSession("kc.assistant", "E9001");
 * RunResult result = session.chat(MessageRequest.text("你好"));
 * }</pre>
 *
 * 线程安全：客户端可多线程共享；Session 句柄的方法应由业务侧
 * 按会话串行调用（契约口径：同一会话同时只跑一轮）。
 */
public final class AgentBaseClient {
    static final ObjectMapper MAPPER = new ObjectMapper();

    private final String baseUrl;
    private final String projectKey;
    private final HttpClient http;
    private final Duration requestTimeout;
    private final ConfirmationHandler defaultConfirmationHandler;
    private final boolean endUserProof;

    private AgentBaseClient(Builder builder) {
        this.baseUrl = builder.baseUrl;
        this.projectKey = builder.projectKey;
        this.requestTimeout = builder.requestTimeout;
        this.defaultConfirmationHandler = builder.confirmationHandler;
        this.endUserProof = builder.endUserProof;
        this.http = builder.httpClient != null
                ? builder.httpClient
                : HttpClient.newBuilder()
                        .connectTimeout(builder.connectTimeout)
                        .build();
    }

    public static Builder builder() {
        return new Builder();
    }

    public static final class Builder {
        private String baseUrl;
        private String projectKey;
        private Duration connectTimeout = Duration.ofSeconds(10);
        private Duration requestTimeout = Duration.ofSeconds(180);
        private HttpClient httpClient;
        private ConfirmationHandler confirmationHandler;
        private boolean endUserProof;

        /** 网关地址，如 http://gateway:8000（不带尾斜杠亦可）。 */
        public Builder baseUrl(String baseUrl) {
            this.baseUrl = baseUrl;
            return this;
        }

        /** 平台发放的项目密钥（X-Project-Key）。 */
        public Builder projectKey(String projectKey) {
            this.projectKey = projectKey;
            return this;
        }

        public Builder connectTimeout(Duration timeout) {
            this.connectTimeout = timeout;
            return this;
        }

        /** 同步调用的读超时（默认 180 秒，须大于网关单轮超时）。 */
        public Builder requestTimeout(Duration timeout) {
            this.requestTimeout = timeout;
            return this;
        }

        /** 注入自定义 HttpClient（如需代理/证书配置）时用。 */
        public Builder httpClient(HttpClient httpClient) {
            this.httpClient = httpClient;
            return this;
        }

        /** 默认确认处理器，Session 级可覆盖。 */
        public Builder confirmationHandler(ConfirmationHandler handler) {
            this.confirmationHandler = handler;
            return this;
        }

        /**
         * 终端身份断言：开启后，凡带终端用户标识的请求自动附带
         * X-End-User-Ts 与 X-End-User-Proof（HMAC_SHA256(项目密钥,
         * "{工号}.{时间戳}")）。网关开启断言校验的部署必填开，
         * 否则调用会被 401 USER_PROOF_* 拒绝。
         */
        public Builder endUserProof(boolean endUserProof) {
            this.endUserProof = endUserProof;
            return this;
        }

        public AgentBaseClient build() {
            if (baseUrl == null || baseUrl.isBlank()) {
                throw new IllegalArgumentException("baseUrl 必填");
            }
            if (projectKey == null || projectKey.isBlank()) {
                throw new IllegalArgumentException("projectKey 必填");
            }
            if (baseUrl.endsWith("/")) {
                baseUrl = baseUrl.substring(0, baseUrl.length() - 1);
            }
            return new AgentBaseClient(this);
        }
    }

    /** 健康检查（不需要密钥）：返回原文 JSON（含当前模型名）。 */
    public JsonNode health() {
        return request("GET", "/health", null, null);
    }

    /** 本租户被分配的 Agent 清单。 */
    public List<AgentInfo> listAgents() {
        JsonNode body = request("GET", "/v1/agents", null, null);
        List<AgentInfo> agents = new ArrayList<>();
        JsonNode arr = body.get("agents");
        if (arr != null && arr.isArray()) {
            arr.forEach(n -> agents.add(new AgentInfo(n)));
        }
        return agents;
    }

    /**
     * 开一个会话。endUserId 是终端用户在业务系统内的唯一标识
     * （如工号），同一会话后续所有调用必须同值。
     */
    public Session createSession(String agentId, String endUserId) {
        var body = JsonNodeFactory.instance.objectNode();
        body.put("agent_id", agentId);
        JsonNode created = request("POST", "/v1/sessions", endUserId, body);
        return new Session(
                this,
                created.get("session_id").asText(),
                created.get("agent_id").asText(),
                endUserId);
    }

    /**
     * 恢复一个已存在的会话句柄（业务进程重启后续聊）：会话与
     * 历史在基座侧，句柄只是路由信息。sessionId 是基座的会话
     * id（建会话时返回、业务侧应落库），agentId 与 endUserId
     * 必须与建会话时一致，对不上后续调用按会话不存在处理。
     */
    public Session resumeSession(String sessionId, String agentId, String endUserId) {
        return new Session(this, sessionId, agentId, endUserId);
    }

    /** 查一轮运行的记录（在途轮次可轮询其状态推进）。 */
    public RunResult getRun(String runId) {
        return new RunResult(request("GET", "/v1/runs/" + runId, null, null));
    }

    /**
     * 某终端用户的会话列表（基座建过的会话元数据，新→旧）。
     * agentId 为 null 表示不限定 Agent。用户隔天回来找回旧
     * 会话靠这个，业务侧不必自建会话索引。
     */
    public List<SessionInfo> listSessions(String agentId, String endUserId) {
        String path = "/v1/sessions" + (agentId == null
                ? ""
                : "?agent_id=" + java.net.URLEncoder.encode(
                        agentId, java.nio.charset.StandardCharsets.UTF_8));
        JsonNode body = request("GET", path, endUserId, null);
        List<SessionInfo> out = new ArrayList<>();
        JsonNode arr = body.get("sessions");
        if (arr != null && arr.isArray()) {
            arr.forEach(n -> out.add(new SessionInfo(n)));
        }
        return out;
    }

    /** 本租户用量聚合（原文 JSON：calls/runs/input_tokens/output_tokens 等）。 */
    public JsonNode usage() {
        return request("GET", "/v1/usage", null, null);
    }

    /**
     * 本租户审计账（原文 JSON：events 数组，新→旧）：会话创建/
     * 归档、确认决定（谁、何时、对哪张卡、什么决定）。只读，
     * 数据在网关台账库，独立于运行日志。
     */
    public JsonNode audit(int limit) {
        return request("GET", "/v1/audit?limit=" + limit, null, null);
    }

    // ------------------------------------------------------------ 内部 plumbing

    HttpClient http() {
        return http;
    }

    String baseUrl() {
        return baseUrl;
    }

    Duration requestTimeout() {
        return requestTimeout;
    }

    ConfirmationHandler defaultConfirmationHandler() {
        return defaultConfirmationHandler;
    }

    HttpRequest.Builder requestBuilder(String path, String endUserId) {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + path))
                .header("X-Project-Key", projectKey)
                .header("Accept", "application/json");
        if (endUserId != null) {
            builder.header("X-End-User-Id", endUserId);
            if (endUserProof) {
                String ts = Long.toString(System.currentTimeMillis() / 1000);
                builder.header("X-End-User-Ts", ts);
                builder.header(
                        "X-End-User-Proof", proof(endUserId + "." + ts));
            }
        }
        return builder;
    }

    private String proof(String message) {
        try {
            javax.crypto.Mac mac = javax.crypto.Mac.getInstance("HmacSHA256");
            mac.init(new javax.crypto.spec.SecretKeySpec(
                    projectKey.getBytes(java.nio.charset.StandardCharsets.UTF_8),
                    "HmacSHA256"));
            byte[] raw = mac.doFinal(
                    message.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(raw);
        } catch (java.security.GeneralSecurityException e) {
            throw new IllegalStateException("HmacSHA256 不可用", e);
        }
    }

    /**
     * 发一个 JSON 请求并解析响应；非 2xx 一律映射为 GatewayException
     * （契约错误信封 {code, message, trace_id}）。
     */
    JsonNode request(String method, String path, String endUserId, JsonNode body) {
        HttpRequest.Builder builder = requestBuilder(path, endUserId)
                .timeout(requestTimeout);
        if (body == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            builder.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(body.toString()));
        }
        HttpResponse<String> response;
        try {
            response = http.send(builder.build(),
                    HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            throw new GatewayException(0, "SDK_NETWORK", "网关不可达：" + e.getMessage(), null);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new GatewayException(0, "SDK_INTERRUPTED", "调用被中断", null);
        }
        return parseResponse(response.statusCode(), response.body());
    }

    static JsonNode parseResponse(int status, String bodyText) {
        JsonNode parsed = null;
        if (bodyText != null && !bodyText.isBlank()) {
            try {
                parsed = MAPPER.readTree(bodyText);
            } catch (IOException ignored) {
                // 非 JSON 响应：按原文兜底成错误信息
            }
        }
        if (status >= 200 && status < 300) {
            return parsed == null ? JsonNodeFactory.instance.objectNode() : parsed;
        }
        String code = "INTERNAL";
        String message = bodyText == null ? "" : bodyText;
        String traceId = null;
        if (parsed != null) {
            if (parsed.hasNonNull("code")) {
                code = parsed.get("code").asText();
            } else if (parsed.has("detail")) {
                code = "VALIDATION";
            }
            if (parsed.hasNonNull("message")) {
                message = parsed.get("message").asText();
            }
            if (parsed.hasNonNull("trace_id")) {
                traceId = parsed.get("trace_id").asText();
            }
        }
        throw new GatewayException(status, code, message, traceId);
    }
}
