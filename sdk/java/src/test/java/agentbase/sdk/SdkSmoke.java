package agentbase.sdk;

import com.fasterxml.jackson.databind.JsonNode;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Java SDK 集成冒烟（对本地 mock 栈）：java agentbase.sdk.SdkSmoke
 * 参数：&lt;baseUrl&gt; &lt;projectKey&gt; &lt;notesFile&gt;
 * 覆盖：health/agents/建会话/同步对话/chat 确认回路（笔记真落盘）/
 * 异步轮询/事件回放/带图切模型（model.switched）/错误码映射/
 * 无 vision 收图的 MODEL_NO_VISION/归档。退出码 0 = 全部通过。
 */
public final class SdkSmoke {
    private static final List<String> FAILURES = new ArrayList<>();
    private static final String TINY_PNG_B64 =
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                    + "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=";

    private static void check(String name, boolean cond) {
        check(name, cond, "");
    }

    private static void check(String name, boolean cond, String detail) {
        System.out.println("[" + (cond ? "PASS" : "FAIL") + "] " + name
                + (detail == null || detail.isEmpty() ? "" : " " + detail));
        if (!cond) {
            FAILURES.add(name);
        }
    }

    public static void main(String[] args) throws Exception {
        String baseUrl = args[0];
        String projectKey = args[1];
        Path notesFile = Path.of(args[2]);
        String marker = "SDK冒烟笔记" + System.currentTimeMillis();

        AgentBaseClient client = AgentBaseClient.builder()
                .baseUrl(baseUrl)
                .projectKey(projectKey)
                .endUserProof(true)
                .confirmationHandler(request -> Decision.approved())
                .build();

        JsonNode health = client.health();
        check("health", health.has("status") || health.has("model"),
                health.toString());

        List<AgentInfo> agents = client.listAgents();
        check("listAgents 非空", !agents.isEmpty(),
                "-> " + agents.size());

        Session session = client.createSession("demo.echo-assistant", "demo-user");
        check("createSession", session.sessionId() != null
                && session.sessionId().length() == 32, "-> " + session.sessionId());

        RunResult echo = session.send(MessageRequest.text("你好"));
        check("sync send completed", echo.isCompleted()
                && echo.reply() != null && echo.reply().contains("你好"),
                "-> " + echo.status() + " " + echo.reply());

        // chat() + 自动批准：ASK 级写笔记工具应真落盘
        RunResult noted = session.chat(
                MessageRequest.text("帮我记一条笔记：" + marker));
        check("chat 确认回路 completed", noted.isCompleted(),
                "-> " + noted.status());
        String notes = Files.exists(notesFile)
                ? Files.readString(notesFile) : "";
        check("笔记经确认后真落盘", notes.contains(marker));

        // 逐调用决定：从 confirmation.required 事件取 tool_call id，
        // 只按 id 表决该条（Decision.perCall），笔记应照常落盘
        String marker2 = "SDK逐调用笔记" + System.currentTimeMillis();
        Session s2 = client.createSession("demo.echo-assistant", "demo-user");
        s2.sendAsync(MessageRequest.text("帮我记一条笔记：" + marker2));
        String perCallCid = null;
        String perCallTcId = null;
        for (int i = 0; i < 40 && perCallCid == null; i++) {
            for (Event e : s2.events(null)) {
                if ("confirmation.required".equals(e.type())) {
                    perCallCid = e.payload().path("confirmation_id").asText(null);
                    JsonNode calls = e.payload().path("tool_calls");
                    if (calls.isArray() && !calls.isEmpty()) {
                        perCallTcId = calls.get(0).path("id").asText(null);
                    }
                }
            }
            if (perCallCid == null) {
                Thread.sleep(500);
            }
        }
        check("逐调用：事件带 tool_call id", perCallCid != null
                && perCallTcId != null && !perCallTcId.isBlank(),
                "-> cid=" + perCallCid + " tc=" + perCallTcId);
        RunResult perCallRun = s2.confirmAsync(perCallCid,
                Decision.perCall(java.util.List.of(
                        CallDecision.approved(perCallTcId))));
        String perCallStatus = "";
        for (int i = 0; i < 40; i++) {
            perCallStatus = client.getRun(perCallRun.runId()).status();
            if ("completed".equals(perCallStatus)
                    || "failed".equals(perCallStatus)) {
                break;
            }
            Thread.sleep(500);
        }
        String notes2 = Files.exists(notesFile)
                ? Files.readString(notesFile) : "";
        check("逐调用决定续跑完成且笔记落盘",
                "completed".equals(perCallStatus) && notes2.contains(marker2),
                "-> " + perCallStatus);

        // 同会话排队：连发两条 async，前一轮未收口时后一条应排队
        // 等待并最终 completed，而不是以 failed 收口
        Session s3 = client.createSession("demo.echo-assistant", "demo-user");
        RunResult q1 = s3.sendAsync(MessageRequest.text("排队一"));
        RunResult q2 = s3.sendAsync(MessageRequest.text("排队二"));
        String qs1 = "";
        String qs2 = "";
        for (int i = 0; i < 60; i++) {
            qs1 = client.getRun(q1.runId()).status();
            qs2 = client.getRun(q2.runId()).status();
            boolean done1 = "completed".equals(qs1) || "failed".equals(qs1);
            boolean done2 = "completed".equals(qs2) || "failed".equals(qs2);
            if (done1 && done2) {
                break;
            }
            Thread.sleep(500);
        }
        check("同会话连发两条都 completed（排队而非失败）",
                "completed".equals(qs1) && "completed".equals(qs2),
                "-> " + qs1 + "/" + qs2);

        // 异步 + 轮询
        RunResult handle = session.sendAsync(MessageRequest.text("现在几点"));
        String runId = handle.runId();
        String status = "";
        for (int i = 0; i < 40; i++) {
            RunResult run = client.getRun(runId);
            status = run.status();
            if ("completed".equals(status) || "failed".equals(status)
                    || "awaiting_confirmation".equals(status)) {
                break;
            }
            Thread.sleep(500);
        }
        check("async 轮询到终态", "completed".equals(status)
                || "awaiting_confirmation".equals(status), "-> " + status);

        // 事件回放含关键类型
        List<Event> events = session.events(null);
        boolean sawConfirm = events.stream()
                .anyMatch(e -> "confirmation.required".equals(e.type()));
        boolean sawCompleted = events.stream()
                .anyMatch(e -> "run.completed".equals(e.type()));
        check("events 回放含 confirmation.required 与 run.completed",
                sawConfirm && sawCompleted, "-> " + events.size());

        // 带图切模型：vision-assistant 应切到 mock-vision 并完成
        Session vision = client.createSession("demo.vision-assistant", "demo-user");
        RunResult img = vision.chat(MessageRequest.builder()
                .text("看看这张图")
                .addImage(ImageInput.base64(TINY_PNG_B64, "image/png"))
                .build());
        check("带图 chat completed", img.isCompleted(), "-> " + img.status());
        boolean sawSwitch = vision.events(null).stream().anyMatch(e ->
                "model.switched".equals(e.type())
                        && "mock-vision".equals(
                                e.payload().path("to_entry").asText("")));
        check("events 含 model.switched -> mock-vision", sawSwitch);
        check("run 记录 model 已是 mock-vision-1",
                "mock-vision-1".equals(img.model()), "-> " + img.model());

        // 无 vision 的 Agent 收图 -> GatewayException MODEL_NO_VISION
        try {
            session.send(MessageRequest.builder()
                    .text("看看这张图")
                    .addImage(ImageInput.base64(TINY_PNG_B64, "image/png"))
                    .build());
            check("无 vision 收图抛 MODEL_NO_VISION", false, "未抛异常");
        } catch (GatewayException e) {
            check("无 vision 收图抛 MODEL_NO_VISION",
                    e.status() == 400 && "MODEL_NO_VISION".equals(e.code()),
                    "-> " + e.code());
        }

        // 错误密钥 -> AUTH_FAILED
        try {
            AgentBaseClient bad = AgentBaseClient.builder()
                    .baseUrl(baseUrl).projectKey("wrong-key").build();
            bad.listAgents();
            check("错误密钥抛 AUTH_FAILED", false, "未抛异常");
        } catch (GatewayException e) {
            check("错误密钥抛 AUTH_FAILED",
                    e.status() == 401 && "AUTH_FAILED".equals(e.code()),
                    "-> " + e.code());
        }

        JsonNode usage = client.usage();
        check("usage 可读", usage.has("tenant"), usage.toString());

        // 审计账：应能查到本轮的确认决定与会话创建记录
        JsonNode audit = client.audit(100);
        boolean sawDecision = false;
        boolean sawCreated = false;
        JsonNode auditEvents = audit.path("events");
        if (auditEvents.isArray()) {
            for (JsonNode ev : auditEvents) {
                if ("confirmation.decided".equals(ev.path("action").asText())) {
                    sawDecision = true;
                }
                if ("session.created".equals(ev.path("action").asText())) {
                    sawCreated = true;
                }
            }
        }
        check("audit 含 confirmation.decided 与 session.created",
                sawDecision && sawCreated,
                "-> events=" + auditEvents.size());

        // 断言校验（仅当以第 4 参数 proof-on 运行时严格检查）：
        // 不带断言的客户端建会话应被 401 USER_PROOF_* 拒绝
        if (args.length > 3 && "proof-on".equals(args[3])) {
            try {
                AgentBaseClient noProof = AgentBaseClient.builder()
                        .baseUrl(baseUrl).projectKey(projectKey).build();
                noProof.createSession("demo.echo-assistant", "demo-user");
                check("无断言建会话被拒", false, "未抛异常");
            } catch (GatewayException e) {
                check("无断言建会话被拒",
                        e.status() == 401 && e.code() != null
                                && e.code().startsWith("USER_PROOF_"),
                        "-> " + e.code());
            }
        } else {
            System.out.println("[SKIP] 断言负例（未以 proof-on 运行）");
        }

        // 展示历史投影：用户原文/确认卡（approved）/回复三件齐
        List<HistoryItem> historyItems = session.history();
        boolean hasUser = historyItems.stream().anyMatch(
                h -> "user".equals(h.kind()) && h.text() != null
                        && h.text().contains("记一条笔记"));
        boolean hasApprovedCard = historyItems.stream().anyMatch(
                h -> "confirmation".equals(h.kind())
                        && "approved".equals(h.status()));
        boolean hasReply = historyItems.stream().anyMatch(
                h -> "assistant".equals(h.kind()) && h.text() != null
                        && h.text().contains(marker));
        check("history 含用户原文/已批准确认卡/回复",
                hasUser && hasApprovedCard && hasReply,
                "-> items=" + historyItems.size());

        // 会话列表：当前会话应在该用户名下可查回
        List<SessionInfo> sessions =
                client.listSessions("demo.echo-assistant", "demo-user");
        boolean listed = sessions.stream().anyMatch(
                s -> session.sessionId().equals(s.sessionId()));
        check("listSessions 含当前会话", listed,
                "-> " + sessions.size());

        check("archive", vision.archive());

        System.out.println(FAILURES.isEmpty()
                ? "RESULT: ALL PASS"
                : "RESULT: FAILED " + FAILURES);
        System.exit(FAILURES.isEmpty() ? 0 : 1);
    }
}
