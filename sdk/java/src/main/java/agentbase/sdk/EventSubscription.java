package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

import java.io.IOException;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import java.util.stream.Stream;

/**
 * 事件流订阅句柄：内部线程维持 SSE 长连接，断线后按最后收到的
 * event_id 自动重连补订（网关流是「先回放后跟随」，重连不丢
 * 事件）。close 后线程退出。事件在订阅线程上按序回调，
 * 回调里不要做阻塞长操作。
 */
public final class EventSubscription implements AutoCloseable {
    private final AgentBaseClient client;
    private final String sessionId;
    private final String endUserId;
    private final Consumer<Event> listener;
    private final AtomicBoolean open = new AtomicBoolean(true);
    private final Thread worker;
    private volatile CompletableFuture<HttpResponse<Stream<String>>> inFlight;
    private volatile String lastEventId;

    EventSubscription(AgentBaseClient client, String sessionId, String endUserId,
                      Consumer<Event> listener) {
        this.client = client;
        this.sessionId = sessionId;
        this.endUserId = endUserId;
        this.listener = listener;
        this.worker = new Thread(this::runLoop, "agentbase-events-" + sessionId);
        this.worker.setDaemon(true);
        this.worker.start();
    }

    private void runLoop() {
        long backoffMs = 1000;
        while (open.get()) {
            try {
                streamOnce();
                backoffMs = 1000;
            } catch (Exception e) {
                if (!open.get()) {
                    return;
                }
                // 订阅失败不惊动业务回调：退避后按 lastEventId 补订
            }
            if (!open.get()) {
                return;
            }
            try {
                Thread.sleep(backoffMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            backoffMs = Math.min(backoffMs * 2, 10_000);
        }
    }

    private void streamOnce() throws IOException, InterruptedException {
        String path = "/v1/sessions/" + sessionId + "/events?stream=true"
                + (lastEventId == null ? "" : "&last_event_id=" + lastEventId);
        HttpRequest request = client.requestBuilder(path, endUserId)
                .header("Accept", "text/event-stream")
                .timeout(Duration.ofHours(1))
                .GET()
                .build();
        CompletableFuture<HttpResponse<Stream<String>>> future =
                client.http().sendAsync(request, HttpResponse.BodyHandlers.ofLines());
        inFlight = future;
        HttpResponse<Stream<String>> response = future.join();
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IOException("stream status " + response.statusCode());
        }
        try (Stream<String> lines = response.body()) {
            lines.forEach(line -> {
                if (!open.get()) {
                    throw new IllegalStateException("closed");
                }
                if (line == null || !line.startsWith("data:")) {
                    return; // 心跳注释（: heartbeat）与 id 行不在此处理
                }
                String data = line.substring(5).trim();
                if (data.isEmpty()) {
                    return;
                }
                try {
                    JsonNode node = AgentBaseClient.MAPPER.readTree(data);
                    Event event = new Event(node);
                    if (event.eventId() != null) {
                        lastEventId = event.eventId();
                    }
                    listener.accept(event);
                } catch (IOException ignored) {
                    // 单条事件解析失败不中断订阅
                }
            });
        }
    }

    @Override
    public void close() {
        open.set(false);
        CompletableFuture<HttpResponse<Stream<String>>> future = inFlight;
        if (future != null) {
            future.cancel(true);
        }
        worker.interrupt();
    }
}
