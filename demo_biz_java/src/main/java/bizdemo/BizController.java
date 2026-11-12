package bizdemo;

import agentbase.sdk.AgentBaseClient;
import agentbase.sdk.AgentInfo;
import agentbase.sdk.Decision;
import agentbase.sdk.GatewayException;
import agentbase.sdk.ImageInput;
import agentbase.sdk.MessageRequest;
import agentbase.sdk.RunResult;
import agentbase.sdk.Session;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.List;
import java.util.UUID;

/**
 * 业务后端接口：页面只调这里；这里用官方 SDK 调基座。
 * 项目密钥只出现在后端（SDK 客户端里），页面拿不到。
 */
@RestController
public class BizController {

    private final AgentBaseClient client;
    private final SessionHub hub;

    @Value("${biz.end-user:E9001}")
    private String endUser;

    public BizController(AgentBaseClient client, SessionHub hub) {
        this.client = client;
        this.hub = hub;
    }

    private String browserId(String cookie, HttpServletResponse response) {
        if (cookie != null && !cookie.isBlank()) {
            return cookie;
        }
        String id = UUID.randomUUID().toString().replace("-", "");
        Cookie c = new Cookie("biz_sid", id);
        c.setPath("/");
        c.setMaxAge(7 * 24 * 3600);
        response.addCookie(c);
        return id;
    }

    /** 基座错误原样透传错误码给页面（业务后端不吞错）。 */
    @ExceptionHandler(GatewayException.class)
    public ResponseEntity<JsonNode> gatewayError(GatewayException e) {
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        node.put("code", e.code());
        node.put("message", e.getMessage());
        if (e.traceId() != null) {
            node.put("trace_id", e.traceId());
        }
        int status = e.status() > 0 ? e.status() : 502;
        return ResponseEntity.status(status).body(node);
    }

    @GetMapping("/api/me")
    public JsonNode me() {
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        node.put("end_user", endUser);
        node.put("name", "王经理（模拟登录）");
        return node;
    }

    @GetMapping("/api/agents")
    public JsonNode agents() {
        List<AgentInfo> agents = client.listAgents();
        ObjectNode root = JsonNodeFactory.instance.objectNode();
        var arr = root.putArray("agents");
        for (AgentInfo a : agents) {
            arr.add(a.raw());
        }
        root.put("total", agents.size());
        return root;
    }

    @PostMapping("/api/session")
    public JsonNode newSession(
            @RequestBody JsonNode body,
            @CookieValue(value = "biz_sid", required = false) String cookie,
            HttpServletResponse response) {
        String browser = browserId(cookie, response);
        Session session = hub.freshSession(
                browser, body.get("agent_id").asText(), endUser);
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        node.put("session_id", session.sessionId());
        return node;
    }

    @PostMapping("/api/chat")
    public JsonNode chat(
            @RequestBody JsonNode body,
            @CookieValue(value = "biz_sid", required = false) String cookie,
            HttpServletResponse response) {
        String browser = browserId(cookie, response);
        Session session = hub.ensureSession(
                browser, body.get("agent_id").asText(), endUser);
        MessageRequest.Builder builder = MessageRequest.builder()
                .text(body.path("text").asText(""));
        JsonNode image = body.get("image");
        if (image != null && !image.isNull()) {
            String mediaType = image.path("media_type").asText("image/png");
            if (image.hasNonNull("data_base64")) {
                builder.addImage(ImageInput.base64(
                        image.get("data_base64").asText(), mediaType));
            } else if (image.hasNonNull("url")) {
                builder.addImage(ImageInput.url(
                        image.get("url").asText(), mediaType));
            }
        }
        RunResult run = session.sendAsync(builder.build());
        return run.raw();
    }

    @PostMapping("/api/confirm")
    public JsonNode confirm(@RequestBody JsonNode body) {
        Session session = hub.session(body.get("session_id").asText());
        if (session == null) {
            throw new GatewayException(404, "SESSION_NOT_FOUND",
                    "业务后端没有这个会话（可能重启过）", null);
        }
        String decisionText = body.get("decision").asText();
        Decision decision = switch (decisionText) {
            case "approved" -> Decision.approved();
            case "edited" -> Decision.edited(
                    body.path("edited_input").asText(null));
            default -> Decision.rejected();
        };
        RunResult run = session.confirmAsync(
                body.get("confirmation_id").asText(), decision);
        return run.raw();
    }

    /** 页面打开时恢复展示历史：后端向基座取投影，不自存。 */
    @GetMapping("/api/history")
    public JsonNode history(@RequestParam("session_id") String sessionId) {
        ObjectNode root = JsonNodeFactory.instance.objectNode();
        ArrayNode arr = root.putArray("items");
        hub.historyItems(sessionId).forEach(arr::add);
        return root;
    }

    // ---------------------------------------------------------------
    // GET 版动作接口（演示传输层适配）：InstaBox 公网端口网关目前
    // 只放行 GET（POST 请求体会被挂起，实测含极简回声服务亦然），
    // 故页面经公网访问时改用 GET 传参。真实业务部署（内网直连
    // 后端）应使用上面的 POST 版，语义与参数完全一致。
    // ---------------------------------------------------------------

    private static final String SAMPLE_PNG_B64 =
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                    + "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=";

    /** 页面初始化用：取本浏览器在该 Agent 下的会话（没有才建），不换新。 */
    @GetMapping("/api/session-ensure")
    public JsonNode ensureSessionGet(
            @RequestParam("agent_id") String agentId,
            @CookieValue(value = "biz_sid", required = false) String cookie,
            HttpServletResponse response) {
        String browser = browserId(cookie, response);
        Session session = hub.ensureSession(browser, agentId, endUser);
        ObjectNode node = JsonNodeFactory.instance.objectNode();
        node.put("session_id", session.sessionId());
        return node;
    }

    @GetMapping("/api/session-get")
    public JsonNode newSessionGet(
            @RequestParam("agent_id") String agentId,
            @CookieValue(value = "biz_sid", required = false) String cookie,
            HttpServletResponse response) {
        ObjectNode body = JsonNodeFactory.instance.objectNode();
        body.put("agent_id", agentId);
        return newSession(body, cookie, response);
    }

    @GetMapping("/api/chat-get")
    public JsonNode chatGet(
            @RequestParam("agent_id") String agentId,
            @RequestParam(value = "text", defaultValue = "") String text,
            @RequestParam(value = "sample_image", defaultValue = "0") int sampleImage,
            @CookieValue(value = "biz_sid", required = false) String cookie,
            HttpServletResponse response) {
        ObjectNode body = JsonNodeFactory.instance.objectNode();
        body.put("agent_id", agentId);
        body.put("text", text);
        if (sampleImage == 1) {
            ObjectNode image = body.putObject("image");
            image.put("data_base64", SAMPLE_PNG_B64);
            image.put("media_type", "image/png");
        }
        return chat(body, cookie, response);
    }

    @GetMapping("/api/confirm-get")
    public JsonNode confirmGet(
            @RequestParam("session_id") String sessionId,
            @RequestParam("confirmation_id") String confirmationId,
            @RequestParam("decision") String decision) {
        ObjectNode body = JsonNodeFactory.instance.objectNode();
        body.put("session_id", sessionId);
        body.put("confirmation_id", confirmationId);
        body.put("decision", decision);
        return confirm(body);
    }

    @GetMapping("/api/runs/{runId}")
    public JsonNode run(@PathVariable String runId) {
        return client.getRun(runId).raw();
    }

    @GetMapping(value = "/api/events/{sessionId}",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter events(
            @PathVariable String sessionId,
            @RequestHeader(value = "Last-Event-ID", required = false)
            String lastEventId,
            HttpServletRequest request) {
        SseEmitter emitter = new SseEmitter(0L);
        hub.attach(sessionId, emitter, lastEventId);
        emitter.onCompletion(() -> hub.detach(sessionId, emitter));
        emitter.onTimeout(() -> hub.detach(sessionId, emitter));
        return emitter;
    }
}
