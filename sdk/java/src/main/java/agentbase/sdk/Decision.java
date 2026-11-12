package agentbase.sdk;

/**
 * 对确认单的决定。两种形状：
 * <ul>
 *   <li>整批决定：approved 放行、rejected 拒绝、edited 改参后放行
 *       （editedInput 为修改后的工具入参 JSON 文本），套给卡上
 *       全部工具调用；</li>
 *   <li>逐调用决定：{@link #perCall(java.util.List)}，卡上有多个
 *       工具调用时逐条表决，未覆盖的调用由网关默认拒绝。</li>
 * </ul>
 */
public final class Decision {
    private final String decision;
    private final String editedInput;
    private final java.util.List<CallDecision> calls;

    private Decision(String decision, String editedInput) {
        this.decision = decision;
        this.editedInput = editedInput;
        this.calls = null;
    }

    private Decision(java.util.List<CallDecision> calls) {
        this.decision = null;
        this.editedInput = null;
        this.calls = calls;
    }

    public static Decision approved() {
        return new Decision("approved", null);
    }

    public static Decision rejected() {
        return new Decision("rejected", null);
    }

    public static Decision edited(String editedInputJson) {
        return new Decision("edited", editedInputJson);
    }

    public static Decision perCall(java.util.List<CallDecision> calls) {
        if (calls == null || calls.isEmpty()) {
            throw new IllegalArgumentException("calls 不能为空");
        }
        return new Decision(java.util.List.copyOf(calls));
    }

    public String decision() {
        return decision;
    }

    public String editedInput() {
        return editedInput;
    }

    /** 逐调用条目；整批决定时为 null。 */
    public java.util.List<CallDecision> calls() {
        return calls;
    }
}
