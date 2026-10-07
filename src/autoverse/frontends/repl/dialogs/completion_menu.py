"""Inline slash-command completion menu (docs/reference/commands.md §8, I3).

Unlike :class:`SelectScreen` (a full modal overlay), ``CompletionMenu`` is a
*non-modal* ``OptionList`` that sits just above the input box and updates as the
user types ``/...`` — the MiMo ``prompt/autocomplete.tsx`` (``visible:"/"``)
experience. It does not steal focus from the input: the app forwards ↑/↓/Tab/
Enter to it while it is visible, and hides it otherwise.

Data comes from the command registry's ``completion_items()`` — ``(name,
summary, hints)`` — filtered fuzzily on the text after ``/``.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from autoverse.frontends.repl.dialogs.fuzzy import fuzzy_filter
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme

# (name, summary, hints) — the shape of CommandRegistry.completion_items().
CompletionItem = tuple[str, str, tuple[str, ...]]


class CompletionMenu(OptionList):
    """Inline, non-modal slash-command completion list above the input."""

    DEFAULT_CSS = """
    CompletionMenu {
        layer: overlay; dock: bottom; height: auto; max-height: 10;
        margin: 0 1 7 3; background: $panel; border: round $panel-lighten-2;
        padding: 0; display: none;
    }
    CompletionMenu.-visible { display: block; }
    """

    def __init__(
        self,
        items: tuple[CompletionItem, ...] = (),
        *,
        theme: Theme | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._items = items
        self._theme = theme or DEFAULT_THEME
        self._filtered: list[CompletionItem] = []
        self.can_focus = False  # never steal focus from the input

    def set_items(self, items: tuple[CompletionItem, ...]) -> None:
        self._items = items

    def set_bottom_offset(self, rows: int) -> None:
        """Float the menu *rows* lines above the screen bottom so it sits flush
        above the input frame instead of covering it.

        The input box grows 1→6 lines, so a fixed ``margin-bottom`` would either
        overlap the input (too small) or leave a gap (too large). The app passes
        the live height of everything below the menu. Mirrors MiMo's
        ``top = anchor.y - height`` — pop up above the anchor, never cover it.
        Left/right margins stay aligned with the input frame (left 3, right 1).
        """
        self.styles.margin = (0, 1, max(0, rows), 3)

    # ── visibility / population ────────────────────────────────────────────────
    def update_for(self, text: str) -> bool:
        """Refresh for the current input *text*. Returns True if the menu shows.

        Shows only when *text* starts with ``/`` and has no space yet (we're
        still typing the command word). Otherwise hides.
        """
        if not text.startswith("/") or " " in text:
            self.hide()
            return False
        query = text[1:]
        self._filtered = fuzzy_filter(
            query, list(self._items), key=lambda it: it[0].lstrip("/")
        )
        if not self._filtered:
            self.hide()
            return False
        self.clear_options()
        for name, summary, hints in self._filtered:
            self.add_option(Option(self._row(name, summary, hints)))
        self.highlighted = 0
        self.add_class("-visible")
        return True

    def _row(self, name: str, summary: str, hints: tuple[str, ...]) -> Text:
        t = self._theme
        row = Text()
        row.append(name, style=t.role("accent"))
        if hints:
            row.append(" " + " ".join(hints), style=t.role("text_dim"))
        if summary:
            row.append("  " + summary, style=t.role("text_muted"))
        return row

    def hide(self) -> None:
        self.remove_class("-visible")
        self._filtered = []

    @property
    def visible_now(self) -> bool:
        return self.has_class("-visible")

    def move_down(self) -> None:
        self.action_cursor_down()

    def move_up(self) -> None:
        self.action_cursor_up()

    def current_name(self) -> str | None:
        """The highlighted command name (e.g. ``/model``), or None."""
        if not self.visible_now or self.highlighted is None:
            return None
        if 0 <= self.highlighted < len(self._filtered):
            return self._filtered[self.highlighted][0]
        return None


__all__ = ["CompletionMenu", "CompletionItem"]
