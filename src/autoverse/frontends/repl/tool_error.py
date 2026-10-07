"""Shared tool-error display classification for the REPL/TUI renderers.

The renderers distinguish recoverable tool misses (struck-through inline line)
from hard failures (expandable ``Error (N lines)`` card). Keep the classification
here so registered and generic tools make the same decision.
"""

from __future__ import annotations

from autoverse.types.events import ToolEndPayload


# Error kinds that mark a benign failure: the agent can self-correct, the user
# declined, or the current surface simply cannot support the requested action.
BENIGN_ERROR_KINDS = frozenset(
    {
        "stale_or_unread",
        "no_match",
        "ambiguous_match",
        "disproportionate",
        "invalid_args",
        "check_mode_blocked",
        # AskUserQuestion can be invoked in non-interactive surfaces; the agent can
        # continue with another approach, so this is a recoverable display miss.
        "ask_user_unavailable",
    }
)
BENIGN_ERROR_SUBSTRINGS = (
    "rejected permission",
    "user dismissed",
    "QuestionRejectedError",
    "specified a rule",
    "denied",
)


def is_benign_error(p: ToolEndPayload) -> bool:
    """A denied / agent-recoverable failure -> struck-through, not a red block."""
    if not p.is_error:
        return False
    if p.error_kind in BENIGN_ERROR_KINDS:
        return True
    msg = (p.error_message or "").lower()
    return any(s.lower() in msg for s in BENIGN_ERROR_SUBSTRINGS)


def is_hard_error(p: ToolEndPayload) -> bool:
    """True for an error the user must inspect, not an agent-recoverable miss."""
    return bool(p.is_error) and not is_benign_error(p)


def error_text(p: ToolEndPayload) -> str:
    """Full error body for expandable error cards."""
    out = (p.output or "").strip()
    msg = (p.error_message or "").strip()
    if out and msg and msg not in out:
        return f"{out}\n{msg}"
    return out or msg


__all__ = [
    "BENIGN_ERROR_KINDS",
    "BENIGN_ERROR_SUBSTRINGS",
    "is_benign_error",
    "is_hard_error",
    "error_text",
]
