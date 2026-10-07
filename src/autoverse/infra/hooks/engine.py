"""HookEngine (docs/reference/hooks.md).

Hooks are injectable async callables (``HookHandler``) keyed by lifecycle event.
Keeping the handler an injectable callable (rather than hard-wiring subprocess
execution) makes the engine deterministic and offline-testable; a shell-script
adapter is simply another ``HookHandler`` that shells out — the engine semantics
are identical.

Semantics:
  - PreToolUse hooks run serially; the **first** ``deny`` short-circuits (E5).
  - Each hook is bounded by ``HookDef.timeout`` via ``asyncio.wait_for``. At a
    **blocking** event (PreToolUse / Stop) a timeout or exception **fails closed**
    -> ``deny`` with an explanation (E6); a non-blocking event degrades to
    "did not run" and keeps going.
  - SessionStart merges every hook's ``additional_context`` in order.

``middleware`` exposes the PreToolUse interception as a dispatcher middleware
(``(call, ctx, nxt)``): deny -> a ``ToolResult`` error feedback (P5), never
entering permission/execution; otherwise it runs ``nxt`` then PostToolUse.

P2 boundary: the engine never references ``PermissionPort`` — a hook ``deny`` and
a permission ``deny`` are independent, separately recorded mechanisms.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from typing import Awaitable, Callable, ClassVar, Literal

from autoverse.core.tools.base import Tool
from autoverse.types.tools import ToolCall, ToolResult


class HookEvent(Enum):
    SESSION_START = "SessionStart"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


@dataclass(frozen=True)
class HookInput:
    """What a hook handler receives. ``tool`` is set for Pre/PostToolUse."""

    event: HookEvent
    tool: ToolCall | None = None
    trigger: str = ""


@dataclass(frozen=True)
class HookOutcome:
    """Parsed result of one hook run (stdout JSON / exit code in the shell form)."""

    additional_context: str = ""
    decision: Literal["proceed", "deny"] = "proceed"
    reason: str = ""


HookHandler = Callable[[HookInput], Awaitable[HookOutcome]]

_BLOCKING_EVENTS = frozenset({HookEvent.PRE_TOOL_USE, HookEvent.STOP})


@dataclass(frozen=True)
class HookDef:
    """A single registered hook. ``matcher`` is fnmatch'd against the tool name."""

    event: HookEvent
    handler: HookHandler
    matcher: str = "*"
    timeout: float = 5.0
    blocking: bool = True
    description: str = ""

    def matches(self, tool: ToolCall | None) -> bool:
        if self.event in (HookEvent.SESSION_START, HookEvent.STOP):
            return True
        if tool is None:
            return False
        return fnmatch(tool.name, self.matcher)


# Middleware signature mirrors the dispatcher's "next" contract.
_Next = Callable[[ToolCall, object], Awaitable[ToolResult]]


class HookEngine:
    """ExtensionSource + dispatcher-middleware provider for lifecycle hooks."""

    name: ClassVar[str] = "hooks"

    def __init__(self, hooks: dict[HookEvent, list[HookDef]] | None = None) -> None:
        self._hooks: dict[HookEvent, list[HookDef]] = {e: [] for e in HookEvent}
        for event, defs in (hooks or {}).items():
            self._hooks[event].extend(defs)

    def add(self, hook: HookDef) -> None:
        self._hooks[hook.event].append(hook)

    def add_all(self, hooks: list[HookDef]) -> None:
        for hook in hooks:
            self.add(hook)

    async def run(self, event: HookEvent, *, tool: ToolCall | None = None,
                  trigger: str = "") -> HookOutcome:
        """Run matching hooks for ``event`` and fold them into one outcome."""
        hook_input = HookInput(event=event, tool=tool, trigger=trigger)
        contexts: list[str] = []
        for hook in self._hooks.get(event, []):
            if not hook.matches(tool):
                continue
            outcome = await self._run_one(hook, hook_input)
            if outcome.decision == "deny":
                return outcome  # PreToolUse/Stop short-circuit on first deny.
            if outcome.additional_context:
                contexts.append(outcome.additional_context)
        return HookOutcome(additional_context="\n".join(contexts))

    async def _run_one(self, hook: HookDef, hook_input: HookInput) -> HookOutcome:
        try:
            return await asyncio.wait_for(hook.handler(hook_input), timeout=hook.timeout)
        except asyncio.TimeoutError:
            return self._fail_closed(hook, "hook timed out")
        except Exception as exc:  # noqa: BLE001 — contain; never bubble into the loop
            return self._fail_closed(hook, f"hook failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _fail_closed(hook: HookDef, why: str) -> HookOutcome:
        if hook.event in _BLOCKING_EVENTS and hook.blocking:
            return HookOutcome(
                decision="deny",
                reason=f"{why} ({hook.event.value}, fail-closed: conservatively denied)",
            )
        # Non-blocking point: degrade to "did not run" — do not stall the session.
        return HookOutcome()

    def middleware(self) -> Callable[[ToolCall, object, _Next], Awaitable[ToolResult]]:
        """Dispatcher middleware wrapping one tool call with Pre/PostToolUse (E5)."""

        async def hook_middleware(call: ToolCall, ctx: object, nxt: _Next) -> ToolResult:
            outcome = await self.run(HookEvent.PRE_TOOL_USE, tool=call)
            if outcome.decision == "deny":
                return ToolResult.error(
                    f"Blocked by hook: {outcome.reason}", error_kind="hook_blocked"
                )
            result = await nxt(call, ctx)
            await self.run(HookEvent.POST_TOOL_USE, tool=call)  # bystander observation
            return result

        return hook_middleware

    # ── ExtensionSource ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        return None

    def contributed_tools(self) -> list[Tool]:
        return []

    def prompt_fragment(self) -> str:
        """Project only behaviour-changing hooks into the prompt.

        PostToolUse (passive audit) is intentionally NOT projected — it does not
        change what the model should do, so listing it would be noise.
        """
        blocking = [
            h
            for h in self._hooks.get(HookEvent.PRE_TOOL_USE, [])
            if h.blocking
        ]
        if not blocking:
            return ""
        lines = ["## Active Tool Hooks",
                 "These hooks may block matching tool calls; adjust and retry if denied:"]
        for h in blocking:
            note = f": {h.description}" if h.description else ""
            lines.append(f"- matcher `{h.matcher}`{note}")
        return "\n".join(lines)

    async def teardown(self) -> None:
        return None


__all__ = [
    "HookDef",
    "HookEngine",
    "HookEvent",
    "HookHandler",
    "HookInput",
    "HookOutcome",
]
