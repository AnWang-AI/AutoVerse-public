"""Terminal capability probing & UI-tier selection (docs/guides/using-cli-and-tui.md §5).

This module is the single source of truth for *which interactive frontend tier
to attempt* given the current terminal. It is pure front-end policy: it reads
only the environment (``os.environ`` / ``sys.std*``) and never imports
``textual``/``rich`` or reflects engine internals (P3). Every input is
injectable so the decision is exhaustively unit-testable (docs/guides/using-cli-and-tui.md §12 B1).

The four tiers (docs/guides/using-cli-and-tui.md §3):

* ``fullscreen`` — Textual alt-screen ``AutoVerseApp`` (best).
* ``inline``    — the *same* app via Textual's inline driver (no alt-screen).
* ``line``      — append-only ``LineRepl`` (docs/guides/using-cli-and-tui.md).
* ``plain``     — one-shot ``--print`` (non-interactive).

``recommend_tier`` is only consulted for ``--ui auto``; explicit ``--ui X``
honours X directly (and errors if unavailable — no silent downgrade, §3.2).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from typing import IO, Mapping

# ── tier ladder ────────────────────────────────────────────────────────────


class UITier(str, Enum):
    """The interactive frontend tiers, richest → leanest.

    The ``.value`` doubles as the ``--ui`` token. Ordering for the runtime
    fallback ladder is given by :data:`_RANK` (higher == richer), not by the
    string value, so ``descend_from`` can walk fullscreen → inline → line.
    """

    FULLSCREEN = "tui"
    INLINE = "inline"
    LINE = "line"
    PLAIN = "plain"


# Richness rank: higher means a fuller experience. Used to build the monotone
# fallback ladder (a tier only ever degrades to a *lower* rank, §3.2).
_RANK: dict[UITier, int] = {
    UITier.FULLSCREEN: 3,
    UITier.INLINE: 2,
    UITier.LINE: 1,
    UITier.PLAIN: 0,
}


def descend_from(start: UITier) -> list[UITier]:
    """Interactive fallback ladder starting at *start*, descending to ``line``.

    ``fullscreen`` → ``[fullscreen, inline, line]``; ``inline`` → ``[inline,
    line]``; ``line`` → ``[line]``. ``plain`` is non-interactive and never part
    of the ladder (docs/guides/using-cli-and-tui.md §8.2). The list is the order the dispatcher
    attempts tiers, dropping to the next on a *startup* failure.
    """
    interactive = (UITier.FULLSCREEN, UITier.INLINE, UITier.LINE)
    start_rank = _RANK[start]
    return [t for t in interactive if _RANK[t] <= start_rank]


# ── probe thresholds (docs/guides/using-cli-and-tui.md §5.2) ──────────────────────────────────

# Below these the full cockpit (sidebar + content) is cramped to the point the
# line REPL reads better at *any* size — so auto routes such terminals to line
# rather than fullscreen/inline.
MIN_FULLSCREEN_COLS = 60
MIN_FULLSCREEN_LINES = 12

_DUMB_TERMS = {"", "dumb", "unknown"}
_CI_ENV_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "JENKINS_URL")


# ── capability snapshot ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TerminalCapabilities:
    """A read-only snapshot of the current terminal's relevant traits (§5.1)."""

    stdin_tty: bool
    stdout_tty: bool
    term: str
    is_dumb: bool
    color: str  # "none" | "16" | "256" | "truecolor"
    columns: int
    lines: int
    encoding: str
    is_utf8: bool
    in_multiplexer: bool  # tmux / screen
    in_ssh: bool
    in_ci: bool
    is_windows: bool


def _detect_color(environ: Mapping[str, str], term: str, is_dumb: bool) -> str:
    """Best-effort colour depth from the environment (§5.1).

    Conservative: an explicit ``NO_COLOR`` (any value) or a dumb terminal means
    no colour; ``COLORTERM=truecolor/24bit`` means truecolor; a ``*-256color``
    term means 256; otherwise assume a basic 16-colour terminal.
    """
    if "NO_COLOR" in environ or is_dumb:
        return "none"
    colorterm = environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return "truecolor"
    if "256" in term:
        return "256"
    return "16"


def probe(
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> TerminalCapabilities:
    """Snapshot the current terminal's capabilities (defensive, never raises).

    All inputs are injectable for testing; production passes nothing and reads
    the live ``os.environ`` / ``sys.std*``. A non-TTY (pipe/redirect) makes
    ``get_terminal_size`` raise ``OSError`` — we fall back to a conservative
    80×24 so callers always get a usable snapshot (docs/guides/using-cli-and-tui.md §12 B2).
    """
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    def _isatty(stream: IO[str] | None) -> bool:
        try:
            return bool(stream is not None and stream.isatty())
        except Exception:  # pragma: no cover - exotic stream objects
            return False

    stdin_tty = _isatty(stdin)
    stdout_tty = _isatty(stdout)
    term = environ.get("TERM", "")
    is_dumb = term.lower() in _DUMB_TERMS

    try:
        size = shutil.get_terminal_size(fallback=(80, 24))
        columns, lines = int(size.columns), int(size.lines)
    except Exception:  # pragma: no cover - extremely defensive
        columns, lines = 80, 24

    encoding = (getattr(stdout, "encoding", None) or "utf-8")
    is_utf8 = encoding.lower().replace("-", "") in ("utf8", "utf")

    return TerminalCapabilities(
        stdin_tty=stdin_tty,
        stdout_tty=stdout_tty,
        term=term,
        is_dumb=is_dumb,
        color=_detect_color(environ, term, is_dumb),
        columns=columns,
        lines=lines,
        encoding=encoding,
        is_utf8=is_utf8,
        in_multiplexer=bool(environ.get("TMUX") or environ.get("STY")),
        in_ssh=bool(environ.get("SSH_TTY") or environ.get("SSH_CONNECTION")),
        in_ci=any(environ.get(v) for v in _CI_ENV_VARS),
        is_windows=os.name == "nt",
    )


def recommend_tier(caps: TerminalCapabilities) -> UITier:
    """Pick the best interactive tier for *caps* (``--ui auto`` only, §5.3).

    Pure function — deterministic in ``caps`` — so the decision is table-driven
    unit-testable. Returns only ``FULLSCREEN`` or ``LINE``: ``auto`` never
    selects ``INLINE`` because alt-screen availability cannot be reliably probed
    (docs/guides/using-cli-and-tui.md §5.3 / N2). ``INLINE`` is reached only via explicit
    ``--ui inline`` or as a runtime fallback when fullscreen fails to start.
    """
    # Defensive: interactive entry requires both ends to be a TTY (the caller
    # already gates non-TTY upstream). If somehow not, line is the safe floor.
    if not (caps.stdin_tty and caps.stdout_tty):
        return UITier.LINE
    # A dumb / colourless terminal cannot render Textual decently → line.
    if caps.is_dumb or caps.color == "none":
        return UITier.LINE
    # Too short/narrow for the cockpit at any tier → line (stable at any size).
    if caps.lines < MIN_FULLSCREEN_LINES:
        return UITier.LINE
    if caps.columns < MIN_FULLSCREEN_COLS:
        return UITier.LINE
    return UITier.FULLSCREEN


def describe(caps: TerminalCapabilities, tier: UITier) -> str:
    """One-line human-readable summary for logs / ``--debug-ui`` (§5.4)."""
    term = caps.term or "?"
    tty = "tty" if (caps.stdin_tty and caps.stdout_tty) else "no-tty"
    return (
        f"term={term} {caps.columns}x{caps.lines} color={caps.color} "
        f"enc={caps.encoding} {tty}"
        f"{' tmux' if caps.in_multiplexer else ''}"
        f"{' ssh' if caps.in_ssh else ''}"
        f"{' ci' if caps.in_ci else ''} → {tier.value}"
    )


__all__ = [
    "UITier",
    "TerminalCapabilities",
    "probe",
    "recommend_tier",
    "describe",
    "descend_from",
    "MIN_FULLSCREEN_COLS",
    "MIN_FULLSCREEN_LINES",
]
