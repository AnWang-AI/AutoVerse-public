"""Generic searchable selection overlay (docs/reference/commands.md §8, I1/I6/I11).

``SelectScreen[T]`` is the L1 foundation every other command overlay builds on
(MiMo ``ui/dialog-select.tsx``). It renders:

* a title bar,
* a fuzzy-filter ``Input`` (type to narrow; I14),
* a scrollable ``OptionList`` of results with per-item ``title`` /
  ``description`` / ``footer`` / ``disabled`` / ``current`` highlight and
  optional ``category`` group headers (I11),
* a hint footer.

Pushed with ``await app.push_screen_wait(SelectScreen(...))`` (same pattern as
``PermissionScreen``); resolves to the selected option's ``value`` (type ``T``)
or ``None`` if dismissed with Esc.

When constructed with ``multi=True`` it becomes a multi-select form: Space
toggles the highlighted row, Enter confirms, and the screen resolves to a
``list`` of the checked ``value`` s (empty list if none checked, ``None`` only
when dismissed with Esc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from autoverse.frontends.repl.dialogs.fuzzy import fuzzy_filter
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme

T = TypeVar("T")


@dataclass(frozen=True)
class SelectOption(Generic[T]):
    """One row in a :class:`SelectScreen` (MiMo ``DialogSelectOption``)."""

    title: str
    value: T
    description: str = ""
    footer: str = ""
    category: str = ""
    disabled: bool = False
    current: bool = False
    # The string fuzzy-matching runs against (defaults to title + description).
    search_key: str = ""

    def key(self) -> str:
        return self.search_key or f"{self.title} {self.description}".strip()


# A non-selectable group-header sentinel rendered as a dim category label.
_HEADER_PREFIX = "\x00header\x00"


class SelectScreen(ModalScreen["T | list[T] | None"], Generic[T]):
    """Searchable, grouped, keyboard-navigable selection overlay.

    Resolves to a single ``T`` (single mode), a ``list[T]`` of checked values
    (multi mode), or ``None`` if dismissed with Esc.
    """

    BINDINGS = [
        ("escape", "dismiss_none", "Cancel"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("space", "toggle_checked", "Toggle"),
        ("ctrl+s", "confirm_multi", "Confirm"),
    ]

    # Layout only — colours/borders are injected from the theme in on_mount so
    # the overlay stays on the same calibrated palette as the main surface (and
    # still renders in a bare ``App`` that lacks the ``$av-*`` variables).
    DEFAULT_CSS = """
    SelectScreen { align: center middle; }
    SelectScreen > Vertical {
        width: 92%; max-width: 124; height: auto; max-height: 80%;
        border: none; padding: 1 2;
    }
    SelectScreen #dialog-title { text-style: bold; padding: 0 0 1 0; }
    SelectScreen #dialog-filter { border: none; margin: 0 0 1 0; }
    SelectScreen OptionList {
        height: auto; max-height: 18; border: none; padding: 0;
    }
    SelectScreen #dialog-hint { padding: 1 0 0 0; }
    """

    def __init__(
        self,
        options: list[SelectOption[T]],
        *,
        title: str = "Select",
        placeholder: str = "type to filter…",
        theme: Theme | None = None,
        show_categories: bool = True,
        multi: bool = False,
    ) -> None:
        super().__init__()
        self._options = options
        self._title = title
        self._placeholder = placeholder
        self._theme = theme or DEFAULT_THEME
        self._show_categories = show_categories
        self._multi = multi
        # Maps each OptionList option-id back to its SelectOption.
        self._by_id: dict[str, SelectOption[T]] = {}
        # Maps each OptionList option-id to the option's stable index in
        # ``self._options`` (survives filter rebuilds, which re-issue opt-N ids).
        self._idx_by_oid: dict[str, int] = {}
        self._stable_idx: dict[int, int] = {id(o): i for i, o in enumerate(options)}
        # Stable indices of checked options in multi-select mode (insertion order).
        self._checked: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, id="dialog-title")
            yield Input(placeholder=self._placeholder, id="dialog-filter")
            yield OptionList(id="dialog-filter-list")
            hint = (
                "↑/↓ move · Space/Enter toggle · Ctrl+S confirm · Esc cancel"
                if self._multi
                else "↑/↓ move · Enter select · Esc cancel"
            )
            yield Static(hint, id="dialog-hint")

    def on_mount(self) -> None:
        self._apply_theme()
        self._rebuild("")
        # Single mode focuses the filter box (type-to-narrow is the primary
        # action). Multi mode focuses the list so Space/arrows act on rows; the
        # filter box is still reachable by clicking or Tab.
        if self._multi:
            self.query_one("#dialog-filter-list", OptionList).focus()
        else:
            self.query_one("#dialog-filter", Input).focus()

    def _apply_theme(self) -> None:
        """Inject the palette: scrim + panel wash + amber accent bar.

        The selection overlay is "the thing you're operating on right now", so
        its accent bar rides the same amber as the live input frame — they read
        as the same family of surface."""
        t = self._theme
        self.styles.background = Color.parse(t.steps[0]).with_alpha(0.55)
        box = self.query_one(Vertical)
        box.styles.background = t.role("bg_panel")
        box.styles.border_left = ("heavy", t.role("accent"))
        self.query_one("#dialog-title", Static).styles.color = t.role("accent")
        # Filter box echoes the main CommandInput: the element wash, no box.
        self.query_one("#dialog-filter", Input).styles.background = t.role("bg_element")
        ol = self.query_one("#dialog-filter-list", OptionList)
        ol.styles.background = t.role("bg_panel")
        self.query_one("#dialog-hint", Static).styles.color = t.role("text_dim")

    # ── filtering / rendering ────────────────────────────────────────────────
    def _rebuild(self, query: str) -> None:
        ol = self.query_one("#dialog-filter-list", OptionList)
        ol.clear_options()
        self._by_id.clear()
        self._idx_by_oid.clear()

        matched = fuzzy_filter(query, self._options, key=lambda o: o.key())
        first_selectable: str | None = None

        if self._show_categories and not query.strip():
            # Grouped view (stable category order = first appearance).
            order: list[str] = []
            groups: dict[str, list[SelectOption[T]]] = {}
            for opt in matched:
                cat = opt.category or ""
                if cat not in groups:
                    groups[cat] = []
                    order.append(cat)
                groups[cat].append(opt)
            idx = 0
            for cat in order:
                if cat:
                    ol.add_option(
                        Option(self._header_text(cat), id=None, disabled=True)
                    )
                for opt in groups[cat]:
                    oid = f"opt-{idx}"
                    idx += 1
                    self._by_id[oid] = opt
                    self._idx_by_oid[oid] = self._stable_idx.get(id(opt), -1)
                    ol.add_option(self._option(opt, oid))
                    if first_selectable is None and not opt.disabled:
                        first_selectable = oid
        else:
            for idx, opt in enumerate(matched):
                oid = f"opt-{idx}"
                self._by_id[oid] = opt
                self._idx_by_oid[oid] = self._stable_idx.get(id(opt), -1)
                ol.add_option(self._option(opt, oid))
                if first_selectable is None and not opt.disabled:
                    first_selectable = oid

        if first_selectable is not None:
            ol.highlighted = ol.get_option_index(first_selectable)

    def _header_text(self, category: str) -> Text:
        return Text(category.upper(), style=self._theme.role("text_dim"))

    def _option(self, opt: SelectOption[T], oid: str) -> Option:
        checked = self._idx_by_oid.get(oid, -1) in self._checked
        return Option(self._row_text(opt, checked), id=oid, disabled=opt.disabled)

    def _row_text(self, opt: SelectOption[T], checked: bool = False) -> Text:
        t = self._theme
        accent = t.role("accent")
        text = t.role("text")
        muted = t.role("text_muted")
        dim = t.role("text_dim")

        row = Text()
        if self._multi:
            box = "[x] " if checked else "[ ] "
            row.append(box, style=accent if checked else dim)
        marker = "● " if opt.current else "  "
        row.append(marker, style=accent if opt.current else dim)
        row.append(opt.title, style=(accent if opt.current else text))
        if opt.description:
            row.append("  ", style="")
            row.append(opt.description, style=muted)
        if opt.footer:
            row.append("  ", style="")
            row.append(opt.footer, style=dim)
        return row

    # ── events ────────────────────────────────────────────────────────────────
    @on(Input.Changed, "#dialog-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._rebuild(event.value)

    @on(Input.Submitted, "#dialog-filter")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        # Enter in the filter box: confirm the checked set (multi) or select the
        # highlighted option (single).
        event.stop()
        if self._multi:
            self._confirm_multi()
            return
        ol = self.query_one("#dialog-filter-list", OptionList)
        if ol.highlighted is None:
            return
        option = ol.get_option_at_index(ol.highlighted)
        self._select_by_id(option.id)

    @on(OptionList.OptionSelected, "#dialog-filter-list")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        # In multi mode an OptionList "select" (Enter on a row) toggles it
        # rather than dismissing; Enter from the filter box confirms the set.
        if self._multi:
            self._toggle_by_id(event.option.id)
            return
        self._select_by_id(event.option.id)

    def _select_by_id(self, oid: str | None) -> None:
        if oid is None:
            return
        opt = self._by_id.get(oid)
        if opt is None or opt.disabled:
            return
        self.dismiss(opt.value)

    def _toggle_by_id(self, oid: str | None) -> None:
        if oid is None:
            return
        opt = self._by_id.get(oid)
        if opt is None or opt.disabled:
            return
        sidx = self._idx_by_oid.get(oid, -1)
        if sidx < 0:
            return
        if sidx in self._checked:
            self._checked.remove(sidx)
        else:
            self._checked.append(sidx)
        # Re-render in place, preserving the highlighted row.
        ol = self.query_one("#dialog-filter-list", OptionList)
        keep = ol.highlighted
        self._rebuild(self.query_one("#dialog-filter", Input).value)
        if keep is not None:
            ol.highlighted = min(keep, ol.option_count - 1) if ol.option_count else None

    def _confirm_multi(self) -> None:
        by_index = {self._stable_idx[id(o)]: o for o in self._options}
        values = [by_index[i].value for i in self._checked if i in by_index]
        self.dismiss(values)

    # ── keyboard ────────────────────────────────────────────────────────────────
    def action_cursor_up(self) -> None:
        self.query_one("#dialog-filter-list", OptionList).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#dialog-filter-list", OptionList).action_cursor_down()

    def action_toggle_checked(self) -> None:
        if not self._multi:
            return
        ol = self.query_one("#dialog-filter-list", OptionList)
        if ol.highlighted is None:
            return
        option = ol.get_option_at_index(ol.highlighted)
        self._toggle_by_id(option.id)

    def action_confirm_multi(self) -> None:
        if self._multi:
            self._confirm_multi()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


__all__ = ["SelectScreen", "SelectOption"]
