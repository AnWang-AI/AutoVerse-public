"""WebLog — the duck-typed log surface that turns ``TuiEventSink`` writes into
structured block commands for the browser (see docs/guides/using-webui.md).

This is the Web analogue of ``LineLog`` (docs/guides/using-cli-and-tui.md §3): the sink is reused
verbatim and probes this surface for capabilities (``begin_stream`` /
``write_collapsible`` / ``write_or_update_agent``) via ``callable(getattr(...))``.
We implement the streaming + collapsible + sub-agent seams (so live markdown
streams, tools fold, and sub-agent cards update in place — matching the mockup),
but deliberately NOT the running-tool seam (``begin_tool``), so ``_can_tool_card``
stays False and a tool simply renders its final card on completion (exactly like
the line tier). Each surface call is converted to a block command dict and handed
to the bridge's ``emit`` callback:

    {"op": "append"|"update"|"upsert"|"remove", "id": str, "block": {...}|None}

The block ``kind`` selects the renderer on the frontend:
``user`` · ``markdown`` · ``thinking`` · ``line`` · ``toolblock`` ·
``collapsible`` · ``subagent`` · ``compaction``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable, cast

from autoverse.frontends.repl.renderer import SUBAGENT_ROLE
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.frontends.web.markup_html import (
    markup_to_html,
    renderable_to_diff_html,
    renderable_to_html,
    renderable_to_text,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import RenderableType

EmitFn = Callable[[dict[str, object]], None]

_MARKUP = re.compile(r"\[[^\]]*\]")
_DIFF_TITLE_RE = re.compile(r"\u2190 Edit|# Wrote")  # ← Edit or # Wrote
_SUBAGENT_HEAD_RE = re.compile(r"^\s*\u2502\s+([A-Za-z]+)\s+Task(?:\b|$)")
_SUBAGENT_ROLES = frozenset(SUBAGENT_ROLE)


def _is_diff_card(title_plain: str) -> bool:
    """Check if a collapsible card title indicates a diff (edit/write tool)."""
    return bool(_DIFF_TITLE_RE.search(title_plain))


class WebLog:
    """A ``TuiEventSink`` log surface that streams structured blocks over SSE."""

    def __init__(self, emit: EmitFn, theme: Theme | None = None) -> None:
        self._emit = emit
        self._theme = theme or DEFAULT_THEME
        self._seq = 0
        self._stream_id: str | None = None
        self._stream_text: str = ""

    # ── id helpers ──────────────────────────────────────────────────────────
    def _next_id(self) -> str:
        self._seq += 1
        return f"b{self._seq}"

    def _append(self, block: dict[str, object]) -> str:
        bid = self._next_id()
        self._emit({"op": "append", "id": bid, "block": block})
        return bid

    # ── core write (Text / Markdown / Rule) ──────────────────────────────────
    def write(self, renderable: object, *, classes: str = "") -> None:
        # Compaction divider arrives as a Rich Rule.
        try:
            from rich.rule import Rule

            if isinstance(renderable, Rule):
                title = renderable.title
                label = _MARKUP.sub("", str(title)) if title else ""
                self._append({"kind": "compaction", "label": label.strip()})
                return
        except Exception:  # pragma: no cover - defensive
            pass
        # Markdown fallback path (only if write_markdown raised) → raw md text.
        markup = getattr(renderable, "markup", None)
        if markup is not None and renderable.__class__.__name__ == "Markdown":
            self._append({"kind": "markdown", "md": str(markup), "streaming": False})
            return
        typed = cast("RenderableType", renderable)
        html = renderable_to_html(typed)
        plain = renderable_to_text(typed).lstrip()
        if classes and "tool-block" in classes:
            self._append({"kind": "toolblock", "html": html})
            return
        # Single-line inline tool cards (web_search / read / grep / …) keep their
        # muted inline styling but carry an extra semantic ``tool`` class so the
        # web frontend's turn-history fold can count them as tool calls.
        cls = "inline tool" if (classes and "tool-line" in classes) else _line_class(plain)
        self._append({"kind": "line", "html": html, "cls": cls})

    def write_markdown(self, text: str) -> None:
        self._append({"kind": "markdown", "md": text, "streaming": False})

    # ── collapsible card (Thought / tool block / diff / error) ───────────────
    def write_collapsible(
        self,
        title: object,
        body: object,
        *,
        collapsed: bool = True,
        classes: str = "",
    ) -> None:
        title_str = str(title)
        title_html = markup_to_html(title_str, self._theme)
        title_plain = _MARKUP.sub("", title_str)
        body_kind = "html"
        if body.__class__.__name__ == "Markdown":
            body_html = str(getattr(body, "markup", ""))
            body_kind = "md"
        elif _is_diff_card(title_plain):
            body_html = renderable_to_diff_html(cast("RenderableType", body))
        else:
            body_html = renderable_to_html(cast("RenderableType", body))
        self._append(
            {
                "kind": "collapsible",
                "title_html": title_html,
                "body_html": body_html,
                "body_kind": body_kind,
                "card_cls": _card_class(title_plain, classes),
                "collapsed": bool(collapsed),
            }
        )

    # ── sub-agent live card (mounted once, updated in place) ──────────────────
    def write_or_update_agent(
        self, job: str, text: object, *, done: bool = False
    ) -> None:
        from rich.text import Text

        # A duck-typed surface can receive either a styled Rich Text or the raw
        # ANSI string used by the line frontend. Never let terminal control
        # bytes cross the structured Web block boundary.
        plain = Text.from_ansi(
            renderable_to_text(cast("RenderableType", text))
        ).plain.rstrip("\n")
        lines = plain.split("\n")
        head = lines[0] if lines else ""
        foot = lines[1] if len(lines) > 1 else ""
        typed = cast("RenderableType", text)
        head_html = _line_html(typed, 0)
        accent = _first_color(head_html)
        role_match = _SUBAGENT_HEAD_RE.match(head)
        role = role_match.group(1).lower() if role_match else "generalist"
        if role not in _SUBAGENT_ROLES:
            role = "generalist"
        self._emit(
            {
                "op": "upsert",
                "id": f"agent:{job}",
                "block": {
                    "kind": "subagent",
                    "job": job,
                    "head_text": head,
                    "foot_text": foot,
                    "role": role,
                    "failed": accent.lower() == self._theme.role("error").lower(),
                    "done": bool(done),
                },
            }
        )

    # ── live streaming markdown (assistant answer) ───────────────────────────
    def begin_stream(self, text: str) -> None:
        self._stream_text = text
        bid = self._next_id()
        self._stream_id = bid
        self._emit(
            {
                "op": "append",
                "id": bid,
                "block": {"kind": "markdown", "md": text, "streaming": True},
            }
        )

    def update_stream(self, text: str) -> None:
        self._stream_text = text
        if self._stream_id is None:
            self.begin_stream(text)
            return
        self._emit(
            {
                "op": "update",
                "id": self._stream_id,
                "block": {"kind": "markdown", "md": text, "streaming": True},
            }
        )

    def end_stream(self) -> None:
        if self._stream_id is None:
            return
        self._emit(
            {
                "op": "update",
                "id": self._stream_id,
                "block": {
                    "kind": "markdown",
                    "md": self._stream_text,
                    "streaming": False,
                },
            }
        )
        self._stream_id = None
        self._stream_text = ""

    def clear_running_tools(self) -> None:
        # No running-tool cards are mounted (we don't expose begin_tool), so a
        # dangling stream is the only thing to release at an interrupt boundary.
        if self._stream_id is not None:
            self.end_stream()


def _line_class(plain: str) -> str:
    """Semantic CSS class for a one-off ``write`` line (mirrors mockup classes)."""
    if not plain:
        return "inline"
    g = plain[0]
    if g == "↻":
        return "retry"
    if g == "✗":
        return "error"
    if plain.startswith("◔") or plain.startswith("◑"):
        return "md"  # plan head — keep it a plain accented line
    return "inline"


def _card_class(title_plain: str, classes: str) -> str:
    t = title_plain.strip()
    if _is_diff_card(t):
        return "diff-card"
    if t.startswith("Thought"):
        return "thought"
    if "Todos" in t or t.startswith("☰"):
        return "todos"
    if "Error (" in t:
        return "error"
    if classes and "tool-block" in classes:
        return "tool-block"
    return ""


def _line_html(text: "RenderableType", line_index: int) -> str:
    """Render only the *line_index*-th physical line of a Rich Text to HTML."""
    plain = renderable_to_text(text)
    lines = plain.split("\n")
    if line_index >= len(lines):
        return ""
    # Re-slice the Rich Text to the requested line so styling is preserved.
    try:
        from rich.text import Text

        if isinstance(text, Text):
            split = text.split("\n")
            if line_index < len(split):
                return renderable_to_html(split[line_index])
    except Exception:  # pragma: no cover - defensive
        pass
    from html import escape

    return escape(lines[line_index])


_COLOR = re.compile(r"color:(#[0-9a-fA-F]{6})")


def _first_color(html: str) -> str:
    """The first ``color:#rrggbb`` in *html* (the sub-agent role accent)."""
    m = _COLOR.search(html)
    return m.group(1) if m else ""


__all__ = ["WebLog", "EmitFn"]
