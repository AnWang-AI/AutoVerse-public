"""Unattended approval (docs/reference/extensions.md).

``GatewayPermissionPort`` implements :class:`~autoverse.ports.PermissionPort`.
On the ``ask`` tier it emits an ``APPROVAL`` :class:`StreamEvent` (so the sink
renders a button/reply UI) and waits on an :class:`ApprovalRegistry` Future for
a human decision. With nobody present, the wait times out and the port returns a
fail-safe default **DENY** carrying an explanation (P5/P7).

The verdict source (rule matcher) is injected: a plain callable
``ToolCall -> "allow" | "deny" | "ask"`` keeps this independent of the
permission rule engine (that lives in ``infra/permission``, P1/P3).
"""

from __future__ import annotations

import asyncio
from typing import Callable, Literal

from autoverse.ports import EventSink
from autoverse.types.events import ApprovalPayload, make_event
from autoverse.types.tools import Decision, ToolCall

Verdict = Literal["allow", "deny", "ask"]
# Returns the rule verdict for a call (allow/deny/ask). Defaults to "ask" so an
# unconfigured gateway is fail-safe (everything needs approval -> times out deny).
RuleMatcher = Callable[[ToolCall], Verdict]


def _default_matcher(_call: ToolCall) -> Verdict:
    return "ask"


class ApprovalRegistry:
    """Session-level ``call_id -> Future`` table for pending approvals.

    The sink renders the APPROVAL event; the inbound path calls
    :meth:`resolve` (or :meth:`resolve_by_user`) when the user clicks/replies.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self._owner: dict[str, str] = {}  # call_id -> requesting user_id

    def open(self, call_id: str, *, user_id: str = "") -> asyncio.Future[Decision]:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._pending[call_id] = fut
        if user_id:
            self._owner[call_id] = user_id
        return fut

    def resolve(self, call_id: str, decision: Decision) -> bool:
        fut = self._pending.pop(call_id, None)
        self._owner.pop(call_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def resolve_by_user(
        self, call_id: str, decision: Decision, *, user_id: str
    ) -> bool:
        """Resolve only if ``user_id`` matches the requester (anti-proxy, §6)."""
        owner = self._owner.get(call_id)
        if owner and owner != user_id:
            return False
        return self.resolve(call_id, decision)

    def discard(self, call_id: str) -> None:
        self._pending.pop(call_id, None)
        self._owner.pop(call_id, None)

    def deny_all(self, reason: str) -> None:
        for call_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(Decision.deny(reason=reason))
        self._pending.clear()
        self._owner.clear()

    def pending_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)


class GatewayPermissionPort:
    """PermissionPort adapter for unattended/gateway mode (G6)."""

    def __init__(
        self,
        events: EventSink,
        approvals: ApprovalRegistry,
        *,
        matcher: RuleMatcher | None = None,
        ask_timeout: float = 120.0,
        requesting_user_id: str = "",
    ) -> None:
        self._events = events
        self._approvals = approvals
        self._matcher = matcher or _default_matcher
        self._ask_timeout = ask_timeout
        self._user_id = requesting_user_id
        self._cancelled = False

    # ── PermissionPort ────────────────────────────────────────────────────────
    def check_sync(self, call: ToolCall) -> Decision:
        verdict = self._matcher(call)
        if verdict == "allow":
            return Decision.allow(rule="gateway-rule")
        if verdict == "deny":
            return Decision.deny(reason="denied by gateway rule", rule="gateway-rule")
        # ask cannot be resolved synchronously -> defer to async check.
        return Decision.deny(reason="approval required", rule="gateway-ask")

    async def check(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="session cancelled")
        verdict = self._matcher(call)
        if verdict == "allow":
            return Decision.allow(rule="gateway-rule")
        if verdict == "deny":
            return Decision.deny(reason="denied by gateway rule", rule="gateway-rule")
        return await self.request_approval(call)

    async def request_approval(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="session cancelled")
        fut = self._approvals.open(call.id, user_id=self._user_id)
        await self._events.emit(
            make_event(
                ApprovalPayload(
                    call_id=call.id,
                    tool_name=call.name,
                    summary=_summarize(call),
                )
            )
        )
        try:
            return await asyncio.wait_for(fut, self._ask_timeout)
        except asyncio.TimeoutError:
            self._approvals.discard(call.id)
            # fail-safe: nobody present -> default DENY with an explanation (G6).
            return Decision.deny(
                reason="approval timed out with no responder; denied (fail-safe)"
            )

    def cancel_all(self) -> None:
        self._cancelled = True
        self._approvals.deny_all(reason="session cancelled")


def _summarize(call: ToolCall) -> str:
    inp = call.input
    if "file_path" in inp:
        return str(inp["file_path"])
    if "command" in inp:
        return str(inp["command"])
    if "path" in inp:
        return str(inp["path"])
    return ""


__all__ = [
    "ApprovalRegistry",
    "GatewayPermissionPort",
    "RuleMatcher",
    "Verdict",
]
