"""Shell-command hook adapter + config parser (docs/reference/hooks.md).

The :class:`~autoverse.infra.hooks.engine.HookEngine` runs injectable async
``HookHandler`` callables; this module provides the *production* handler that
shells out to a user-configured command and the parser that turns the raw
``config.hooks`` mapping into ``HookDef`` entries.

Wire protocol (docs/reference/hooks.md): the command receives the event via
env vars (``AUTOVERSE_HOOK_EVENT`` / ``AUTOVERSE_TOOL_NAME`` / ``AUTOVERSE_HOOK_TRIGGER``)
and a JSON payload on stdin; it communicates back via a stdout JSON object::

    {"decision": "deny", "reason": "writing to .env is forbidden"}
    {"additionalContext": "## Repo conventions\\nUse pnpm, not npm."}

No output (or non-JSON output) is treated as "proceed / no context". The engine
owns the timeout (``asyncio.wait_for``); on cancellation we kill the subprocess so
a hung hook never leaks a process.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from autoverse.infra.hooks.engine import (
    HookDef,
    HookEvent,
    HookInput,
    HookOutcome,
)

_EVENT_BY_NAME = {e.value: e for e in HookEvent}


class ShellHookHandler:
    """A :data:`HookHandler` that runs a shell command and parses its stdout JSON."""

    def __init__(self, command: str, *, cwd: Path | None = None) -> None:
        self._command = command
        self._cwd = cwd

    async def __call__(self, hook_input: HookInput) -> HookOutcome:
        env = dict(os.environ)
        env["AUTOVERSE_HOOK_EVENT"] = hook_input.event.value
        env["AUTOVERSE_HOOK_TRIGGER"] = hook_input.trigger
        if hook_input.tool is not None:
            env["AUTOVERSE_TOOL_NAME"] = hook_input.tool.name
        payload = json.dumps(_payload(hook_input))

        proc = await asyncio.create_subprocess_shell(
            self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._cwd) if self._cwd else None,
            env=env,
        )
        try:
            stdout, _stderr = await proc.communicate(payload.encode())
        except asyncio.CancelledError:
            # Engine-level timeout cancelled us: don't leak the subprocess.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        if proc.returncode:
            raise RuntimeError(f"hook command exited with status {proc.returncode}")
        return _parse_stdout(stdout.decode(errors="replace"))


def _payload(hook_input: HookInput) -> dict[str, Any]:
    tool = hook_input.tool
    out: dict[str, Any] = {"event": hook_input.event.value, "trigger": hook_input.trigger}
    if tool is not None:
        out["tool_name"] = tool.name
        out["tool_input"] = dict(tool.input)
    return out


def _parse_stdout(text: str) -> HookOutcome:
    text = text.strip()
    if not text:
        return HookOutcome()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # Non-JSON output is advisory context, not a control signal (proceed).
        return HookOutcome(additional_context=text)
    if not isinstance(data, dict):
        return HookOutcome()
    decision = "deny" if str(data.get("decision", "")).lower() == "deny" else "proceed"
    return HookOutcome(
        additional_context=str(data.get("additionalContext", "") or ""),
        decision=decision,  # type: ignore[arg-type]
        reason=str(data.get("reason", "") or ""),
    )


def parse_hooks_config(
    raw: Mapping[str, Any] | None, cwd: Path | None = None
) -> dict[HookEvent, list[HookDef]]:
    """Parse the raw ``config.hooks`` mapping into engine ``HookDef`` entries.

    Shape: ``{event_name: [{"command", "matcher"?, "timeout"?, "blocking"?,
    "description"?}, ...]}``. Unknown event names and malformed entries are
    skipped, so one bad hook entry does not abort extension assembly.
    """
    out: dict[HookEvent, list[HookDef]] = {}
    if not raw:
        return out
    for event_name, entries in raw.items():
        event = _EVENT_BY_NAME.get(str(event_name))
        if (
            event is None
            or not isinstance(entries, Sequence)
            or isinstance(entries, str)
        ):
            continue
        defs: list[HookDef] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            command = str(entry.get("command", "") or "").strip()
            if not command:
                continue
            timeout = _parse_timeout(entry.get("timeout", 5.0))
            if timeout is None:
                continue
            blocking = _parse_blocking(entry.get("blocking", True))
            if blocking is None:
                continue
            defs.append(
                HookDef(
                    event=event,
                    handler=ShellHookHandler(command, cwd=cwd),
                    matcher=str(entry.get("matcher", "*") or "*"),
                    timeout=timeout,
                    blocking=blocking,
                    description=str(entry.get("description", "") or ""),
                )
            )
        if defs:
            out[event] = defs
    return out


def _parse_timeout(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if value is None or value == "":
        value = 5.0
    if not isinstance(value, (int, float, str)):
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _parse_blocking(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


__all__ = ["ShellHookHandler", "parse_hooks_config"]
