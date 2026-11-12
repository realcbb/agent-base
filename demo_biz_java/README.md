# 模拟业务系统（Java · 演示）

用 agent-base 官方 Java SDK 接入基座的模拟业务系统，演示真实
业务后端的标准姿势：**后端持项目密钥、页面永不直连基座**；
页面发消息 → 后端用 SDK 调网关 → 后端订阅基座事件流并中继
给页面（右侧过程面板）→ ASK 级工具的确认卡由页面渲染、用户
决定后经后端回传。

- 技术：Spring Boot 3.3（JDK 17）+ `agentbase-java-sdk:1.0.0`
- 入口：`bizdemo.DemoBizApplication`；接口在 `BizController`，
  会话与事件中继在 `SessionHub`
- 配置（环境变量）：`BIZ_GATEWAY_URL`、`BIZ_PROJECT_KEY`、
  `BIZ_END_USER`（模拟登录的终端用户工号）
- 接口两套：POST 版（`/api/chat` 等，真实内网部署用）与 GET 版
  （`/api/chat-get` 等，为绕 InstaBox 公网端口网关只放行 GET
  的限制而设的演示传输适配，参数语义一致）

2026-11-08 起部署在 InstaBox（/data/home/ab-demo），8300 端口
经 InstaBox 发布为公网网址。注意机器闲置 30 分钟自动暂停，
暂停时网址离线、访问不唤醒，需先 resume 机器再重启栈
（/tmp/demo_stack.sh 起基座三件套，业务 jar 用 nohup 拉起）。
