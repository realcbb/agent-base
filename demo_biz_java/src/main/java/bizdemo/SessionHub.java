package bizdemo;

import agentbase.sdk.AgentBaseClient;
import agentbase.sdk.Event;
import agentbase.sdk.EventSubscription;
import agentbase.sdk.HistoryItem;
import agentbase.sdk.Session;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 会话中枢（业务后端侧）：按浏览器记住每人每个 Agent 的基座
 * 会话；每个基座会话在后端持有一条 SDK 事件订阅，事件一面
 * 缓存供页面断线重连时补播，一面实时推给连着的页面（SSE）。
 * 这就是"业务后端订阅基座事件、页面只连业务后端"的标准形态。
 */
@Component
public class SessionHub {

    private final AgentBaseClient client;
    private final Map<String, Map<String, Session>> byBrowser = new ConcurrentHashMap<>();
    private final Map<String, Session> bySessionId = new ConcurrentHashMap<>();
    private final Map<String, EventChannel> channels = new ConcurrentHashMap<>();
    // 用户点过「新会话」而主动弃用的旧会话：恢复时跳过它们，
    // 否则刚弃的会话会被列表恢复立刻接回来。
    private final java.util.Set<String> superseded =
            ConcurrentHashMap.newKeySet();

    public SessionHub(AgentBaseClient client) {
        this.client = client;
    }

    /** 取（没有就建）某浏览器在某 Agent 下的会话，并保证事件通道已订。 */
    public Session ensureSession(String browserId, String agentId, String endUserId) {
        Map<String, Session> mine =
                byBrowser.computeIfAbsent(browserId, k -> new ConcurrentHashMap<>());
        Session existing = mine.get(agentId);
        if (existing != null) {
            return existing;
        }
        // 本地没有（首次/后端重启后）：先向基座要会话列表，把该
        // 用户最近一个会话接回来续用——业务后端不存会话索引，
        // 隔天回来、后端重启都不丢会话（列表由基座存储供数，
        // 已关闭的会话不在其中）。列表为空才新建。
        Session session = null;
        try {
            var metas = client.listSessions(agentId, endUserId);
            for (var meta : metas) {
                if (!superseded.contains(meta.sessionId())) {
                    session = client.resumeSession(
                            meta.sessionId(), agentId, endUserId);
                    break;
                }
            }
        } catch (Exception ignored) {
            // 列表取不到就走新建，不阻塞
        }
        if (session == null) {
            session = client.createSession(agentId, endUserId);
        }
        mine.put(agentId, session);
        bySessionId.put(session.sessionId(), session);
        EventChannel channel = new EventChannel();
        channels.put(session.sessionId(), channel);
        channel.subscription = session.subscribe(channel::onEvent);
        return session;
    }

    // ------------------------------------------------------------ 展示历史

    /**
     * 展示历史直接向基座取（GET /sessions/{id}/history 投影），
     * 业务后端不自存：页面重开、业务后端重启都不丢。形状转成
     * 页面渲染用的字段名（与网关投影一一对应）。
     */
    public List<ObjectNode> historyItems(String sessionId) {
        Session session = bySessionId.get(sessionId);
        if (session == null) {
            return List.of();
        }
        List<ObjectNode> out = new ArrayList<>();
        for (HistoryItem h : session.history()) {
            ObjectNode item = JsonNodeFactory.instance.objectNode();
            item.put("kind", h.kind());
            switch (h.kind()) {
                case "user" -> {
                    item.put("text", h.text());
                    item.put("image", h.image());
                }
                case "assistant" -> {
                    item.put("text", h.text());
                    item.put("run_id", h.runId());
                }
                case "confirmation" -> {
                    item.put("confirmation_id", h.confirmationId());
                    item.set("tool_calls", h.toolCalls());
                    item.put("impact", h.impact());
                    item.put("status", h.status());
                }
                default -> {
                }
            }
            item.put("ts", h.ts());
            out.add(item);
        }
        return out;
    }

    /** 开新会话：丢弃旧句柄（基座侧旧会话仍在，只是不再用）。 */
    public Session freshSession(String browserId, String agentId, String endUserId) {
        Map<String, Session> mine =
                byBrowser.computeIfAbsent(browserId, k -> new ConcurrentHashMap<>());
        Session old = mine.remove(agentId);
        if (old != null) {
            superseded.add(old.sessionId());
            bySessionId.remove(old.sessionId());
            EventChannel channel = channels.remove(old.sessionId());
            if (channel != null && channel.subscription != null) {
                channel.subscription.close();
            }
        }
        return ensureSession(browserId, agentId, endUserId);
    }

    public Session session(String sessionId) {
        return bySessionId.get(sessionId);
    }

    /** 页面连上事件口：注册 emitter 并按 lastEventId 补播缓存。 */
    public void attach(String sessionId, SseEmitter emitter, String lastEventId) {
        EventChannel channel = channels.get(sessionId);
        if (channel == null) {
            try {
                emitter.completeWithError(
                        new IllegalArgumentException("unknown session"));
            } catch (Exception ignored) {
            }
            return;
        }
        channel.attach(emitter, lastEventId);
    }

    public void detach(String sessionId, SseEmitter emitter) {
        EventChannel channel = channels.get(sessionId);
        if (channel != null) {
            channel.emitters.remove(emitter);
        }
    }

    /** 一个基座会话的事件通道：缓存最近 500 条 + 扇出给页面。 */
    private static final class EventChannel {
        private final Deque<Event> buffer = new ArrayDeque<>();
        private final List<SseEmitter> emitters = new CopyOnWriteArrayList<>();
        private volatile EventSubscription subscription;

        synchronized void onEvent(Event event) {
            if (buffer.size() >= 500) {
                buffer.pollFirst();
            }
            buffer.addLast(event);
            for (SseEmitter emitter : emitters) {
                send(emitter, event);
            }
        }

        synchronized void attach(SseEmitter emitter, String lastEventId) {
            // 新页面（无 lastEventId）：不倒灌历史事件——历史走
            // /api/history 向基座取数渲染，事件流只管实时；只有
            // 断线重连（带 lastEventId）才按序号补播断开期间
            // 错过的事件。
            if (lastEventId != null) {
                boolean replaying = false;
                for (Event event : buffer) {
                    if (!replaying) {
                        if (event.eventId() != null
                                && event.eventId().equals(lastEventId)) {
                            replaying = true;
                        }
                        continue;
                    }
                    send(emitter, event);
                }
            }
            emitters.add(emitter);
        }

        private void send(SseEmitter emitter, Event event) {
            try {
                emitter.send(SseEmitter.event()
                        .id(event.eventId())
                        .name(event.type())
                        .data(event.raw()));
            } catch (IOException | IllegalStateException e) {
                emitters.remove(emitter);
            }
        }
    }
}
