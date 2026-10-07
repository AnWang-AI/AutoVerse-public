"""Interactive modal dialogs for the AutoVerse TUI (docs/reference/commands.md §8).

This package is the *interaction/display layer* that consumes the command
registry's already-rich data model (``palette_entries`` / ``completion_items`` /
``suggested`` / ``keybind`` / ``group``) and renders it as keyboard-navigable
overlays — the layer MiMo-Code has and AutoVerse previously lacked.

* :class:`SelectScreen` — generic searchable, grouped, keyboard-navigable
  selection overlay (L1 foundation; MiMo ``ui/dialog-select.tsx``).
* :class:`ConfirmScreen` — yes/no confirmation overlay (MiMo
  ``ui/dialog-confirm.tsx``).
* :class:`Toast` — transient corner notification (MiMo ``ui/toast.tsx``).
* :func:`fuzzy_score` / :func:`fuzzy_filter` — dependency-free fuzzy matcher.

All overlays are Textual ``ModalScreen`` pushed with ``push_screen_wait`` — the
same pattern as the existing ``PermissionScreen`` (screens.py).
"""

from __future__ import annotations

from autoverse.frontends.repl.dialogs.completion_menu import CompletionMenu
from autoverse.frontends.repl.dialogs.confirm_screen import ConfirmScreen
from autoverse.frontends.repl.dialogs.fuzzy import fuzzy_filter, fuzzy_score
from autoverse.frontends.repl.dialogs.host import DialogHost, get_dialog_host
from autoverse.frontends.repl.dialogs.inline_choice import ChoiceItem, InlineChoiceMenu
from autoverse.frontends.repl.dialogs.prompt_screen import PromptScreen
from autoverse.frontends.repl.dialogs.select_screen import SelectOption, SelectScreen
from autoverse.frontends.repl.dialogs.text_screen import TextScreen
from autoverse.frontends.repl.dialogs.toast import Toast

__all__ = [
    "SelectScreen",
    "SelectOption",
    "ConfirmScreen",
    "InlineChoiceMenu",
    "ChoiceItem",
    "PromptScreen",
    "TextScreen",
    "Toast",
    "CompletionMenu",
    "DialogHost",
    "get_dialog_host",
    "fuzzy_score",
    "fuzzy_filter",
]
