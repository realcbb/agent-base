package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.List;

/**
 * 确认单：ASK 级工具执行前，平台经 confirmation.required 事件
 * 下发给业务侧的待决事项。业务界面据此渲染确认卡，用户决定后
 * 经 Session.confirm 回传。
 */
public final class ConfirmationRequest {
    private final JsonNode payload;

    ConfirmationRequest(JsonNode payload) {
        this.payload = payload;
    }

    /** 确认号，confirm 时回传。 */
    public String confirmationId() {
        JsonNode node = payload.get("confirmation_id");
        return node == null ? null : node.asText();
    }

    /** 影响说明（平台给出的风险提示文案，可直接展示）。 */
    public String impact() {
        JsonNode node = payload.get("impact");
        return node == null ? null : node.asText();
    }

    /** 待确认的工具调用原文列表：每项含 name 与 input(JSON)。 */
    public List<JsonNode> toolCalls() {
        List<JsonNode> calls = new ArrayList<>();
        JsonNode arr = payload.get("tool_calls");
        if (arr != null && arr.isArray()) {
            arr.forEach(calls::add);
        }
        return calls;
    }

    public JsonNode raw() {
        return payload;
    }
}
