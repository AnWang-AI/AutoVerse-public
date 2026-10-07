"""Theme export — generate the WebUI ``:root`` CSS variables from ``Theme``.

theme.py is the single source of truth for colour (see docs/guides/using-webui.md);
the mockup's hand-written ``:root`` only approximated it. This module emits the SAME CSS
custom-property names the ported mockup stylesheet consumes (``--bg`` /
``--accent`` / ``--diff-add`` / ``--syn-kw`` …), each mapped to the live theme
role hex, so changing the theme JSON re-tints the whole web surface without
touching CSS. Served at ``/theme.css``.
"""

from __future__ import annotations

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme

# WebUI CSS var name → theme role name (or step index for the base background).
_VAR_TO_ROLE: dict[str, str] = {
    "bg-panel": "bg_panel",
    "bg-element": "bg_element",
    "border-subtle": "border_subtle",
    "border": "border",
    "text-dim": "text_dim",
    "text-muted": "text_muted",
    "text": "text",
    "accent": "accent",
    "accent-soft": "accent_soft",
    "accent-cool": "accent_cool",
    "secondary": "secondary",
    "success": "success",
    "warning": "warning",
    "error": "error",
    "info": "info",
    "tool-running": "tool_running",
    # diff palette
    "diff-add": "diff_add",
    "diff-del": "diff_del",
    "diff-add-bg": "diff_add_bg",
    "diff-del-bg": "diff_del_bg",
    "diff-add-emph": "diff_add_emph_bg",
    "diff-del-emph": "diff_del_emph_bg",
    "diff-ctx-bg": "diff_context_bg",
    "diff-ctx": "diff_context",
    "diff-hunk": "diff_hunk",
    "diff-lineno": "diff_lineno",
    "diff-lineno-add-bg": "diff_lineno_add_bg",
    "diff-lineno-del-bg": "diff_lineno_del_bg",
    "diff-lineno-bg": "diff_lineno_bg",
    # syntax palette
    "syn-default": "syntax_default",
    "syn-kw": "syntax_keyword",
    "syn-str": "syntax_string",
    "syn-com": "syntax_comment",
    "syn-num": "syntax_number",
    "syn-fn": "syntax_function",
    "syn-cls": "syntax_class",
    "syn-const": "syntax_constant",
    "syn-op": "syntax_operator",
    "syn-builtin": "syntax_builtin",
}


def theme_css(theme: Theme | None = None) -> str:
    theme = theme or DEFAULT_THEME
    lines = [":root {"]
    # Base background = darkest step (no pure black); panels come from roles.
    lines.append(f"  --bg: {theme.steps[0]};")
    for var, role in _VAR_TO_ROLE.items():
        hexc = theme.role(role)
        if hexc:
            lines.append(f"  --{var}: {hexc};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def theme_config(
    theme: Theme | None = None, *, version: str = "0.1.0"
) -> dict[str, str | list[str]]:
    """Non-colour theme facts the frontend needs (spinner frames, version)."""
    theme = theme or DEFAULT_THEME
    return {
        "spinner_frames": list(theme.spinner_frames),
        "version": version,
        "name": theme.name,
    }


__all__ = ["theme_css", "theme_config"]
