"""TerminalApproval — a terminal PermissionPort Adapter (R7, D5).

Implements ``ports.PermissionPort``. ``request_approval`` pops a confirm prompt
and returns an allow/deny :class:`Decision` — this is the standard
"core defines the Port, frontend implements the Adapter" inversion, *not* a
holder-list / callback injected back into the engine (D5).

The confirm prompt function is injectable so the adapter is unit-testable
without a TTY: pass any ``Callable[[str], str]`` returning the user's answer.
"""

from __future__ import annotations

from typing import Callable

from autoverse.types.tools import Decision, ToolCall

PromptFn = Callable[[str], str]

_YES = {"y", "yes", "a", "always"}


def _default_prompt(text: str) -> str:  # pragma: no cover - interactive only
    return input(text)


class TerminalApproval:
    """Terminal Adapter for ``PermissionPort`` (allow/deny via a confirm prompt)."""

    def __init__(self, prompt: PromptFn | None = None) -> None:
        self._prompt: PromptFn = prompt or _default_prompt
        self._cancelled = False

    # ── PermissionPort ───────────────────────────────────────────────────────
    def check_sync(self, call: ToolCall) -> Decision:
        # No fast-path rules here; defer the verdict to the async approval flow.
        return self._verdict(call)

    async def check(self, call: ToolCall) -> Decision:
        return await self.request_approval(call)

    async def request_approval(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="approval cancelled")
        return self._verdict(call)

    def cancel_all(self) -> None:
        self._cancelled = True

    # ── internal ──────────────────────────────────────────────────────────────
    def _verdict(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="approval cancelled")
        summary = self._summarize(call)
        answer = self._prompt(f"Allow {call.name}? {summary} [y/N/a] ").strip().lower()
        if answer in _YES:
            return Decision.allow(rule="user-approved")
        return Decision.deny(reason="denied by user")

    @staticmethod
    def _summarize(call: ToolCall) -> str:
        if "file_path" in call.input:
            return str(call.input["file_path"])
        if "command" in call.input:
            return str(call.input["command"])
        return ""


__all__ = ["TerminalApproval", "PromptFn"]
