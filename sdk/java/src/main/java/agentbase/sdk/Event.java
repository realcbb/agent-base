package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 契约事件：{event_id, session_id, run_id, type, ts, payload}。
 * type 全集见对接规范事件类型表（run.started / message.delta /
 * tool.calling / tool.result / confirmation.required /
 * model.switched / run.completed / run.failed）。
 * 未知 type 应忽略而非报错（规范兼容承诺）。
 */
public final class Event {
    private final JsonNode raw;

    Event(JsonNode raw) {
        this.raw = raw;
    }

    public String eventId() {
        return text("event_id");
    }

    public String sessionId() {
        return text("session_id");
    }

    public String runId() {
        return text("run_id");
    }

    public String type() {
        return text("type");
    }

    public String ts() {
        return text("ts");
    }

    /** 事件负载原文（结构随 type 而定，见规范）。 */
    public JsonNode payload() {
        JsonNode payload = raw.get("payload");
        return payload == null
                ? com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode()
                : payload;
    }

    /** type == confirmation.required 时的确认单视图，其余情况为 null。 */
    public ConfirmationRequest asConfirmation() {
        if (!"confirmation.required".equals(type())) {
            return null;
        }
        return new ConfirmationRequest(payload());
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
        return "Event{" + type() + ", run=" + runId() + ", id=" + eventId() + "}";
    }
}
