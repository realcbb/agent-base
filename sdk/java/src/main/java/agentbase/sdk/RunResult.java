package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 一轮运行的记录：{run_id, session_id, agent_id, model, status, reply, ts}。
 * status 取值：running / awaiting_confirmation / completed / failed。
 * awaiting_confirmation 表示本轮停在确认单上等人决定——用
 * Session.chat（自带确认回路）时该状态由 SDK 内部消化。
 */
public final class RunResult {
    private final JsonNode raw;

    RunResult(JsonNode raw) {
        this.raw = raw;
    }

    public String runId() {
        return text("run_id");
    }

    public String sessionId() {
        return text("session_id");
    }

    public String agentId() {
        return text("agent_id");
    }

    /** 本轮实际使用的模型名（平台侧策略决定，业务只读）。 */
    public String model() {
        return text("model");
    }

    public String status() {
        return text("status");
    }

    /** 最终回复全文（completed 时）。 */
    public String reply() {
        return text("reply");
    }

    public String ts() {
        return text("ts");
    }

    public boolean isCompleted() {
        return "completed".equals(status());
    }

    public boolean isFailed() {
        return "failed".equals(status());
    }

    public boolean isAwaitingConfirmation() {
        return "awaiting_confirmation".equals(status());
    }

    public JsonNode raw() {
        return raw;
    }

    private String text(String field) {
        JsonNode node = raw.get(field);
        return node == null || node.isNull() ? null : node.asText();
    }

    @Override
    public String toString() {
        return "RunResult{" + runId() + ", " + status() + "}";
    }
}
