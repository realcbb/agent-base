package bizdemo;

import agentbase.sdk.AgentBaseClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

/**
 * 模拟业务系统（演示）：扮演真实业务后端的角色——
 * 持有平台发放的项目密钥（只在后端），页面的一切请求都经
 * 本后端用官方 SDK 调基座网关，事件流由后端订阅后中继给
 * 页面，确认卡由页面渲染、决定经后端回传。页面永不直连基座。
 */
@SpringBootApplication
public class DemoBizApplication {

    @Value("${biz.gateway-url:http://127.0.0.1:8000}")
    private String gatewayUrl;

    @Value("${biz.project-key}")
    private String projectKey;

    public static void main(String[] args) {
        SpringApplication.run(DemoBizApplication.class, args);
    }

    @Bean
    public AgentBaseClient agentBaseClient() {
        return AgentBaseClient.builder()
                .baseUrl(gatewayUrl)
                .projectKey(projectKey)
                // 网关开启终端身份断言校验：请求自动附带 HMAC 断言
                .endUserProof(true)
                .build();
    }
}
