"""MCP extension adapter (docs/reference/mcp.md).

Bridges external MCP servers into the engine: each server tool is wrapped as a
core ``Tool`` and injected into the ``ToolRegistry`` so the dispatcher can call
it. Unknown / undeclared-capability servers fail-closed to ``CapabilityTier.PROCESS``
(P7). Disconnected servers reconnect with exponential backoff under a two-layer
timeout. The wire transport is abstracted behind the injectable ``MCPClient``
Protocol (P6: transport ⊥ codec ⊥ manager); tests inject an in-memory fake, while
production uses the real stdio JSON-RPC client (``transport.StdioMcpClient``).
Two resource tools (``list_mcp_resources`` / ``read_mcp_resource``) expose the
servers' ``resources/*`` capability as model-callable READ_ONLY tools (§4.4).
"""

from __future__ import annotations

from autoverse.infra.mcp.manager import (
    MCP_DEFAULT_TIER,
    ListMcpResourcesTool,
    MCPClient,
    MCPError,
    MCPManager,
    MCPResourceDef,
    MCPServerConfig,
    MCPTool,
    MCPToolDef,
    ReadMcpResourceTool,
    ReconnectPolicy,
    ServerStatus,
    build_args_model,
    mcp_tool_name,
    tier_for_server,
)
from autoverse.infra.mcp.session import MCPSession
from autoverse.infra.mcp.transport import (
    MCPTransport,
    StdioMcpClient,
    StdioTransport,
    default_client_factory,
)

__all__ = [
    "MCP_DEFAULT_TIER",
    "ListMcpResourcesTool",
    "MCPClient",
    "MCPError",
    "MCPManager",
    "MCPResourceDef",
    "MCPServerConfig",
    "MCPSession",
    "MCPTool",
    "MCPToolDef",
    "MCPTransport",
    "ReadMcpResourceTool",
    "ReconnectPolicy",
    "ServerStatus",
    "StdioMcpClient",
    "StdioTransport",
    "build_args_model",
    "default_client_factory",
    "mcp_tool_name",
    "tier_for_server",
]
