"""Scrollable read-only text viewer overlay (docs/reference/commands.md §8, I9/I11).

``TextScreen`` is the third L1 primitive (alongside ``SelectScreen`` and
``ConfirmScreen``): a modal, **scrollable, read-only** viewer for long reports —
``/config`` JSON, ``/diff`` git output, ``/help``, ``/doctor``, the cost/usage
tables, ``/session-tree`` — so they no longer flood the conversation scroll
region (MiMo's info/help/status dialogs).

Pushed with ``await app.push_screen_wait(TextScreen(...))``; resolves to ``None``.
Esc / q / Enter close it. The body is rendered monospaced and is scrollable with
↑/↓/PgUp/PgDn.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme


class TextScreen(ModalScreen[None]):
    """Modal, scrollable, read-only viewer for a long text report.

    Shares the main surface's shape language: a left amber accent bar over the
    panel wash (no round box) with a dim scrim behind. Colours are injected from
    the ``Theme`` roles in ``on_mount`` so it stays on the calibrated palette and
    still renders in a bare ``App`` without the ``$av-*`` variables."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("up", "scroll_up", "Up", show=False),
        Binding("down", "scroll_down", "Down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
    ]

    # Layout only — colours/borders are injected from the theme in on_mount.
    DEFAULT_CSS = """
    TextScreen { align: center middle; }
    TextScreen > Vertical {
        width: 84%; max-width: 110; height: auto; max-height: 84%;
        border: none; padding: 1 2;
    }
    TextScreen #text-title { text-style: bold; padding: 0 0 1 0; }
    TextScreen #text-scroll {
        height: auto; max-height: 30;
        border: none; padding: 0; scrollbar-size-vertical: 1;
    }
    TextScreen #text-hint { padding: 1 0 0 0; }
    """

    def __init__(
        self,
        body: str,
        *,
        title: str = "",
        theme: Theme | None = None,
    ) -> None:
        super().__init__()
        self._body = body
        self._title = title
        self._theme = theme or DEFAULT_THEME

    def compose(self) -> ComposeResult:
        with Vertical():
            if self._title:
                yield Static(self._title, id="text-title")
            with VerticalScroll(id="text-scroll"):
                yield Static(self._body or "(empty)", id="text-body", markup=False)
            yield Static("↑/↓ scroll · Esc / q close", id="text-hint")

    def on_mount(self) -> None:
        self._apply_theme()
        self.query_one("#text-scroll", VerticalScroll).focus()

    def _apply_theme(self) -> None:
        """Inject the palette: scrim + panel wash + amber accent bar."""
        t = self._theme
        self.styles.background = Color.parse(t.steps[0]).with_alpha(0.55)
        box = self.query_one(Vertical)
        box.styles.background = t.role("bg_panel")
        box.styles.border_left = ("heavy", t.role("accent"))
        if self._title:
            self.query_one("#text-title", Static).styles.color = t.role("accent")
        scroll = self.query_one("#text-scroll", VerticalScroll)
        scroll.styles.background = t.role("bg_panel")
        body = self.query_one("#text-body", Static)
        body.styles.background = t.role("bg_panel")
        body.styles.color = t.role("text") or t.steps[10]
        self.query_one("#text-hint", Static).styles.color = t.role("text_dim")

    # ── actions ────────────────────────────────────────────────────────────────
    def action_close(self) -> None:
        self.dismiss(None)

    def action_scroll_up(self) -> None:
        self.query_one("#text-scroll", VerticalScroll).scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one("#text-scroll", VerticalScroll).scroll_down()

    def action_page_up(self) -> None:
        self.query_one("#text-scroll", VerticalScroll).scroll_page_up()

    def action_page_down(self) -> None:
        self.query_one("#text-scroll", VerticalScroll).scroll_page_down()


__all__ = ["TextScreen"]
