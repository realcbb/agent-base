package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 展示历史条目：基座按会话投影出的可渲染条目
 * （GET /v1/sessions/{id}/history 的 items 一项）。
 *
 * kind 三种：
 *   user         —— 用户消息（text 为原文，image 表示带图）
 *   confirmation —— 确认卡（toolCalls/impact 为卡面内容，
 *                   status 为 pending/approved/rejected/edited）
 *   assistant    —— 助手回复（text 为回复全文，runId 为轮次）
 */
public final class HistoryItem {
    private final JsonNode node;

    HistoryItem(JsonNode node) {
        this.node = node;
    }

    public String kind() {
        return text("kind");
    }

    public String text() {
        return text("text");
    }

    public boolean image() {
        JsonNode n = node.get("image");
        return n != null && n.asBoolean(false);
    }

    public String runId() {
        return text("run_id");
    }

    public String confirmationId() {
        return text("confirmation_id");
    }

    /** 确认卡的工具调用原文（JSON 数组，未定型直出）。 */
    public JsonNode toolCalls() {
        return node.get("tool_calls");
    }

    public String impact() {
        return text("impact");
    }

    /** confirmation 条目是决定状态；assistant 条目是轮次状态。 */
    public String status() {
        return text("status");
    }

    public String ts() {
        return text("ts");
    }

    public JsonNode raw() {
        return node;
    }

    private String text(String field) {
        JsonNode n = node.get(field);
        return n == null || n.isNull() ? null : n.asText();
    }
}
