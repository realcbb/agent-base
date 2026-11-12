package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/**
 * 一个会话句柄。会话与对话历史在基座侧，句柄只是路由信息；
 * 业务进程重启后可用同一 session_id 重新构造句柄续聊
 * （见 AgentBaseClient 的 resumeSession 用法或直接 new 时的
 * 内部路径——本 SDK 以 createSession 为主入口，续聊场景可把
 * session_id 存业务库、需要时经 client.resumeSession 恢复）。
 */
public final class Session {
    private final AgentBaseClient client;
    private final String sessionId;
    private final String agentId;
    private final String endUserId;
    private ConfirmationHandler confirmationHandler;

    Session(AgentBaseClient client, String sessionId, String agentId, String endUserId) {
        this.client = client;
        this.sessionId = sessionId;
        this.agentId = agentId;
        this.endUserId = endUserId;
    }

    public String sessionId() {
        return sessionId;
    }

    public String agentId() {
        return agentId;
    }

    public String endUserId() {
        return endUserId;
    }

    /** 覆盖客户端默认确认处理器（只作用于本会话）。 */
    public Session setConfirmationHandler(ConfirmationHandler handler) {
        this.confirmationHandler = handler;
        return this;
    }

    // ------------------------------------------------------------ 发消息

    /**
     * 同步发一轮：阻塞到本轮收口或停在确认单上。
     * 返回 status=awaiting_confirmation 表示撞上 ASK 级工具——
     * 此时应走事件流拿确认单并 confirm；想省事直接用 chat()。
     */
    public RunResult send(MessageRequest request) {
        JsonNode body = client.request(
                "POST", "/v1/sessions/" + sessionId + "/messages?mode=sync",
                endUserId, request.toJson());
        return new RunResult(body);
    }

    /** 异步发一轮：立即返回 run 句柄（status=running），过程看事件流。 */
    public RunResult sendAsync(MessageRequest request) {
        JsonNode body = client.request(
                "POST", "/v1/sessions/" + sessionId + "/messages?mode=async",
                endUserId, request.toJson());
        return new RunResult(body);
    }

    /**
     * 一站式对话：发消息 → 订阅事件流 → 撞上确认单时回调确认
     * 处理器 → 续跑 → 直到本轮 completed/failed 返回最终记录。
     * 确认处理器未配置时，确认单一律按 rejected 处理（不会
     * 静默放行写工具）。
     */
    public RunResult chat(MessageRequest request) {
        return chat(request, Duration.ofSeconds(300));
    }

    public RunResult chat(MessageRequest request, Duration timeout) {
        RunResult first = sendAsync(request);
        String currentRunId = first.runId();
        BlockingQueue<Event> queue = new LinkedBlockingQueue<>();
        long deadline = System.nanoTime() + timeout.toNanos();
        try (EventSubscription ignored = subscribe(queue::offer)) {
            while (true) {
                long remainingMs = (deadline - System.nanoTime()) / 1_000_000;
                if (remainingMs <= 0) {
                    throw new GatewayException(0, "SDK_TIMEOUT",
                            "chat 等待超时（" + timeout + "）", null);
                }
                Event event;
                try {
                    event = queue.poll(remainingMs, TimeUnit.MILLISECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new GatewayException(0, "SDK_INTERRUPTED", "chat 被中断", null);
                }
                if (event == null) {
                    continue;
                }
                if (!currentRunId.equals(event.runId())) {
                    continue; // 历史轮次或他人轮次的事件，与本次 chat 无关
                }
                switch (event.type()) {
                    case "confirmation.required" -> {
                        ConfirmationRequest confirmation = event.asConfirmation();
                        Decision decision = decide(confirmation);
                        RunResult continuation =
                                confirmAsync(confirmation.confirmationId(), decision);
                        currentRunId = continuation.runId();
                    }
                    case "run.completed", "run.failed" -> {
                        return client.getRun(currentRunId);
                    }
                    default -> {
                        // 其余事件（started/delta/tool.*/model.switched）不处理
                    }
                }
            }
        }
    }

    private Decision decide(ConfirmationRequest confirmation) {
        ConfirmationHandler handler = confirmationHandler != null
                ? confirmationHandler
                : client.defaultConfirmationHandler();
        if (handler == null) {
            return Decision.rejected();
        }
        Decision decision = handler.decide(confirmation);
        return decision == null ? Decision.rejected() : decision;
    }

    // ------------------------------------------------------------ 确认

    /** 同步回传决定：阻塞到续跑轮收口。 */
    public RunResult confirm(String confirmationId, Decision decision) {
        JsonNode body = client.request(
                "POST",
                "/v1/sessions/" + sessionId + "/confirmations/" + confirmationId
                        + "?mode=sync",
                endUserId, decisionJson(decision));
        return new RunResult(body);
    }

    /** 异步回传决定：立即返回续跑轮的 run 句柄。 */
    public RunResult confirmAsync(String confirmationId, Decision decision) {
        JsonNode body = client.request(
                "POST",
                "/v1/sessions/" + sessionId + "/confirmations/" + confirmationId
                        + "?mode=async",
                endUserId, decisionJson(decision));
        return new RunResult(body);
    }

    private JsonNode decisionJson(Decision decision) {
        var node = JsonNodeFactory.instance.objectNode();
        if (decision.calls() != null) {
            var arr = JsonNodeFactory.instance.arrayNode();
            for (CallDecision call : decision.calls()) {
                var item = JsonNodeFactory.instance.objectNode();
                if (call.toolCallId() != null) {
                    item.put("tool_call_id", call.toolCallId());
                }
                item.put("decision", call.decision());
                if (call.editedInput() != null) {
                    item.put("edited_input", call.editedInput());
                }
                if (call.rules() != null) {
                    item.set("rules", call.rules());
                }
                arr.add(item);
            }
            node.set("decisions", arr);
            return node;
        }
        node.put("decision", decision.decision());
        if (decision.editedInput() != null) {
            node.put("edited_input", decision.editedInput());
        }
        return node;
    }

    // ------------------------------------------------------------ 事件

    /** 拉取已缓冲的事件（回放）：lastEventId 为空表示从最早开始。 */
    public List<Event> events(String lastEventId) {
        String path = "/v1/sessions/" + sessionId + "/events"
                + (lastEventId == null ? "" : "?last_event_id=" + lastEventId);
        JsonNode body = client.request("GET", path, endUserId, null);
        List<Event> events = new ArrayList<>();
        JsonNode arr = body.get("events");
        if (arr != null && arr.isArray()) {
            arr.forEach(n -> events.add(new Event(n)));
        }
        return events;
    }

    /**
     * 订阅实时事件流（先回放后跟随，断线自动按序号补订）。
     * 返回的句柄须 close；回调在订阅线程上执行。
     */
    public EventSubscription subscribe(Consumer<Event> listener) {
        return new EventSubscription(client, sessionId, endUserId, listener);
    }

    // ------------------------------------------------------------ 历史

    /**
     * 本会话的展示历史（基座投影）：用户消息、确认卡（含决定
     * 状态）、助手回复按轮次序排列，页面可直接渲染恢复。
     * 正文由基座供数，业务侧无需自存会话历史。
     */
    public List<HistoryItem> history() {
        JsonNode body = client.request(
                "GET", "/v1/sessions/" + sessionId + "/history",
                endUserId, null);
        List<HistoryItem> items = new ArrayList<>();
        JsonNode arr = body.get("items");
        if (arr != null && arr.isArray()) {
            arr.forEach(n -> items.add(new HistoryItem(n)));
        }
        return items;
    }

    // ------------------------------------------------------------ 生命周期

    /** 归档（关闭）本会话：之后不可再发消息，历史在基座侧保留。 */
    public boolean archive() {
        JsonNode body = client.request(
                "DELETE", "/v1/sessions/" + sessionId, endUserId, null);
        JsonNode archived = body.get("archived");
        return archived != null && archived.asBoolean(false);
    }
}
