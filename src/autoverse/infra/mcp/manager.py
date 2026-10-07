"""MCPManager + MCPTool (docs/reference/mcp.md).

Protocol/transport split (P6) is honoured by depending only on the small
``MCPClient`` Protocol — codec/transport concretions live behind it, so tests
inject an in-memory fake. ``MCPManager`` implements the ``ExtensionSource`` shape
(``setup`` / ``contributed_tools`` / ``prompt_fragment`` / ``teardown``):

  ② discovery + ③ start  -> ``setup`` connects every configured server in
     parallel with failure isolation; a server that fails to connect is marked
     ``failed`` and a background exponential-backoff reconnect task is scheduled.
  ① contribution         -> ``contributed_tools`` returns the wrapped ``MCPTool``s.
  ④ prompt projection    -> ``prompt_fragment`` lists Connected MCP Tools (P8).

Two-layer timeout (§4.5): ``ReconnectPolicy.attempt_timeout`` bounds a single
connect/handshake attempt (layer 1, ``asyncio.wait_for``); ``MCPServerConfig.
rpc_timeout`` is the per-call JSON-RPC timeout enforced by the client (layer 2).

Fail-closed capability (§4.6, P7): a server not present in the explicit trust
table — or one that declares no capability — has *all* its tools wrapped at the
most-restricted ``CapabilityTier.PROCESS`` with ``needs_network=True``. The
permission engine therefore asks; the MCP layer never decides permission itself
(P2 — it only declares).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Literal,
    Protocol,
    cast,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, create_model

from autoverse.core.tools.base import Tool
from autoverse.core.tools.context import ToolContext
from autoverse.infra.mcp.session import (
    MCPError,
    MCPResourceDef,
    MCPToolDef,
)
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

MCP_DEFAULT_TIER = CapabilityTier.PROCESS

ServerStatus = Literal["connected", "failed", "reconnecting"]


@runtime_checkable
class MCPClient(Protocol):
    """Injectable transport+codec seam. Tests provide an in-memory fake.

    Production currently uses the stdio JSON-RPC client; future HTTP or
    streamable-HTTP clients can implement the same Protocol. The manager depends
    only on this seam (P6: transport ⊥ codec ⊥ manager).
    """

    async def initialize(self) -> dict[str, Any]: ...

    async def list_tools(self) -> list[MCPToolDef]: ...

    async def call_tool(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def list_resources(self) -> list[MCPResourceDef]: ...

    async def read_resource(self, uri: str) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class MCPServerConfig:
    """Declarative server entry. ``capability`` (if set) narrows fail-closed default."""

    name: str
    command: str = ""
    url: str = ""
    capability: CapabilityTier | None = None
    needs_network: bool = True
    rpc_timeout: float = 30.0  # layer-2 per-call timeout (client-enforced)


@dataclass(frozen=True)
class ReconnectPolicy:
    """Exponential backoff with a per-attempt (layer-1) connect timeout."""

    base_delay: float = 2.0
    factor: float = 2.0
    max_delay: float = 60.0
    max_attempts: int = 5
    attempt_timeout: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """Delay (seconds) before reconnect attempt ``attempt`` (1-based)."""
        delay = self.base_delay * (self.factor ** max(0, attempt - 1))
        return min(delay, self.max_delay)


def mcp_tool_name(server: str, tool: str) -> str:
    """Industry-convention key ``mcp__{server}__{tool}`` (P5: matches model habit)."""
    return f"mcp__{server}__{tool}"


def tier_for_server(
    server: str, table: dict[str, CapabilityTier] | None
) -> CapabilityTier:
    """Resolve capability tier; unknown server -> fail-closed PROCESS (§4.6, P7)."""
    if table is None:
        return MCP_DEFAULT_TIER
    return table.get(server, MCP_DEFAULT_TIER)


def build_args_model(name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    """Build a permissive pydantic ``Args`` model from a JSON Schema (P1 source).

    Declared properties become optional ``Any`` fields; ``required`` ones lose
    their default. ``extra='allow'`` keeps any undeclared keys so ``model_dump``
    round-trips the exact arguments to the server.
    """
    props = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    required = set(input_schema.get("required", []) if isinstance(input_schema, dict) else [])
    fields: dict[str, Any] = {}
    for prop in props:
        if prop in required:
            fields[prop] = (Any, ...)
        else:
            fields[prop] = (Any, None)
    model: type[BaseModel] = create_model(
        f"MCPArgs_{name}",
        __config__=ConfigDict(extra="allow"),
        **fields,
    )
    return model


def _render_blocks(blocks: list[dict[str, Any]]) -> str:
    """Render MCP content blocks into a plain-text tool output."""
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(str(block))
            continue
        if block.get("type") == "text" and "text" in block:
            out.append(str(block["text"]))
        elif "text" in block:
            out.append(str(block["text"]))
        else:
            out.append(str(block))
    return "\n".join(out)


class MCPTool:
    """Wraps one MCP server tool as a core ``Tool`` (structural Protocol).

    ``name`` / ``capability`` / ``needs_network`` / ``Args`` are *instance*
    attributes (per-server), which still satisfy the runtime-checkable ``Tool``
    Protocol. ``MCPTool`` never calls the permission engine (P2): it only
    *declares* its tier, fail-closed to PROCESS for unknown servers (§4.6).
    """

    def __init__(self, server: str, client: MCPClient, defn: MCPToolDef,
                 capability: CapabilityTier, *, needs_network: bool = True) -> None:
        self._server = server
        self._client = client
        self._defn = defn
        self.name: str = mcp_tool_name(server, defn.name)
        self.description: str = f"[{server}] {defn.description}".strip()
        self.capability: CapabilityTier = capability
        self.needs_network: bool = needs_network
        self.Args: type[BaseModel] = build_args_model(self.name, defn.input_schema)

    def input_json_schema(self) -> dict[str, Any]:
        return self.Args.model_json_schema()

    def to_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_json_schema(),
        }

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        try:
            blocks = await self._client.call_tool(self._defn.name, args.model_dump())
        except MCPError as exc:
            return ToolResult.error(
                f"MCP tool {self.name!r} failed: {exc}", error_kind="mcp_error"
            )
        return ToolResult.ok(
            _render_blocks(blocks), server=self._server, mcp_tool=self._defn.name
        )


async def _safe_list_resources(
    client: MCPClient,
) -> list[MCPResourceDef]:
    """Call ``list_resources`` defensively.

    Older / in-memory fake clients (e.g. the E1–E8 acceptance double) may not
    implement the resource methods. Treat their absence as "no resources"
    rather than crashing — backward compat with pre-resource clients.
    """
    fn = getattr(client, "list_resources", None)
    if fn is None:
        return []
    return cast("list[MCPResourceDef]", await fn())


async def _safe_read_resource(client: MCPClient, uri: str) -> list[dict[str, Any]]:
    """Call ``read_resource`` defensively (see ``_safe_list_resources``)."""
    fn = getattr(client, "read_resource", None)
    if fn is None:
        return []
    return cast("list[dict[str, Any]]", await fn(uri))


class _ListResourcesArgs(BaseModel):
    server: str | None = None


class ListMcpResourcesTool:
    """Builtin-style tool listing resources advertised by connected MCP servers.

    READ_ONLY tier: pure observation (docs/reference/mcp.md). Lives in infra/mcp
    (not core/tools/builtin) since it wraps the manager's MCP clients. Satisfies
    the structural core ``Tool`` Protocol.
    """

    name: ClassVar[str] = "list_mcp_resources"
    description: ClassVar[str] = (
        "List resources advertised by connected MCP servers. "
        "Optional 'server' narrows to one server."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = _ListResourcesArgs

    def __init__(self, manager: "MCPManager") -> None:
        self._manager = manager

    def input_json_schema(self) -> dict[str, Any]:
        return self.Args.model_json_schema()

    def to_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_json_schema(),
        }

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        target = getattr(args, "server", None)
        lines: list[str] = []
        for server, client in self._manager.clients.items():
            if target is not None and server != target:
                continue
            try:
                resources = await _safe_list_resources(client)
            except MCPError as exc:
                return ToolResult.error(
                    f"list_mcp_resources failed for {server!r}: {exc}",
                    error_kind="mcp_error",
                )
            for r in resources:
                lines.append(f"[{server}] {r.uri} — {r.name or r.description}")
        if not lines:
            return ToolResult.ok("(no MCP resources)")
        return ToolResult.ok("\n".join(lines))


class _ReadResourceArgs(BaseModel):
    uri: str
    server: str | None = None


class ReadMcpResourceTool:
    """Builtin-style tool reading one MCP resource by URI (READ_ONLY, §4.4)."""

    name: ClassVar[str] = "read_mcp_resource"
    description: ClassVar[str] = (
        "Read the contents of an MCP resource by URI. "
        "Optional 'server' selects which connected server to read from."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = _ReadResourceArgs

    def __init__(self, manager: "MCPManager") -> None:
        self._manager = manager

    def input_json_schema(self) -> dict[str, Any]:
        return self.Args.model_json_schema()

    def to_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_json_schema(),
        }

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        uri = str(getattr(args, "uri"))
        target = getattr(args, "server", None)
        clients = self._manager.clients
        if target is not None:
            client = clients.get(target)
            if client is None:
                return ToolResult.error(
                    f"read_mcp_resource: no connected MCP server {target!r}",
                    error_kind="mcp_error",
                )
            candidates = [(target, client)]
        else:
            candidates = list(clients.items())
        for server, client in candidates:
            try:
                contents = await _safe_read_resource(client, uri)
            except MCPError as exc:
                return ToolResult.error(
                    f"read_mcp_resource failed on {server!r}: {exc}",
                    error_kind="mcp_error",
                )
            if contents:
                return ToolResult.ok(_render_blocks(contents), server=server, uri=uri)
        return ToolResult.error(
            f"read_mcp_resource: resource {uri!r} not found on any connected server",
            error_kind="mcp_error",
        )


ClientFactory = Callable[[MCPServerConfig], MCPClient]


class MCPManager:
    """ExtensionSource that owns MCP server lifecycles (§4.4)."""

    name: ClassVar[str] = "mcp"

    def __init__(
        self,
        configs: dict[str, MCPServerConfig],
        client_factory: ClientFactory | None = None,
        *,
        capability_table: dict[str, CapabilityTier] | None = None,
        reconnect: ReconnectPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._configs = configs
        # No factory injected -> build real stdio clients from config.command.
        # The injectable factory remains the test seam (P6).
        if client_factory is None:
            from autoverse.infra.mcp.transport import default_client_factory

            client_factory = default_client_factory
        self._client_factory = client_factory
        self._capability_table = capability_table
        self._reconnect = reconnect or ReconnectPolicy()
        self._sleep = sleep or asyncio.sleep
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, list[MCPTool]] = {}
        self._status: dict[str, ServerStatus] = {}
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}
        # Backoff delays actually waited per server (observability / tests).
        self.reconnect_delays: dict[str, list[float]] = {}
        # TOOL_LIST_CHANGED signal (§4.6). No new EventType is introduced (P8):
        # a successful (re)connect that mutates the contributed tool set bumps
        # this manager-visible counter, which a frontend/test can observe to
        # refresh the prompt/registry. A callback hook could replace this later
        # without a new event mechanism.
        self.tool_list_changed_count = 0

    # ── ExtensionSource ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        await asyncio.gather(
            *(self._start_one(name, cfg) for name, cfg in self._configs.items()),
            return_exceptions=True,
        )

    def contributed_tools(self) -> list[Tool]:
        # The MCP tool wrappers are the per-server tools (§4.4). The two resource
        # tools are kept *separate* (``resource_tools``) so callers that only
        # want the dynamic per-server set are unaffected; the engine registers
        # both groups. They are still fully dispatchable core Tools.
        tools: list[Tool] = []
        for server_tools in self._tools.values():
            tools.extend(cast("list[Tool]", server_tools))
        return tools

    def resource_tools(self) -> list[Tool]:
        """The two MCP resource tools, exposed only when a server is connected.

        ``list_mcp_resources`` / ``read_mcp_resource`` are READ_ONLY (§4.4).
        Returned separately from ``contributed_tools`` so the per-server tool
        set (asserted exactly by acceptance tests) stays stable; the engine
        registers ``[*contributed_tools(), *resource_tools()]``.
        """
        if not self._clients:
            return []
        return [
            cast("Tool", ListMcpResourcesTool(self)),
            cast("Tool", ReadMcpResourceTool(self)),
        ]

    def prompt_fragment(self) -> str:
        mcp_tools = [t for server_tools in self._tools.values() for t in server_tools]
        if not mcp_tools:
            return ""
        lines = [f"- {t.name}: {t.description}" for t in mcp_tools]
        return "## Connected MCP Tools\n" + "\n".join(lines)

    async def teardown(self) -> None:
        for task in self._reconnect_tasks.values():
            task.cancel()
        await asyncio.gather(
            *(t for t in self._reconnect_tasks.values()), return_exceptions=True
        )
        await asyncio.gather(
            *(c.close() for c in self._clients.values()), return_exceptions=True
        )

    # ── status ───────────────────────────────────────────────────────────────
    @property
    def server_status(self) -> dict[str, ServerStatus]:
        return dict(self._status)

    @property
    def clients(self) -> dict[str, MCPClient]:
        """Connected clients keyed by server name (used by resource tools)."""
        return dict(self._clients)

    def _emit_tool_list_changed(self) -> None:
        """Record a TOOL_LIST_CHANGED signal after a reconnect mutates tools.

        P8 — no new EventType / mechanism: just bump a manager-visible counter
        that a frontend/prompt-refresh loop (or a test) can poll. The §4.6
        "tools dynamically added on reconnect" semantics are satisfied because
        ``contributed_tools`` already derives from the live registry (P1); this
        counter only signals *that* the derivation should be re-run.
        """
        self.tool_list_changed_count += 1

    # ── lifecycle internals ───────────────────────────────────────────────────
    async def _start_one(self, name: str, cfg: MCPServerConfig) -> None:
        try:
            await self._connect(name, cfg)
        except Exception:  # noqa: BLE001 — isolate: one server must not abort setup
            self._status[name] = "failed"
            self._schedule_reconnect(name, cfg)

    async def _connect(self, name: str, cfg: MCPServerConfig) -> None:
        client = self._client_factory(cfg)
        # Layer-1 timeout: bound the handshake + listing.
        await asyncio.wait_for(client.initialize(), timeout=self._reconnect.attempt_timeout)
        defs = await asyncio.wait_for(
            client.list_tools(), timeout=self._reconnect.attempt_timeout
        )
        tier = self._tier_for(name, cfg)
        self._clients[name] = client
        self._tools[name] = [
            MCPTool(name, client, d, tier, needs_network=cfg.needs_network) for d in defs
        ]
        self._status[name] = "connected"

    def _tier_for(self, name: str, cfg: MCPServerConfig) -> CapabilityTier:
        if cfg.capability is not None:
            return cfg.capability
        return tier_for_server(name, self._capability_table)

    def _schedule_reconnect(self, name: str, cfg: MCPServerConfig) -> None:
        if name in self._reconnect_tasks and not self._reconnect_tasks[name].done():
            return
        self._reconnect_tasks[name] = asyncio.ensure_future(
            self._reconnect_loop(name, cfg)
        )

    async def _reconnect_loop(self, name: str, cfg: MCPServerConfig) -> None:
        self.reconnect_delays.setdefault(name, [])
        for attempt in range(1, self._reconnect.max_attempts + 1):
            delay = self._reconnect.delay_for(attempt)
            self.reconnect_delays[name].append(delay)
            self._status[name] = "reconnecting"
            await self._sleep(delay)
            try:
                await self._connect(name, cfg)
                # Reconnect succeeded -> the contributed tool set changed (§4.6).
                self._emit_tool_list_changed()
                return
            except Exception:  # noqa: BLE001 — keep retrying until attempts exhausted
                self._status[name] = "failed"
        self._status[name] = "failed"

    async def trigger_reconnect(self, name: str) -> None:
        """Mark a server disconnected and (re)start its backoff loop (§4.6)."""
        cfg = self._configs[name]
        self._tools.pop(name, None)
        self._clients.pop(name, None)
        self._status[name] = "failed"
        self._schedule_reconnect(name, cfg)
        task = self._reconnect_tasks.get(name)
        if task is not None:
            await task


__all__ = [
    "MCP_DEFAULT_TIER",
    "MCPClient",
    "MCPError",
    "MCPManager",
    "MCPResourceDef",
    "MCPServerConfig",
    "MCPTool",
    "MCPToolDef",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "ReconnectPolicy",
    "ServerStatus",
    "build_args_model",
    "mcp_tool_name",
    "tier_for_server",
]
