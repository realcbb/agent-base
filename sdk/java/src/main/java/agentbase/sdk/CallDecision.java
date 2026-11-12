package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * 逐调用决定条目：一张确认卡上有多个工具调用时逐条表决
 * （批 A 拒 B）。toolCallId 取自 ConfirmationRequest.toolCalls()
 * 每项的 "id" 字段；rules 为权限规则（JSON 数组节点），SDK 不
 * 解释其内容、原样透传网关再转框架，可用于"同类调用以后直接
 * 放行"这类记住选择。
 */
public final class CallDecision {
    private final String toolCallId;
    private final String decision;
    private final String editedInput;
    private JsonNode rules;

    private CallDecision(String toolCallId, String decision, String editedInput) {
        this.toolCallId = toolCallId;
        this.decision = decision;
        this.editedInput = editedInput;
    }

    public static CallDecision approved(String toolCallId) {
        return new CallDecision(toolCallId, "approved", null);
    }

    public static CallDecision rejected(String toolCallId) {
        return new CallDecision(toolCallId, "rejected", null);
    }

    public static CallDecision edited(String toolCallId, String editedInputJson) {
        return new CallDecision(toolCallId, "edited", editedInputJson);
    }

    /** 附带权限规则（JSON 数组），返回自身以便链式调用。 */
    public CallDecision withRules(JsonNode rules) {
        this.rules = rules;
        return this;
    }

    public String toolCallId() {
        return toolCallId;
    }

    public String decision() {
        return decision;
    }

    public String editedInput() {
        return editedInput;
    }

    public JsonNode rules() {
        return rules;
    }
}
