"""DialogHost protocol (docs/reference/commands.md §8).

Command handlers live in ``builtin_cmds.py`` and must stay frontend-agnostic —
they cannot import Textual or call ``push_screen_wait`` directly. Instead the
app exposes a tiny async facade through ``CommandContext.extra["ui"]`` that
handlers use to request overlays. The Textual app implements this protocol; a
headless/CLI frontend can implement a no-op or auto-pick version.

This keeps the command data layer (``commands.py``) free of any UI dependency
(P3 dependency inversion) while still letting ``/model`` etc. open a real popup.
"""

from __future__ import annotations

from typing import Protocol, Sequence, TypeVar, cast, runtime_checkable

from autoverse.frontends.repl.dialogs.select_screen import SelectOption

T = TypeVar("T")


@runtime_checkable
class DialogHost(Protocol):
    """Async overlay facade a command handler can use (when present)."""

    async def select(
        self,
        options: Sequence[SelectOption[T]],
        *,
        title: str = "Select",
        placeholder: str = "type to filter…",
        multi: bool = False,
    ) -> T | list[T] | None:
        """Open a searchable selection overlay.

        Single mode returns the chosen value (or None if cancelled). Multi mode
        (``multi=True``) returns a list of checked values (empty if none) or
        None if dismissed with Esc.
        """
        ...

    async def confirm(
        self,
        message: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
    ) -> bool:
        """Open a yes/no overlay; return True if confirmed."""
        ...

    async def prompt(
        self,
        title: str,
        *,
        placeholder: str = "",
        initial: str = "",
        password: bool = False,
    ) -> str | None:
        """Open a one-line input overlay; return None when cancelled."""
        ...

    def toast(self, message: str, *, variant: str = "info") -> None:
        """Show a transient corner notification (fire-and-forget)."""
        ...

    async def show_text(self, body: str, *, title: str = "") -> None:
        """Open a scrollable read-only viewer for a long report (§8 I9)."""
        ...


# The overlay surface command handlers route through. ``prompt`` is *not* in
# this set: no ``get_dialog_host`` consumer calls it (the app invokes its own
# ``prompt`` directly for rename / secret entry), so requiring it would wrongly
# reject an otherwise-capable host. We probe these by name + callability instead
# of ``isinstance(ui, DialogHost)`` — the protocol is ``@runtime_checkable``, so
# ``isinstance`` is all-or-nothing over *every* declared method: the moment the
# protocol grows a method (as ``prompt`` was added), every host missing just
# that one silently fails the check and commands fall back to flooding the
# scroll region. Capability probing degrades per-method instead of per-host.
_REQUIRED_HOST_METHODS = ("select", "confirm", "toast", "show_text")


def get_dialog_host(ctx: object) -> DialogHost | None:
    """Return the DialogHost from ``CommandContext.extra['ui']`` if available.

    A host qualifies when it exposes the core overlay methods
    (:data:`_REQUIRED_HOST_METHODS`) as callables — not when it satisfies the
    *entire* :class:`DialogHost` protocol. This keeps newer, optional protocol
    additions (e.g. ``prompt``) from silently disabling overlay routing for
    hosts that have not yet implemented them.
    """
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, dict):
        return None
    ui = extra.get("ui")
    if ui is None:
        return None
    if all(callable(getattr(ui, name, None)) for name in _REQUIRED_HOST_METHODS):
        return cast(DialogHost, ui)
    return None


__all__ = ["DialogHost", "get_dialog_host"]
