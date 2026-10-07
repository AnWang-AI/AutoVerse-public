"""Yes/no confirmation overlay (docs/reference/commands.md §8, I8; MiMo ui/dialog-confirm).

``ConfirmScreen`` resolves to ``True`` (confirm) / ``False`` (cancel). Left/Right
move between the two buttons; Enter commits the focused one; ``y``/``n`` are
shortcuts; Esc cancels. Pushed via ``await app.push_screen_wait(ConfirmScreen(...))``.

Use for destructive commands (``/clear``, ``/rewind``) so the user gets a
structured confirmation overlay instead of an irreversible immediate action.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme


class ConfirmScreen(ModalScreen[bool]):
    """Confirm/cancel a destructive action; resolves to ``bool``.

    Shape language matches the main surface (cockpit aesthetic): NO round box —
    a left accent bar over the panel wash, a dim scrim behind it for depth, and
    active/inactive buttons distinguished by colour + a ``›`` marker rather than
    a bordered button box. All colours are injected from the ``Theme`` roles in
    ``on_mount`` so the dialog stays on the same calibrated palette as ``#log``
    (and so it still renders in a bare ``App`` without the ``$av-*`` vars).
    """

    BINDINGS = [
        ("left", "focus_confirm", "Confirm"),
        ("right", "focus_cancel", "Cancel"),
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
        ("enter", "commit", "Commit"),
    ]

    # Layout only — colours/borders are injected from the theme in on_mount.
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical {
        width: 60%; max-width: 80; height: auto;
        border: none; padding: 1 2;
    }
    ConfirmScreen #confirm-title { text-style: bold; padding: 0 0 1 0; }
    ConfirmScreen #confirm-message { padding: 0 0 1 0; }
    ConfirmScreen #confirm-buttons { height: auto; align: center middle; }
    ConfirmScreen .confirm-btn { padding: 0 2; margin: 0 3 0 0; }
    ConfirmScreen #confirm-hint { padding: 1 0 0 0; }
    """

    active: reactive[str] = reactive("confirm")  # "confirm" | "cancel"

    def __init__(
        self,
        message: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        theme: Theme | None = None,
    ) -> None:
        super().__init__()
        self._message = message
        self._title = title
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._theme = theme or DEFAULT_THEME

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, id="confirm-title")
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Static(self._confirm_label, id="btn-confirm", classes="confirm-btn")
                yield Static(self._cancel_label, id="btn-cancel", classes="confirm-btn")
            yield Static(
                "←/→ move · Enter commit · y/n shortcut · Esc cancel",
                id="confirm-hint",
            )

    def on_mount(self) -> None:
        self._apply_theme()
        self._refresh_buttons()

    def _apply_theme(self) -> None:
        """Inject the palette: scrim + panel wash + warning accent bar.

        ConfirmScreen guards destructive actions, so its accent rides the
        ``warning`` role (not the default amber) as a quiet "careful here" cue.
        """
        t = self._theme
        # Dim scrim: the main surface shows through at ~45%, so the dialog reads
        # as a layer floating above it (depth via brightness, not a border).
        self.styles.background = Color.parse(t.steps[0]).with_alpha(0.55)
        accent = t.role("warning") or t.role("accent")
        box = self.query_one(Vertical)
        box.styles.background = t.role("bg_panel")
        box.styles.border_left = ("heavy", accent)
        self.query_one("#confirm-title", Static).styles.color = accent
        self.query_one("#confirm-message", Static).styles.color = t.role("text")
        self.query_one("#confirm-hint", Static).styles.color = t.role("text_dim")

    def watch_active(self, _old: str, _new: str) -> None:
        # Guard: the reactive may settle before compose mounts the buttons.
        if self.is_mounted:
            self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        # Active vs inactive read by colour + weight + a leading ``›`` marker —
        # no bordered button box (that belongs to Textual's default chrome).
        t = self._theme
        accent = t.role("accent")
        muted = t.role("text_muted")
        confirm = self.query_one("#btn-confirm", Static)
        cancel = self.query_one("#btn-cancel", Static)
        c_active = self.active == "confirm"
        confirm.update(("› " if c_active else "  ") + self._confirm_label)
        confirm.styles.color = accent if c_active else muted
        confirm.styles.text_style = "bold" if c_active else "none"
        cancel.update(("› " if not c_active else "  ") + self._cancel_label)
        cancel.styles.color = accent if not c_active else muted
        cancel.styles.text_style = "bold" if not c_active else "none"

    def action_focus_confirm(self) -> None:
        self.active = "confirm"

    def action_focus_cancel(self) -> None:
        self.active = "cancel"

    def action_commit(self) -> None:
        self.dismiss(self.active == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = ["ConfirmScreen"]
