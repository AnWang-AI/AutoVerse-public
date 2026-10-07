"""ToolRenderer registry — second render axis, keyed by tool name (R2, P2).

Tool rendering is orthogonal to event-type rendering: adding a tool renderer
never touches the event dispatcher, and adding an event type never touches any
tool renderer. A ``ToolRenderer`` is a callable ``(ToolEndPayload, ctx) -> str``.
Unregistered tools fall back to :data:`DEFAULT_TOOL_RENDERER` (a generic card),
so a brand-new tool is still visualised without frontend changes (P8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from autoverse.frontends.repl.tool_error import error_text, is_benign_error
from autoverse.types.events import ToolEndPayload

if TYPE_CHECKING:
    from autoverse.frontends.repl.renderer import RenderContext

ToolRenderer = Callable[[ToolEndPayload, "RenderContext"], str]

# Per-tool-name renderers. A plain dict so registration is auditable (P1).
TOOL_RENDERERS: dict[str, ToolRenderer] = {}

_TOOL_ICONS: dict[str, str] = {
    "read_file": "→", "Read": "→",
    "glob": "✱", "Glob": "✱",
    "grep": "✱", "Grep": "✱",
    "web_fetch": "%", "WebFetch": "%",
    "web_search": "◈", "WebSearch": "◈",
    "tool_search": "◇",
    "bash": "$", "Bash": "$",
    "edit_file": "←", "Edit": "←",
    "write_file": "←", "Write": "←", "MultiEdit": "←",
    "todo_write": "☰", "TodoWrite": "☰",
    "agent": "│", "Agent": "│",
    "JobOutput": "▸",
}


def _generic_tool_renderer(payload: ToolEndPayload, ctx: "RenderContext") -> str:
    """Default card for an unregistered tool — MiMo ``GenericTool`` parity (P8).

    One muted line ``icon Tool``; status is encoded by colour + form, not a
    ``[ok]``/``[err]`` text tag (§3.9.3 convention). Benign failures are struck
    through; hard errors append a red ``Error (N lines)`` line below (MiMo
    ``CollapsibleError``). Full output stays in the payload / trace.
    """
    icon = _TOOL_ICONS.get(payload.tool_name, "●")
    benign = is_benign_error(payload)
    line = ctx.theme.style(
        f"{icon} {payload.tool_name}",
        "text_muted",
        ansi=ctx.ansi,
        strike=benign,
    )
    if payload.is_error and not benign:
        text = error_text(payload)
        n = len(text.splitlines()) or 1
        label = f"Error ({n} {'line' if n == 1 else 'lines'})"
        line += "\n" + ctx.theme.style(label, "tool_err", ansi=ctx.ansi)
    return line


DEFAULT_TOOL_RENDERER: ToolRenderer = _generic_tool_renderer


def register_tool_renderer(name: str, renderer: ToolRenderer) -> None:
    """Register *renderer* for tool *name* (R2). Last registration wins."""
    TOOL_RENDERERS[name] = renderer


def tool_renderer(*names: str) -> Callable[[ToolRenderer], ToolRenderer]:
    """Decorator registering a ToolRenderer for one or more tool names."""

    def deco(fn: ToolRenderer) -> ToolRenderer:
        for n in names:
            register_tool_renderer(n, fn)
        return fn

    return deco


def get_tool_renderer(name: str) -> ToolRenderer:
    """Return the registered renderer for *name*, else the generic default."""
    return TOOL_RENDERERS.get(name, DEFAULT_TOOL_RENDERER)


__all__ = [
    "ToolRenderer",
    "TOOL_RENDERERS",
    "DEFAULT_TOOL_RENDERER",
    "register_tool_renderer",
    "tool_renderer",
    "get_tool_renderer",
]
