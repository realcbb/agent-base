# -*- coding: utf-8 -*-
"""Mock 模型：demo 环境无真实模型密钥时的确定性模型。

实现方式（探针实证）：AgentScope 的模型由 Credential 驱动——
``CredentialBase.get_chat_model_class()`` 返回 ChatModelBase 子类，
服务层 ``get_model()`` 据此构造。自定义 MockCredential 经
``create_app(extra_credentials=[MockCredential])`` 注册后，
原生服务即可凭 ChatModelConfig{credential_id, model="mock-1"} 使用它。
网关只经服务原生 HTTP API 间接使用（adapter 已不进程内直调框架）。

行为规则（确定性）：
- 最后一条用户消息含「时间/time」且 demo_time 工具可用、且本轮未拿到其结果
  → 发起 demo_time 工具调用；
- 含「笔记/note」且 demo_note_write 可用、且本轮未拿到其结果
  → 发起 demo_note_write 工具调用（ASK 级，触发人工确认演示）；
- 消息中已有工具结果 → 用一句话回报结果；
- 其它 → echo 回复，前缀 [mock]。
"""
import json
import uuid
from typing import Any, Literal, Type

from pydantic import BaseModel, ConfigDict

from agentscope.credential import CredentialBase
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.model._model_usage import ChatUsage


class MockCredential(CredentialBase):
    """Mock 凭证（无真实密钥，仅占位）。"""

    model_config = ConfigDict(title="Mock (demo)")

    type: Literal["mock_credential"] = "mock_credential"

    @classmethod
    def get_chat_model_class(cls) -> Type["ChatModelBase"]:
        return MockChatModel


def _tool_names(tools: list[dict] | None) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            names.append(name)
    return names


def _find_tool(names: list[str], raw: str) -> str | None:
    """MCP 工具在框架内的名字带前缀（mcp__{server}__{tool}，探针实测），
    按后缀匹配并返回全名——工具调用必须用全名才能被 Toolkit 路由。"""
    for name in names:
        if name == raw or name.endswith(f"__{raw}"):
            return name
    return None


def _last_user_text(messages: list[Msg]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            try:
                return msg.get_text_content() or ""
            except Exception:
                return ""
    return ""


def _tool_results_after_last_user(messages: list[Msg]) -> list[ToolResultBlock]:
    """只看最后一条用户消息之后的工具结果——历史结果不算本轮。"""
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.role == "user":
            last_user_idx = i
    results: list[ToolResultBlock] = []
    for msg in messages[last_user_idx + 1:]:
        results.extend(msg.get_content_blocks("tool_result"))
    return results


def _result_text(block: ToolResultBlock) -> str:
    output = block.output
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif hasattr(item, "text"):
                parts.append(str(item.text))
            else:
                parts.append(str(item))
        return " ".join(parts)
    return str(output)


class MockChatModel(ChatModelBase):
    """确定性 mock 模型。"""

    type = "mock_chat"

    class Parameters(BaseModel):
        temperature: float | None = None
        max_tokens: int | None = None

    def __init__(
        self,
        credential: CredentialBase,
        model: str = "mock-1",
        parameters: Parameters | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters or self.Parameters(),
            stream=stream,
            max_retries=0,
            retry_delay=0.0,
            context_size=32768,
        )
        # Agent 格式化器在部分路径会访问 formatter；基类默认由子类设置。
        try:
            from agentscope.formatter import OpenAIChatFormatter

            self.formatter = OpenAIChatFormatter()
        except Exception:
            self.formatter = None  # type: ignore[assignment]

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        names = _tool_names(tools)
        results = _tool_results_after_last_user(messages)
        done_tools = {r.name.split("__")[-1] for r in results}
        user_text = _last_user_text(messages)
        time_tool = _find_tool(names, "demo_time")
        note_tool = _find_tool(names, "demo_note_write")

        def respond(blocks: list) -> ChatResponse:
            return ChatResponse(
                content=blocks,
                is_last=True,
                usage=ChatUsage(
                    input_tokens=max(1, len(user_text) // 2),
                    output_tokens=16,
                    time=0.01,
                ),
            )

        if results:
            last = results[-1]
            result_text = _result_text(last)
            if "denied by user" in result_text:
                # 框架的拒绝结果是一段 system-reminder 原文，别原样
                # 透给用户（演示页面实测会显示一串英文标记）。
                return respond(
                    [
                        TextBlock(
                            text=(
                                f"[mock] 工具 {last.name} 的执行已被"
                                "你拒绝，本次操作已取消。"
                            ),
                        ),
                    ],
                )
            return respond(
                [
                    TextBlock(
                        text=(
                            f"[mock] 工具 {last.name} 已执行，"
                            f"结果：{result_text}"
                        ),
                    ),
                ],
            )

        lowered = user_text.lower()
        if (
            ("时间" in user_text or "time" in lowered)
            and time_tool
            and "demo_time" not in done_tools
        ):
            return respond(
                [ToolCallBlock(id=uuid.uuid4().hex, name=time_tool, input="{}")],
            )
        if (
            ("笔记" in user_text or "note" in lowered)
            and note_tool
            and "demo_note_write" not in done_tools
        ):
            content = user_text.split("：", 1)[-1].split(":", 1)[-1].strip()
            return respond(
                [
                    ToolCallBlock(
                        id=uuid.uuid4().hex,
                        name=note_tool,
                        input=json.dumps(
                            {"content": content or user_text},
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )
        return respond([TextBlock(text=f"[mock] 收到：{user_text}")])
