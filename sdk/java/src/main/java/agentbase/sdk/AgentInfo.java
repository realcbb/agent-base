package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

/** 本租户可见的一个 Agent（GET /v1/agents 的条目）。 */
public final class AgentInfo {
    private final JsonNode raw;

    AgentInfo(JsonNode raw) {
        this.raw = raw;
    }

    public String id() {
        return text("id");
    }

    public String displayName() {
        return text("display_name");
    }

    public int version() {
        JsonNode node = raw.get("version");
        return node == null ? 0 : node.asInt();
    }

    /** 该 Agent 登记的主模型名（仅供展示，运行时策略平台决定）。 */
    public String model() {
        return text("model");
    }

    public JsonNode raw() {
        return raw;
    }

    private String text(String field) {
        JsonNode node = raw.get(field);
        return node == null || node.isNull() ? null : node.asText();
    }
}
