"""Hooks extension adapter (docs/reference/hooks.md).

Lifecycle interception points (SessionStart / PreToolUse / PostToolUse / Stop).
A ``PreToolUse`` hook can ``deny`` a tool call — expressed as a dispatcher
middleware (E5). Slow hooks are bounded by a per-hook timeout; at a blocking
point a timeout fails *closed* (deny) so it never hangs the loop (E6). Hooks are
*mechanism*, not permission (P2): the engine never touches ``PermissionPort``.
"""

from __future__ import annotations

from autoverse.infra.hooks.engine import (
    HookDef,
    HookEngine,
    HookEvent,
    HookHandler,
    HookInput,
    HookOutcome,
)

__all__ = [
    "HookDef",
    "HookEngine",
    "HookEvent",
    "HookHandler",
    "HookInput",
    "HookOutcome",
]
