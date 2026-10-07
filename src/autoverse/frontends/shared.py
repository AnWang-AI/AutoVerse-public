"""Textual-free presentation helpers shared across frontends (docs/architecture/frontend-adapters.md §6.3).

The TUI (``frontends/repl``), the WebUI backend (``frontends/web``) and — via
the values these functions produce — the browser client all need the SAME
session-rail formatting, status vocabulary, interaction-mode colours and
event→phase classification. Historically these lived in ``session_rail.py`` /
``widgets.py`` / ``app.py``, which import ``textual`` and so could not be reused
by the Web backend (P3). This module is the single source of truth: it imports
only ``rich`` (for CJK-aware cell measurement) + ``autoverse.ports`` /
``autoverse.types`` and is safe to import anywhere.

``session_rail.py`` re-exports the names it historically owned so existing TUI
imports / tests keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

from autoverse.ports import SessionStatus, SessionSummary
from autoverse.types import EventType

# ── status vocabulary (rail rows + group rollups + web session list) ─────────
# Status → (leading glyph, glyph theme-role, title theme-role, text tag, bold).
# Glyphs are the geometric/dingbat family (ASCII-safe, NOT emoji). ``running``
# uses the live spinner frame the app supplies, so its glyph here is a
# placeholder. All values are plain strings → textual-free.
STATUS_VIS: dict[SessionStatus, tuple[str, str, str, str, bool]] = {
    "idle": ("", "", "text_muted", "", False),
    "running": ("●", "accent", "text", "(running)", False),
    "awaiting_input": ("◆", "warning", "warning", "(needs input)", True),
    "completed_unseen": ("✓", "success", "text", "(done)", True),
    "error_unseen": ("✗", "error", "error", "(failed)", True),
    "busy_external": ("", "", "warning", "(busy)", False),
}

# ASCII fallbacks for the non-ASCII glyphs (low-colour / restricted terminals).
ASCII_GLYPH = {"●": "*", "◆": "!", "✓": "v", "✗": "x", "▾": "v", "▸": ">"}


# ── interaction-mode colour (input bar / status badge / queued rows) ─────────
# Mode → CSS colour var name. MUST stay the single source so the TUI input bar,
# the queued-message rows and the WebUI mode badge never disagree (the TUI
# previously kept a divergent copy in widgets.py that swapped normal/plan).
MODE_COLOR = {
    "normal": "$av-warning",
    "plan": "$av-success",
    "worktree": "$av-mode-worktree",
}

# Mode → raw theme role (for non-Textual consumers that resolve a hex instead of
# a ``$av-*`` markup var — e.g. the WebUI which can't use Textual CSS vars).
MODE_ROLE = {
    "normal": "warning",
    "plan": "success",
    "worktree": "mode_worktree",
}


def sanitize_title(raw: str, *, max_cells: int = 30, fallback: str = "") -> str:
    """Make a user-authored title safe + bounded for a single rail row.

    Strips control chars / ESC sequences, folds runs of whitespace to one space,
    then truncates to *max_cells* **display cells** (CJK-aware, never mid-glyph)
    with an ellipsis. Empty after cleaning → *fallback* (the caller passes a short
    id). Persistence keeps the raw value; this is the display-layer defence."""
    from rich.cells import cell_len, set_cell_size

    if not raw:
        return fallback
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        code = ord(ch)
        if ch == "\x1b":  # drop an ANSI/escape sequence's introducer + a CSI run
            i += 1
            while i < n and raw[i] not in "\x07\x1b" and not (0x40 <= ord(raw[i]) <= 0x7E):
                i += 1
            i += 1  # skip the final byte
            continue
        if code < 0x20 or code == 0x7F:  # other control chars → whitespace
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    cleaned = " ".join("".join(out).split())
    if not cleaned:
        return fallback
    if cell_len(cleaned) > max_cells:
        # set_cell_size pads/truncates by display width; leave room for the "…".
        cleaned = set_cell_size(cleaned, max(1, max_cells - 1)).rstrip() + "…"
    return cleaned


def cwd_group_key(summary: SessionSummary) -> str:
    """Normalised grouping key for a session's working directory.

    Worktree sessions carry a ``group_root`` (their originating project root) so
    each ephemeral box does not become its own group. Otherwise the cwd is
    canonicalised — realpath (symlinks), trailing slash stripped — so ``/foo``
    and ``/foo/`` and a symlink to it collapse into one stable key. Pure derived:
    recomputable, never persisted."""
    base = summary.group_root or summary.cwd
    if not base:
        return ""
    try:
        resolved = str(Path(base).resolve())
    except OSError:
        resolved = base.rstrip("/") or base
    return resolved


def group_label(key: str) -> tuple[str, str]:
    """(basename, home-folded full path) for a group header."""
    if not key:
        return ("(no directory)", "")
    base = Path(key).name or key
    home = str(Path.home())
    full = key.replace(home, "~", 1) if home and key.startswith(home) else key
    return (base, full)


def relative_time(ts: float, *, now: float) -> str:
    """Compact relative time: ``just now`` / ``2m ago`` / ``3h ago`` / ``5d ago``."""
    if ts <= 0:
        return ""
    delta = max(0.0, now - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def short_id(session_id: str) -> str:
    """Last segment of a ``sess-<uuid>`` id, capped — the empty-title fallback."""
    tail = session_id.rsplit("-", 1)[-1] if session_id else ""
    return (tail[:8] or session_id[:8]) or "(new)"


# ── event → StatusBar phase (TUI _on_engine_event + WebUI status publish) ────
def classify_phase(event_type: EventType) -> str | None:
    """Map a stream event to a coarse StatusBar phase, or ``None`` to keep it.

    Mirrors the TUI ``app._on_engine_event`` switch so the WebUI status bar can
    show the same ``Thinking… / Responding… / Running tool…`` phases instead of
    a single hard-coded ``Responding…``."""
    if event_type is EventType.THINKING_DELTA:
        return "thinking"
    if event_type is EventType.TOOL_START:
        return "tool"
    if event_type in (EventType.TEXT_DELTA, EventType.REQUEST):
        return "generating"
    return None


# Human-readable labels for the phase keys (shared by the TUI StatusBar and the
# WebUI status bar so both read in one consistent ``Title…`` voice).
PHASE_LABELS = {
    "idle": "",
    "thinking": "Thinking…",
    "generating": "Responding…",
    "tool": "Running tool…",
}


def phase_label(phase: str) -> str:
    """Human label for a phase key (falls through for already-prose busy labels)."""
    return PHASE_LABELS.get(phase, phase)


__all__ = [
    "STATUS_VIS",
    "ASCII_GLYPH",
    "MODE_COLOR",
    "MODE_ROLE",
    "sanitize_title",
    "cwd_group_key",
    "group_label",
    "relative_time",
    "short_id",
    "classify_phase",
    "PHASE_LABELS",
    "phase_label",
]
