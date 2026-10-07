"""Machine-generated trace index blocks for compact summaries (docs/architecture/runtime-flow.md §4.9).

Pure string functions — no I/O, no infra dependencies. The ``[Trace:]`` block
appended to a compact summary tells the model where to find the full tool-call
record (inputs/outputs) for the turns that were folded into the summary.
"""

from __future__ import annotations

import re

# Matches a [Trace: session=... turns=X–Y] header line + the → path line.
_TRACE_BLOCK_RE = re.compile(
    r"\n*\[Trace: session=[^\]]+\][^\n]*\n→[^\n]*",
    re.MULTILINE,
)

# Matches turns=X–Y (en-dash) or turns=X-Y (hyphen) inside a [Trace:] header.
_TRACE_TURNS_RE = re.compile(
    r"\[Trace: session=[^\]]*\s+turns=(\d+)[\u2013\-](\d+)\]",
)


def build_trace_index(
    session_id: str, turn_start: int, turn_end: int, trace_path: str
) -> str:
    """Build a ``[Trace: ...]`` block to append to a compact summary message.

    The returned string starts with a newline so callers can simply append it
    to existing summary text. Returns empty string if session_id is empty.
    """
    if not session_id:
        return ""
    parts = [
        f"\n[Trace: session={session_id} turns={turn_start}–{turn_end}]",
        f"→ {trace_path}",
    ]
    return "\n".join(parts)


def extract_last_trace_turn_end(text: str) -> int | None:
    """Parse the ``turns=X–Y`` range from the LAST ``[Trace:]`` block in *text*.

    Returns Y as an int, or None if no block is found. Used so subsequent
    compaction rounds can set ``turn_start = Y + 1`` for non-overlapping ranges.
    """
    matches = list(_TRACE_TURNS_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    try:
        return int(last.group(2))
    except (ValueError, IndexError):
        return None


def strip_trace_blocks(text: str) -> str:
    """Remove all ``[Trace: ...]`` blocks from a summary text.

    A block starts with ``[Trace: session=...]`` and ends after the ``→ path``
    line. Leading blank lines before each block are also stripped. Used before
    appending a fresh block so prior compaction rounds don't accumulate.
    """
    return _TRACE_BLOCK_RE.sub("", text).rstrip()


__all__ = [
    "build_trace_index",
    "extract_last_trace_turn_end",
    "strip_trace_blocks",
]
