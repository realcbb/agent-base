package agentbase.sdk;

/**
 * 确认处理器：Session.chat 在撞上 ASK 级工具时回调。
 * 在业务系统里，这里通常桥接到真实的人工确认界面（把确认单
 * 推给用户、等其点完再返回）；自动化的联调/批处理场景也可
 * 按规则直接裁决。返回 null 按 rejected 处理。
 */
@FunctionalInterface
public interface ConfirmationHandler {
    Decision decide(ConfirmationRequest request);
}
