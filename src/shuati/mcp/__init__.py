"""零外部依赖的刷题 Harness stdio MCP 服务包。"""

from shuati.mcp.server import (
    McpRole,
    McpServerConfig,
    dispatch_jsonrpc_message,
    get_tools_for_role,
    serve_stdio,
)

__all__ = [
    "McpRole",
    "McpServerConfig",
    "dispatch_jsonrpc_message",
    "get_tools_for_role",
    "serve_stdio",
]
