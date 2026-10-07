"""Transient corner notification (docs/reference/commands.md §8, I7; MiMo ui/toast.tsx).

A ``Toast`` is a small ``Static`` docked **bottom-right** in its mounted
container that fades in, lingers, then fades out. In the full TUI it is mounted
under ``#main`` so it stays near the input region rather than drifting into the
session rail or right sidebar. Use it for feedback that should *not* clutter the
conversation scroll region — "model switched", "copied", "command failed".

Visual language matches the main surface (NOT Textual's default modal chrome):
a left accent bar + the panel background, with the bar colour carrying the
variant semantic (info / success / warning / error). All colours come from the
``Theme`` roles via ``on_mount`` injection — never Textual's derived ``$panel``/
``$foreground`` (which drift off the calibrated 12-step scale).

Mount once and call :meth:`show`; or use the :func:`toast` app helper which
mounts a fresh widget per message (auto-removed after it fades out).
"""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.widgets import Static

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme

ToastVariant = Literal["info", "success", "warning", "error"]

# Variant → the theme role that colours the left accent bar. The bar carries the
# semantic; the text stays neutral (info quietest of all) — restraint over a
# fully-coloured banner (taste-skill: max 1 loud signal at a time).
_VARIANT_BAR: dict[str, str] = {
    "info": "text_dim",
    "success": "success",
    "warning": "warning",
    "error": "tool_err",
}

# A small leading glyph reinforces the bar colour for quick scanning; info has
# none (it should read as the calmest, just a muted line).
_VARIANT_GLYPH: dict[str, str] = {
    "info": "",
    "success": "✓ ",
    "warning": "⚠ ",
    "error": "✗ ",
}


class Toast(Static):
    """A self-removing bottom-right notification (fades in/out)."""

    DEFAULT_CSS = """
    Toast {
        width: auto; max-width: 50; height: auto;
        margin: 0 2 1 0; padding: 0 1;
    }
    """

    def __init__(
        self,
        message: str,
        *,
        variant: ToastVariant = "info",
        timeout: float = 3.0,
        theme: Theme | None = None,
    ) -> None:
        super().__init__()
        self._message = message
        self._variant = variant
        self._timeout = timeout
        self._theme = theme or DEFAULT_THEME

    def compose(self) -> ComposeResult:
        return iter(())

    def on_mount(self) -> None:
        t = self._theme
        bar = t.role(_VARIANT_BAR.get(self._variant, "text_dim"))
        # Same shape language as the main surface: a left accent bar over the
        # panel wash (no box, no round border).
        self.styles.background = t.role("bg_panel")
        if bar:
            self.styles.border_left = ("heavy", bar)
        self.styles.color = (
            t.role("text_muted") if self._variant == "info" else t.role("text")
        )
        glyph = _VARIANT_GLYPH.get(self._variant, "")
        self.update(f"{glyph}{self._message}")
        # Fade in so the notice arrives softly instead of hard-cutting in.
        self.styles.opacity = 0.0
        self.styles.animate("opacity", value=1.0, duration=0.25)
        if self._timeout > 0:
            self.set_timer(self._timeout, self._dismiss)

    def _dismiss(self) -> None:
        """Fade out, then remove (a hard removal would pop, defeating the point)."""
        self.styles.animate(
            "opacity", value=0.0, duration=0.3, on_complete=self.remove
        )


__all__ = ["Toast", "ToastVariant"]
