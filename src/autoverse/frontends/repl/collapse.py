"""collapse_tool_output — dual-threshold output folding (docs/guides/using-cli-and-tui.md §3.9.3).

Folds long tool output by line count and character count, whichever is reached
first (opencode/MiMo ``collapse-tool-output.ts`` parity). Returns a preview plus
an ``overflow`` flag the renderer uses to print a truncation note. The full
output is never lost — it lives in the event payload / trace; folding is a
render-side concern only (P2).
"""

from __future__ import annotations


def collapse_tool_output(output: str, max_lines: int, max_chars: int) -> tuple[str, bool]:
    """Return ``(preview, overflow)``.

    ``overflow=True`` means the output was truncated and the renderer should show
    a "… +N lines (truncated)" note. Line count and char count both apply; the
    first threshold hit wins.
    """
    lines = output.split("\n")
    if len(lines) <= max_lines and len(output) <= max_chars:
        return output, False
    preview = "\n".join(lines[:max_lines])
    if len(preview) > max_chars:  # char threshold reached first
        return preview[: max(0, max_chars - 1)] + "…", True
    return preview, True


def overflow_note(output: str, shown_lines: int) -> str:
    """A '… +N lines (truncated, full output in trace)' note, or empty if none."""
    total = output.count("\n") + 1 if output else 0
    hidden = total - shown_lines
    if hidden <= 0:
        return ""
    return f"… +{hidden} lines (truncated; full output in trace)"


__all__ = ["collapse_tool_output", "overflow_note"]
