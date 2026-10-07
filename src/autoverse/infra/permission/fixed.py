"""Fixed-verdict permission adapters (docs/reference/tools.md).

Simple PermissionPort implementations for tests and non-interactive contexts:
allow-all, deny-all, and a non-interactive engine that denies anything reaching
the approval step ("no frontend available").
"""

from __future__ import annotations

from autoverse.infra.permission.engine import PermissionEngine
from autoverse.infra.permission.matcher import RuleMatcher
from autoverse.types.tools import Decision, ToolCall


class AllowAllPermissionAdapter:
    async def check(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def check_sync(self, call: ToolCall) -> Decision:
        return Decision.allow()

    async def request_approval(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def cancel_all(self) -> None:
        return None


class DenyAllPermissionAdapter:
    _REASON = "denied by policy (deny-all adapter)"

    async def check(self, call: ToolCall) -> Decision:
        return Decision.deny(self._REASON)

    def check_sync(self, call: ToolCall) -> Decision:
        return Decision.deny(self._REASON)

    async def request_approval(self, call: ToolCall) -> Decision:
        return Decision.deny(self._REASON)

    def cancel_all(self) -> None:
        return None


class NonInteractivePermissionAdapter(PermissionEngine):
    """A PermissionEngine where `ask` always denies (no UI to approve)."""

    def __init__(self, matcher: RuleMatcher | None = None) -> None:
        super().__init__(matcher or RuleMatcher(), global_mode="ask")

    async def request_approval(self, call: ToolCall) -> Decision:
        return Decision.deny("no interactive frontend available to approve")


__all__ = [
    "AllowAllPermissionAdapter",
    "DenyAllPermissionAdapter",
    "NonInteractivePermissionAdapter",
]
