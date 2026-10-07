"""Single-line text-input overlay (docs/architecture/frontend-adapters.md §5.4 rename).

``PromptScreen`` resolves to the entered string (Enter commits) or ``None``
(Esc cancels / empty). Pushed via ``await app.push_screen_wait(PromptScreen(...))``.
Shape language matches :class:`ConfirmScreen`: a left accent bar over the panel
wash, a dim scrim behind, no rounded box — colours injected from the ``Theme``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme


class PromptScreen(ModalScreen[str | None]):
    """Prompt for one line of text; resolves to the value or ``None``."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    PromptScreen > Vertical {
        width: 60%; max-width: 80; height: auto; border: none; padding: 1 2;
    }
    PromptScreen #prompt-title { text-style: bold; padding: 0 0 1 0; }
    PromptScreen #prompt-input { margin: 0 0 1 0; }
    PromptScreen #prompt-hint { padding: 1 0 0 0; }
    """

    def __init__(
        self,
        title: str,
        *,
        placeholder: str = "",
        initial: str = "",
        password: bool = False,
        theme: Theme | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._initial = initial
        self._password = password
        self._theme = theme or DEFAULT_THEME

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, id="prompt-title")
            yield Input(
                value=self._initial,
                placeholder=self._placeholder,
                password=self._password,
                id="prompt-input",
            )
            yield Static("Enter 确认 · Esc 取消", id="prompt-hint")

    def on_mount(self) -> None:
        t = self._theme
        self.styles.background = Color.parse(t.steps[0]).with_alpha(0.55)
        accent = t.role("accent")
        box = self.query_one(Vertical)
        box.styles.background = t.role("bg_panel")
        box.styles.border_left = ("heavy", accent)
        self.query_one("#prompt-title", Static).styles.color = accent
        self.query_one("#prompt-hint", Static).styles.color = t.role("text_dim")
        self.query_one("#prompt-input", Input).focus()

    def action_commit(self) -> None:
        value = self.query_one("#prompt-input", Input).value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_commit()


__all__ = ["PromptScreen"]
