"""Modal screens for the AutoVerse TUI.

:class:`PermissionScreen` is a Textual ``ModalScreen[Decision]`` that renders an
approval card (tool name + affected target + reversibility hint) and resolves to
a :class:`~autoverse.types.tools.Decision` via ``y`` (allow) / ``n`` (deny) /
``a`` (always allow). It is pushed with ``await app.push_screen_wait(...)`` so the
``PermissionPort`` adapter can ``await`` the user's decision; Textual's Screen
lifecycle owns keyboard focus, so the modal cannot leak keys to the main screen
(replacing the old manual ``mode_stack.push(PERMISSION)`` bookkeeping).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from autoverse.types.tools import Decision, ToolCall


class PermissionScreen(ModalScreen[Decision]):
    """Confirm/deny a tool call. Resolves to a :class:`Decision`."""

    BINDINGS = [
        ("y", "allow", "Allow"),
        ("a", "always", "Always allow"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    CSS = """
    PermissionScreen {
        align: center middle;
    }
    PermissionScreen > Vertical {
        width: 70%;
        max-width: 90;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, call: ToolCall) -> None:
        super().__init__()
        self._call = call

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b $warning]Permission required[/b $warning]")
            yield Static(f"Tool: [b]{self._call.name}[/b]")
            target = self._summarize(self._call)
            if target:
                yield Static(f"Target: {target}")
            yield Static("")
            yield Static("[dim]y = allow once · a = always allow · n / Esc = deny[/dim]")

    def action_allow(self) -> None:
        self.dismiss(Decision.allow(rule="user-approved"))

    def action_always(self) -> None:
        self.dismiss(Decision.allow(rule="user-always"))

    def action_deny(self) -> None:
        self.dismiss(Decision.deny(reason="denied by user"))

    @staticmethod
    def _summarize(call: ToolCall) -> str:
        if "file_path" in call.input:
            return str(call.input["file_path"])
        if "command" in call.input:
            return str(call.input["command"])
        return ""


__all__ = ["PermissionScreen"]
