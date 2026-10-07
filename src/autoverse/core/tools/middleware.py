"""Dispatcher middleware helpers (docs/reference/tools.md §8). Pure formatting functions.

These keep the dispatcher readable and ensure failures never leak stack traces
or unexpected internals back to the model.
"""

from __future__ import annotations

from pydantic import ValidationError


def format_validation_error(tool_name: str, err: ValidationError) -> str:
    """Turn a pydantic ValidationError into compact, actionable feedback."""
    lines = [f"Invalid arguments for tool '{tool_name}':"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ())) or "<root>"
        msg = e.get("msg", "invalid")
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def format_unexpected(tool_name: str, exc: Exception) -> str:
    """Render an unexpected tool error without exposing a stack trace."""
    return (
        f"Tool '{tool_name}' failed unexpectedly: "
        f"{type(exc).__name__}: {exc}"
    )


def make_reject_message(tool_name: str, reason: str) -> str:
    """Permission-denied feedback for a rejected tool call."""
    return f"Permission denied for tool '{tool_name}': {reason}"


def timeout_for(tool_name: str, requested: float | None, default: float) -> float:
    """Resolve the effective timeout for a call (explicit request wins, clamped > 0)."""
    if requested is not None and requested > 0:
        return float(requested)
    return float(default)


__all__ = [
    "format_validation_error",
    "format_unexpected",
    "make_reject_message",
    "timeout_for",
]
