# -*- coding: utf-8 -*-
"""最小 MCP Server（官方 mcp Python SDK，streamable-http）。

注：mcp SDK 2.x 中 FastMCP 已更名为 MCPServer
（mcp.server.mcpserver.MCPServer），agentscope 2.0.11 要求 mcp>=2，
故此处用 2.x API。

工具与权限级（设计文档 8.2，三级 ALLOW/ASK/DENY）：
- demo_time        ALLOW 只读：返回当前时间，直接执行；
- demo_note_write  ASK 重写：把一条笔记追加到本地文件，执行前须人工确认
  （确认由网关/Agent 层权限引擎拦截，工具本身只负责写）。
启动：python -m mcp_demo.server（端口 MCP_PORT，默认 8200，路径 /mcp）
"""
import os
from datetime import datetime

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from common.config import env, load_env, resolve_path

load_env()

mcp = MCPServer("demo-tools")


# 注解即权限级在框架原生世界的表达（服务侧下发口径）：AgentScope 权限引擎
# 在 DEFAULT 模式下对只读调用走快速通道直接放行——ALLOW 级只读工具靠
# read_only_hint=True 获得放行等价语义；写工具不标注（is_read_only=False）
# 则无规则时默认 ASK，与 ASK 级须确认对齐。注册表（mcp_servers.yaml）的
# level 仍是唯一事实来源，注解须与之一致；DENY 级工具根本不许登记，
# 更不会出现在这里。
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def demo_time() -> str:
    """返回当前本地时间（ISO 格式）。当用户询问现在几点/当前时间时使用。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@mcp.tool()
def demo_note_write(content: str) -> str:
    """把一条笔记追加写入笔记文件。这是 ASK 级写操作，调用前需要人工确认。

    Args:
        content: 笔记内容（一行文本）。
    """
    notes_path = resolve_path(env("MCP_DEMO_NOTES_FILE", "./data/notes.txt"))
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {content}\n"
    with notes_path.open("a", encoding="utf-8") as f:
        f.write(line)
    return f"已写入笔记：{content}"


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=env("MCP_HOST", "127.0.0.1"),
        port=int(env("MCP_PORT", "8200")),
    )
