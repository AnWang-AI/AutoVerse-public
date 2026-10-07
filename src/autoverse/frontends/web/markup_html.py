"""Rich renderable / console-markup → HTML (web-side faithful colour bridge).

The Web frontend reuses ``TuiEventSink`` verbatim, so it receives the SAME
pre-styled renderables the Textual ConversationLog gets: ``rich.text.Text``
(parsed from ANSI), ``rich.markdown.Markdown``, ``rich.rule.Rule``, and a few
console-markup title strings that reference Textual theme variables
(``[$av-text-muted]…[/$av-text-muted]``). This module converts those into HTML
spans coloured straight from :class:`~autoverse.frontends.repl.theme.Theme`, so
the browser shows the exact same hues the TUI shows (theme.py is the single
source of truth — the mockup CSS only approximated it).

Two converters:

* :func:`renderable_to_html` — render any Rich renderable to coloured HTML by
  walking the console segment stream (handles fg + bg truecolor, bold, strike,
  italic). Whitespace is preserved; the caller wraps it in a ``white-space:
  pre-wrap`` container, matching the TUI's monospace block look.
* :func:`markup_to_html` — a tiny parser for the ``[$av-role]`` / ``[strike]`` /
  ``[bold]`` console markup the sink uses for collapsible card titles.
"""

from __future__ import annotations

import re
from html import escape
from io import StringIO
from typing import TYPE_CHECKING, cast

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console
    from rich.console import RenderableType
    from rich.segment import Segment


# A wide, non-terminal console used purely to flatten a renderable into styled
# segments. ``color_system="truecolor"`` keeps the 24-bit colours the renderers
# emit; a generous width avoids premature wrapping (the browser re-wraps).
def _console() -> "Console":
    from rich.console import Console

    return Console(
        file=StringIO(),
        force_terminal=False,
        color_system="truecolor",
        width=240,
        height=2000,
        no_color=False,
        legacy_windows=False,
        markup=False,
        emoji=False,
        highlight=False,
    )


def _hex(color: object) -> str | None:
    """Best-effort ``#rrggbb`` for a Rich Color; ``None`` for the default."""
    if color is None:
        return None
    try:
        triplet = color.get_truecolor()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        return None
    if triplet is None:
        return None
    try:
        return cast(str, triplet.hex)
    except Exception:  # pragma: no cover - defensive
        return None


def _style_css(style: object) -> str:
    """CSS declarations for one Rich ``Style`` (fg/bg/bold/strike/italic)."""
    if style is None:
        return ""
    decls: list[str] = []
    fg = _hex(getattr(style, "color", None))
    if fg:
        decls.append(f"color:{fg}")
    bg = _hex(getattr(style, "bgcolor", None))
    if bg:
        decls.append(f"background-color:{bg}")
    if getattr(style, "bold", False):
        decls.append("font-weight:700")
    if getattr(style, "italic", False):
        decls.append("font-style:italic")
    if getattr(style, "strike", False) or getattr(style, "strikethrough", False):
        decls.append("text-decoration:line-through")
    if getattr(style, "underline", False):
        decls.append("text-decoration:underline")
    return ";".join(decls)


def _segments_to_html(segments: "list[Segment]") -> str:
    parts: list[str] = []
    for seg in segments:
        if getattr(seg, "control", None):
            continue
        text = seg.text
        if not text:
            continue
        css = _style_css(getattr(seg, "style", None))
        safe = escape(text)
        if css:
            parts.append(f'<span style="{css}">{safe}</span>')
        else:
            parts.append(safe)
    html = "".join(parts)
    # Rich appends a trailing newline per top-level renderable line; trim only the
    # single trailing newline so multi-line cards keep their internal breaks.
    if html.endswith("\n"):
        html = html[:-1]
    return html


def renderable_to_html(renderable: "RenderableType") -> str:
    """Flatten *renderable* into coloured HTML (whitespace preserved)."""
    try:
        con = _console()
        segments = list(con.render(renderable, con.options))
        return _segments_to_html(segments)
    except Exception:  # pragma: no cover - defensive: never break the stream
        plain = getattr(renderable, "plain", None)
        if isinstance(plain, str):
            return escape(plain)
        return escape(str(renderable))


def renderable_to_text(renderable: "RenderableType") -> str:
    """Plain-text fallback for classification heuristics (no styling)."""
    plain = getattr(renderable, "plain", None)
    if isinstance(plain, str):
        return plain
    try:
        con = _console()
        return "".join(seg.text for seg in con.render(renderable, con.options))
    except Exception:  # pragma: no cover - defensive
        return str(renderable)


# ── console-markup ([$av-role] / [strike] / [bold]) → HTML ──────────────────
_TAG = re.compile(r"\[(/?)([^\]]*)\]")
_MARKUP_NAMES = frozenset({"strike", "s", "bold", "b", "italic", "i", "dim"})


def _is_markup_token(token: str) -> bool:
    token = token.strip()
    return token.startswith("$av-") or token in _MARKUP_NAMES


def _role_to_css(token: str, theme: Theme) -> str:
    """CSS for one markup token: ``$av-text-muted`` / ``strike`` / ``bold`` …"""
    token = token.strip()
    if not token:
        return ""
    if token.startswith("$av-"):
        role = token[len("$av-"):].replace("-", "_")
        hexc = theme.role(role)
        return f"color:{hexc}" if hexc else ""
    if token in ("strike", "s"):
        return "text-decoration:line-through"
    if token in ("bold", "b"):
        return "font-weight:700"
    if token in ("italic", "i"):
        return "font-style:italic"
    if token == "dim":
        return f"color:{theme.role('text_dim')}"
    return ""


def markup_to_html(markup: str, theme: Theme | None = None) -> str:
    """Convert the sink's ``[$av-role]…[/…]`` console markup to HTML spans."""
    theme = theme or DEFAULT_THEME
    out: list[str] = []
    stack: list[str] = []  # css strings, innermost last
    pos = 0
    for m in _TAG.finditer(markup):
        if m.start() > pos:
            out.append(_emit(markup[pos:m.start()], stack))
        closing, token = m.group(1), m.group(2)
        # Shell commands and regexes routinely contain bracket expressions such
        # as ``grep "^[-+]"``. They are literal title text, not Rich markup.
        # Preserve every unknown bracket token verbatim.
        if not _is_markup_token(token):
            out.append(_emit(m.group(0), stack))
            pos = m.end()
            continue
        if closing:
            if stack:
                stack.pop()
        else:
            css = _role_to_css(token, theme)
            stack.append(css)
        pos = m.end()
    if pos < len(markup):
        out.append(_emit(markup[pos:], stack))
    return "".join(out)


def _emit(text: str, stack: list[str]) -> str:
    if not text:
        return ""
    css = ";".join(c for c in stack if c)
    safe = escape(text)
    return f'<span style="{css}">{safe}</span>' if css else safe


# ── diff-aware HTML renderer (CSS-class-based, light/dark mode safe) ──────────

# Diff line pattern: optional leading spaces + digits + space + sign(+/-/ ) + space + code.
_DIFF_LINE_RE = re.compile(r"^(\s*\d+)\s([+\- ])")
# Diagnostics prefix: spaces + ⚠/✗/· + space.
_DIAG_RE = re.compile(r"^\s*[\u26a0\u2717\u00b7]")


def _find_sign(stripped: str) -> tuple[int, str] | None:
    """Find (sign_position, sign_char) in a diff line like ``'42 + code'``.

    Returns ``None`` if the line doesn't match the diff format."""
    m = _DIFF_LINE_RE.match(stripped)
    if m:
        sign_pos = m.start(2)
        return sign_pos, stripped[sign_pos]
    return None


def _is_diag_line(stripped: str) -> bool:
    """Check if a line is a diagnostics line (⚠/✗/· prefix)."""
    return bool(_DIAG_RE.match(stripped))


def renderable_to_diff_html(renderable: "RenderableType") -> str:
    """Convert a diff body (Rich Text) to HTML with CSS classes for theming.

    Outputs structured HTML using ``.diff``, ``.row``, ``.add``/``.del``/``.ctx``/
    ``.hunk`` classes so the CSS variables handle light/dark mode colours. Falls
    back to :func:`renderable_to_html` when the content doesn't look like a diff
    or when the diff uses split view (``│`` separator)."""
    try:
        from html import escape

        from rich.text import Text

        # The live tool renderer commonly passes a Rich Group rather than a
        # bare Text. Rendering that Group straight to HTML preserves its dark
        # terminal background colours as inline styles, which then override the
        # WebUI's light theme. Diff classification only needs the plain rows, so
        # flatten non-Text renderables first and always emit semantic CSS classes.
        if isinstance(renderable, Text):
            lines: list[Text | str] = list(renderable.split("\n"))
        else:
            lines = list(renderable_to_text(renderable).splitlines())

        is_diff = False
        has_split = False
        for line in lines:
            p = line.plain if isinstance(line, Text) else line
            if "\u2502" in p:  # │ split-view separator
                has_split = True
            ps = p.strip()
            if ps.startswith("@@"):
                is_diff = True
            if _find_sign(ps) is not None:
                is_diff = True

        if not is_diff:
            return renderable_to_html(renderable)
        if has_split:
            plain = renderable_to_text(renderable).rstrip()
            return f'<div class="diff diff-split"><pre>{escape(plain)}</pre></div>'

        parts: list[str] = ['<div class="diff">']
        for line in lines:
            plain_line = line.plain if isinstance(line, Text) else line
            stripped = plain_line.strip()
            if not stripped:
                continue

            if stripped.startswith("@@"):
                parts.append(
                    f'<div class="row hunk"><span class="cd">{escape(stripped)}</span></div>'
                )
                continue

            if _is_diag_line(stripped):
                parts.append(
                    renderable_to_html(line)
                    if isinstance(line, Text)
                    else escape(stripped)
                )
                continue

            si = _find_sign(stripped)
            if si is None:
                # Rich may trim the trailing "  " from a blank context row,
                # leaving only its line number (for example ``132``). Keep that
                # number in the gutter instead of rendering it as source code.
                if stripped.isdigit():
                    parts.append(
                        f'<div class="row ctx"><span class="ln">{escape(stripped)}</span>'
                        '<span class="sg"> </span><span class="cd"></span></div>'
                    )
                    continue
                parts.append(
                    f'<div class="row ctx"><span class="cd">{escape(stripped)}</span></div>'
                )
                continue

            sign_pos, sign_char = si
            lineno = stripped[:sign_pos].rstrip()
            code_start = sign_pos + 2  # skip sign char + space
            code = stripped[code_start:].rstrip() if code_start < len(stripped) else ""

            kind = {"+": "add", "-": "del"}.get(sign_char, "ctx")
            ln_html = f'<span class="ln">{escape(lineno)}</span>' if lineno else ""
            sg_html = f'<span class="sg">{escape(sign_char)}</span>'
            cd_html = f'<span class="cd">{escape(code)}</span>'
            parts.append(f'<div class="row {kind}">{ln_html}{sg_html}{cd_html}</div>')

        parts.append("</div>")
        return "\n".join(parts)
    except Exception:  # pragma: no cover - defensive: never break the stream
        return renderable_to_html(renderable)


__all__ = [
    "renderable_to_html",
    "renderable_to_diff_html",
    "renderable_to_text",
    "markup_to_html",
]
