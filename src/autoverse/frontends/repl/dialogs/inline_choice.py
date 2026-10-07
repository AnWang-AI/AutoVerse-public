"""Inline choice menu — non-modal Q&A / approval list above the input (gap12/B1).

``InlineChoiceMenu`` is the inline sibling of :class:`SelectScreen`: instead of a
full-screen modal, it docks just above the input box (exactly like the slash
``CompletionMenu``) and is driven by keys the app forwards while it is visible.
It has **no fuzzy filter** — the question's options are few and meaningful, so the
user just arrows through them — and it never steals focus from the input, so the
"type your own answer" escape hatch can flow straight into the main input box.

It backs two interactions that share the same shape:

* **AskUserQuestion** — single/multi choice plus a synthetic "✎ Other" row that
  hands free-text entry off to the input box.
* **Permission approval** — a fixed ``Allow once / Always allow / Deny`` list with
  ``y``/``a``/``n`` shortcuts.

The menu holds no async machinery: the app owns an ``asyncio.Future`` and resolves
it from the forwarded key handlers (the same pattern the old ``push_screen_wait``
modal used, minus the full-screen takeover).
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme


@dataclass(frozen=True)
class ChoiceItem:
    """One selectable row in an :class:`InlineChoiceMenu`."""

    value: str
    label: str
    shortcut: str = ""        # optional single-key accelerator (e.g. "y")
    is_custom: bool = False   # the "type your own answer" escape hatch


class InlineChoiceMenu(OptionList):
    """Inline, non-modal choice list docked above the input box.

    Visibility is toggled with the ``-visible`` class; the app forwards
    navigation/selection keys to :meth:`move_up` / :meth:`move_down` /
    :meth:`toggle_current` and reads :meth:`current_value` / :meth:`checked_values`
    to resolve its result Future.
    """

    # Sentinel value flagging the "type your own answer" row (AskUserQuestion).
    CUSTOM = "\x00av-choice-custom\x00"

    DEFAULT_CSS = """
    InlineChoiceMenu {
        layer: overlay; dock: bottom; height: auto; max-height: 14;
        margin: 0 1 7 3; background: $panel; border: round $panel-lighten-2;
        padding: 0; display: none;
    }
    InlineChoiceMenu.-visible { display: block; }
    """

    def __init__(self, *, theme: Theme | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._theme = theme or DEFAULT_THEME
        self.can_focus = False  # never steal focus from the input box
        self.multi = False
        self._prompt = ""
        self._items: list[ChoiceItem] = []
        self._checked: list[str] = []           # checked values (multi mode)
        self._row_value: dict[int, str] = {}     # option-index → value (selectable rows)

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def open(
        self, prompt: str, items: list[ChoiceItem], *, multi: bool = False
    ) -> None:
        """Populate and show the menu for a fresh question."""
        self._prompt = prompt
        self._items = list(items)
        self.multi = multi
        self._checked = []
        self._render_rows()
        self.add_class("-visible")

    def reopen(self) -> None:
        """Re-show the last question (e.g. after the user cancels free-text entry)."""
        self._render_rows()
        self.add_class("-visible")

    def close(self) -> None:
        self.remove_class("-visible")

    @property
    def picking_now(self) -> bool:
        return self.has_class("-visible")

    # ── rendering ───────────────────────────────────────────────────────────────
    def _render_rows(self) -> None:
        self.clear_options()
        self._row_value = {}
        idx = 0
        if self._prompt:
            self.add_option(Option(self._prompt_text(), id=None, disabled=True))
            idx += 1
        first_selectable: int | None = None
        for item in self._items:
            self.add_option(Option(self._row_text(item), id=f"ci-{idx}"))
            self._row_value[idx] = item.value
            if first_selectable is None:
                first_selectable = idx
            idx += 1
        self.add_option(Option(self._hint_text(), id=None, disabled=True))
        if first_selectable is not None:
            self.highlighted = first_selectable

    def _prompt_text(self) -> Text:
        return Text(self._prompt, style=self._theme.role("text_muted"))

    def _row_text(self, item: ChoiceItem) -> Text:
        t = self._theme
        row = Text()
        if self.multi:
            checked = item.value in self._checked
            row.append(
                "[x] " if checked else "[ ] ",
                style=t.role("accent") if checked else t.role("text_dim"),
            )
        if item.shortcut:
            row.append(f"({item.shortcut}) ", style=t.role("text_dim"))
        row.append(
            item.label,
            style=t.role("accent") if item.is_custom else t.role("text"),
        )
        return row

    def _hint_text(self) -> Text:
        hint = (
            "↑/↓ move · Space toggle · Enter confirm · Esc cancel"
            if self.multi
            else "↑/↓ move · Enter select · Esc cancel"
        )
        return Text(hint, style=self._theme.role("text_dim"))

    def set_bottom_offset(self, rows: int) -> None:
        """Float the menu *rows* lines above the screen bottom (flush above input).

        Mirrors :meth:`CompletionMenu.set_bottom_offset`; the app passes the live
        height of everything below the menu so it glues to the input frame as the
        box grows. Left/right margins stay aligned with the input (left 3, right 1).
        """
        self.styles.margin = (0, 1, max(0, rows), 3)

    def reserved_rows(self) -> int:
        """Rows this overlay can cover, including border and hint."""
        content_rows = len(self._items) + 1  # options + hint
        if self._prompt:
            content_rows += 1
        return min(14, content_rows + 2)

    # ── navigation / selection ───────────────────────────────────────────────────
    def move_up(self) -> None:
        self.action_cursor_up()

    def move_down(self) -> None:
        self.action_cursor_down()

    def current_value(self) -> str | None:
        if self.highlighted is None:
            return None
        return self._row_value.get(self.highlighted)

    def current_is_custom(self) -> bool:
        return self.current_value() == self.CUSTOM

    def toggle_current(self) -> None:
        value = self.current_value()
        if value is None:
            return
        if value in self._checked:
            self._checked.remove(value)
        else:
            self._checked.append(value)
        keep = self.highlighted
        self._render_rows()
        if keep is not None:
            self.highlighted = keep

    def checked_values(self) -> list[str]:
        """Checked option values in their original (item) order."""
        return [it.value for it in self._items if it.value in self._checked]

    def value_for_shortcut(self, key: str) -> str | None:
        for item in self._items:
            if item.shortcut and item.shortcut == key:
                return item.value
        return None


__all__ = ["InlineChoiceMenu", "ChoiceItem"]
