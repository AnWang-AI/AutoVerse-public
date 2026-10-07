"""autoverse.frontends.gateway — IM/platform gateway (docs/reference/extensions.md).

A pure frontend adapter: maps any chat platform's conversation onto one
AutoVerse session. Depends only on ``ports`` (EngineHandle/EventSink/
PermissionPort) and ``types`` (StreamEvent/ImageBlock) — never on engine
internals (P3). New platform = implement :class:`PlatformAdapter` + register;
the Runner is unchanged (G7/P8).
"""

from __future__ import annotations

from autoverse.frontends.gateway.adapter import (
    EngineInput,
    OnInbound,
    OutboundMessage,
    OutboundRenderer,
    PlatformAdapter,
    PlatformRegistry,
    downgrade_tool_end,
    normalize_inbound,
)
from autoverse.frontends.gateway.approval import (
    ApprovalRegistry,
    GatewayPermissionPort,
    RuleMatcher,
)
from autoverse.frontends.gateway.router import (
    ActiveSession,
    SessionPolicy,
    SessionRouter,
    route_session_id,
)
from autoverse.frontends.gateway.runner import (
    BackoffPolicy,
    GatewayRunner,
    RateLimitError,
)
from autoverse.frontends.gateway.throttle import DeltaThrottle
from autoverse.frontends.gateway.types import (
    InboundAttachment,
    InboundMessage,
    PlatformCapabilities,
)

__all__ = [
    # types
    "InboundMessage",
    "InboundAttachment",
    "PlatformCapabilities",
    "OutboundMessage",
    "EngineInput",
    # adapter / rendering
    "PlatformAdapter",
    "OnInbound",
    "OutboundRenderer",
    "PlatformRegistry",
    "normalize_inbound",
    "downgrade_tool_end",
    # router
    "SessionRouter",
    "SessionPolicy",
    "ActiveSession",
    "route_session_id",
    # approval
    "GatewayPermissionPort",
    "ApprovalRegistry",
    "RuleMatcher",
    # runner
    "GatewayRunner",
    "BackoffPolicy",
    "RateLimitError",
    # throttle
    "DeltaThrottle",
]
