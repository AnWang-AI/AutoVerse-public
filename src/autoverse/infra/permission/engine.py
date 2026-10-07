"""PermissionEngine — the PermissionPort implementation (docs/reference/tools.md).

`check` resolves a verdict in four steps:
  1. explicit rule match: allow/deny is terminal; an `ask` rule skips straight to
     the approval step (4);
  2. no explicit rule -> policy assessment: reversible + no hidden cost = allow,
     otherwise ask;
  3. global-mode fallback;
  4. ask -> await request_approval (abstract; subclasses wire a frontend).
Only allow/deny ever escape `check`. `_PendingApprovals` tracks outstanding
futures so `cancel_all` can deny them all (P7). No global singletons (P4).
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from autoverse.infra.permission.matcher import RuleMatcher
from autoverse.infra.permission.policy import ToolMeta, assess_call
from autoverse.types.tools import Decision, ToolCall

GlobalMode = Literal["allow", "deny", "ask"]


@dataclass
class ApprovalRecord:
    tool: str
    pattern: str
    action: str
    reason: str
    timestamp: float


class _ApprovalLog:
    MAX_ENTRIES = 100

    def __init__(self) -> None:
        self._entries: deque[ApprovalRecord] = deque(maxlen=self.MAX_ENTRIES)

    def record(
        self, call: ToolCall, decision: Decision, reason: str = ""
    ) -> None:
        self._entries.append(
            ApprovalRecord(
                tool=call.name,
                pattern=decision.rule or "",
                action="allow" if decision.allowed else "deny",
                reason=reason or decision.reason or "",
                timestamp=time.time(),
            )
        )

    @property
    def entries(self) -> list[ApprovalRecord]:
        return list(self._entries)


class _PendingApprovals:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[Decision]] = {}

    def open(self, call_id: str) -> asyncio.Future[Decision]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._futures[call_id] = fut
        return fut

    def resolve(self, call_id: str, decision: Decision) -> bool:
        fut = self._futures.pop(call_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def cancel_all(self, reason: str) -> None:
        for call_id, fut in list(self._futures.items()):
            if not fut.done():
                fut.set_result(Decision.deny(reason))
            self._futures.pop(call_id, None)


class PermissionEngine(ABC):
    def __init__(
        self,
        matcher: RuleMatcher,
        global_mode: GlobalMode = "ask",
        meta_factory: Callable[[], ToolMeta] | None = None,
    ) -> None:
        self._matcher = matcher
        self._global_mode: GlobalMode = global_mode
        self._meta = (meta_factory or ToolMeta)()
        self._pending = _PendingApprovals()
        self._approval_log = _ApprovalLog()

    @property
    def rules(self) -> Sequence[object]:
        return self._matcher.rules

    @property
    def matcher(self) -> RuleMatcher:
        """The underlying ``RuleMatcher`` (for cross-process rule sync)."""
        return self._matcher

    @property
    def approval_log(self) -> _ApprovalLog:
        return self._approval_log

    async def check(self, call: ToolCall) -> Decision:
        action, rule = self._matcher.match_explained(call)
        rule_src = rule.source if rule is not None else None

        # ── 1. explicit rule ─────────────────────────────────────────────────
        if action == "allow":
            decision = Decision.allow(rule=rule_src)
            self._approval_log.record(call, decision, reason="explicit rule")
            return decision
        if action == "deny":
            decision = Decision.deny("denied by rule", rule=rule_src)
            self._approval_log.record(call, decision, reason="explicit rule")
            return decision
        if action == "ask":
            decision = await self.request_approval(call)
            self._approval_log.record(call, decision)
            return decision

        # ── 2. policy assessment ─────────────────────────────────────────────
        assessment = assess_call(call, self._meta)
        if not assessment.needs_ask:
            decision = Decision.allow()
            self._approval_log.record(call, decision, reason=assessment.reason)
            return decision

        # ── 3. global fallback / 4. ask ──────────────────────────────────────
        if self._global_mode == "allow":
            decision = Decision.allow()
            self._approval_log.record(call, decision, reason="global allow")
            return decision
        if self._global_mode == "deny":
            decision = Decision.deny(assessment.reason or "denied by policy")
            self._approval_log.record(call, decision, reason="global deny")
            return decision
        decision = await self.request_approval(call)
        self._approval_log.record(call, decision)
        return decision

    def check_sync(self, call: ToolCall) -> Decision:
        """Fast path: explicit allow/deny rules only; ask/policy -> conservative deny."""
        action, rule = self._matcher.match_explained(call)
        rule_src = rule.source if rule is not None else None
        if action == "allow":
            return Decision.allow(rule=rule_src)
        if action == "deny":
            return Decision.deny("denied by rule", rule=rule_src)
        assessment = assess_call(call, self._meta)
        if not assessment.needs_ask and action is None:
            return Decision.allow()
        # Even in global allow mode, never auto-allow an irreversible or
        # hidden-cost operation on the sync fast-path (docs/reference/tools.md §3.2).
        if self._global_mode == "allow" and not assessment.needs_ask:
            return Decision.allow()
        return Decision.deny(assessment.reason or "approval required (sync path)")

    @abstractmethod
    async def request_approval(self, call: ToolCall) -> Decision: ...

    def cancel_all(self) -> None:
        self._pending.cancel_all("approvals cancelled")


__all__ = ["GlobalMode", "PermissionEngine", "_PendingApprovals", "ApprovalRecord", "_ApprovalLog"]
