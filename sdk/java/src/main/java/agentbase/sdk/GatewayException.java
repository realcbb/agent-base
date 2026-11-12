package agentbase.sdk;

/**
 * 网关错误：统一错误信封 {code, message, trace_id} 的 Java 表达。
 * code 取值见《业务系统对接规范》错误码表（如 AUTH_FAILED、
 * SESSION_NOT_FOUND、MODEL_NO_VISION、QUOTA_EXCEEDED）。
 */
public class GatewayException extends RuntimeException {
    private final int status;
    private final String code;
    private final String traceId;

    public GatewayException(int status, String code, String message, String traceId) {
        super(message);
        this.status = status;
        this.code = code;
        this.traceId = traceId;
    }

    /** HTTP 状态码。 */
    public int status() {
        return status;
    }

    /** 契约错误码。 */
    public String code() {
        return code;
    }

    /** 链路号，报障时提供给平台侧定位。 */
    public String traceId() {
        return traceId;
    }

    @Override
    public String toString() {
        return "GatewayException{status=" + status + ", code=" + code
                + ", traceId=" + traceId + ", message=" + getMessage() + "}";
    }
}
