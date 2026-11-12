package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 会话列表条目（GET /v1/sessions 的 sessions 一项）：某终端
 * 用户名下的会话（基座存储直接供数，已关闭的会话不在列表
 * 里）。产品级标题等字段不在内——那是业务系统自己的数据；
 * name 只是基座按首句生成的缺省名，可不用。
 */
public final class SessionInfo {
    private final JsonNode node;

    SessionInfo(JsonNode node) {
        this.node = node;
    }

    public String sessionId() {
        return text("session_id");
    }

    public String agentId() {
        return text("agent_id");
    }

    public String agentVersion() {
        return text("agent_version");
    }

    public String model() {
        return text("model");
    }

    public String createdAt() {
        return text("created_at");
    }

    public String lastActiveAt() {
        return text("last_active_at");
    }

    /** 基座自动生成的缺省会话名（按首句），可不用。 */
    public String name() {
        return text("name");
    }

    /** 基座侧运行态（如 idle/running 等原生状态串）。 */
    public String status() {
        return text("status");
    }

    public boolean isRunning() {
        JsonNode n = node.get("is_running");
        return n != null && n.asBoolean(false);
    }

    public JsonNode raw() {
        return node;
    }

    private String text(String field) {
        JsonNode n = node.get(field);
        return n == null || n.isNull() ? null : n.asText();
    }
}
