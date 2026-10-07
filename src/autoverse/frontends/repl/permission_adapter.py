"""TuiPermissionAdapter — a Textual ``PermissionPort`` adapter.

Implements :class:`~autoverse.ports.PermissionPort`. Fast allow/deny verdicts (if
a rule matcher is supplied) return synchronously; anything needing a human calls
the app's :meth:`AutoVerseApp.request_permission`, which shows an inline choice
menu docked above the input (the same machinery as AskUserQuestion) and ``await``s
the user's :class:`Decision`. This is the standard "core defines the Port, frontend
implements the Adapter" inversion — no holder-list callback injected back into the
engine (D5).

``request_permission`` must run on Textual's message loop; the engine task is a
Textual worker (``thread=False``) so the awaiting coroutine is already on that
loop. ``cancel_all`` resolves any pending prompt to deny (used on a hard Ctrl+C
abort, §3.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, cast

from autoverse.infra.permission.engine import GlobalMode, PermissionEngine
from autoverse.infra.permission.matcher import RuleMatcher, make_scoped_rule
from autoverse.infra.permission.policy import ToolMeta
from autoverse.types.tools import Decision, ToolCall

if TYPE_CHECKING:  # pragma: no cover - typing only
    from textual.app import App
    from autoverse.infra.permission.rule_store import PermissionRuleStore


class TuiPermissionAdapter(PermissionEngine):
    """Terminal-TUI Adapter for ``PermissionPort`` (modal-screen approval)."""

    def __init__(
        self,
        app: "App[Any]",
        matcher: RuleMatcher | None = None,
        *,
        global_mode: GlobalMode = "ask",
        meta_factory: Callable[[], ToolMeta] | None = None,
        rule_store: "PermissionRuleStore | None" = None,
    ) -> None:
        super().__init__(
            matcher or RuleMatcher(),
            global_mode=global_mode,
            meta_factory=meta_factory,
        )
        self._app = app
        self._cancelled = False
        self._rule_store = rule_store
        # Session this adapter's engine belongs to. Set by the CLI after build so
        # a backgrounded session's approval relays correctly (docs/architecture/frontend-adapters.md
        # §5.6.3) instead of seizing the foreground inline menu.
        self.session_id: str = ""

    async def check(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="approval cancelled")
        return await super().check(call)

    async def request_approval(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="approval cancelled")
        decision = await self._app.request_permission(  # type: ignore[attr-defined]
            call, session_id=self.session_id
        )
        if decision is None:  # pragma: no cover - defensive
            return Decision.deny(reason="no decision")
        if decision.allowed and decision.rule == "user-always":
            rule = make_scoped_rule(call, "allow")
            self._matcher.add_rule(rule, prepend=True)
            if self._rule_store is not None:
                self._rule_store.add(rule)
        return cast(Decision, decision)

    def cancel_all(self) -> None:
        self._cancelled = True
        # Resolve any in-flight inline prompt so the awaiting coroutine unblocks
        # (it then denies via the _cancelled guard / Esc-equivalent result).
        cancel = getattr(self._app, "_cancel_choice", None)
        if callable(cancel):
            cancel()
        super().cancel_all()


__all__ = ["TuiPermissionAdapter"]
