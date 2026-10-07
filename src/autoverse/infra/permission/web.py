"""WebPermissionAdapter — engine wired to a frontend via events.

`request_approval` registers a pending future, emits an APPROVAL event, and
awaits the user's reply. `on_user_reply` resolves the future and (for "always"
scopes) installs a scoped rule so subsequent matching calls auto-decide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Literal

from autoverse.infra.permission.engine import GlobalMode, PermissionEngine
from autoverse.infra.permission.matcher import (
    RuleAction,
    RuleMatcher,
    make_scoped_rule,
)
from autoverse.infra.permission.policy import ToolMeta
from autoverse.ports import EventSink
from autoverse.types.events import ApprovalPayload, make_event
from autoverse.types.tools import Decision, ToolCall

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autoverse.infra.permission.rule_store import PermissionRuleStore

ReplyScope = Literal["once", "always"]


class WebPermissionAdapter(PermissionEngine):
    def __init__(
        self,
        matcher: RuleMatcher,
        events: EventSink,
        global_mode: GlobalMode = "ask",
        meta_factory: Callable[[], ToolMeta] | None = None,
        rule_store: "PermissionRuleStore | None" = None,
    ) -> None:
        super().__init__(matcher, global_mode=global_mode, meta_factory=meta_factory)
        self._events = events
        self._rule_store = rule_store

    async def request_approval(self, call: ToolCall) -> Decision:
        fut = self._pending.open(call.id)
        await self._events.emit(
            make_event(
                ApprovalPayload(
                    call_id=call.id,
                    tool_name=call.name,
                    summary=str(call.input)[:200],
                    reason="approval required",
                )
            )
        )
        return await fut

    def on_user_reply(
        self,
        call_id: str,
        action: Literal["allow", "deny"],
        scope: ReplyScope = "once",
        call: ToolCall | None = None,
    ) -> bool:
        """Resolve the pending approval. For scope='always', install a scoped rule
        and persist it so the other frontend picks it up on its next turn."""
        if scope == "always" and call is not None:
            rule_action: RuleAction = "allow" if action == "allow" else "deny"
            rule = make_scoped_rule(call, rule_action)
            self._matcher.add_rule(rule, prepend=True)
            if self._rule_store is not None:
                self._rule_store.add(rule)
        decision = (
            Decision.allow()
            if action == "allow"
            else Decision.deny("denied by user")
        )
        return self._pending.resolve(call_id, decision)


__all__ = ["WebPermissionAdapter", "ReplyScope"]
