"""Unit: command registry single-source (help/completion/handler/keybind)."""

from __future__ import annotations

from typing import Any

import pytest

from autoverse.frontends.repl.commands import (
    CommandRegistry,
    CommandResult,
    CommandSpec,
    command,
    dispatch,
)


async def _h(ctx: Any, args: str) -> CommandResult:
    return CommandResult(display="ok")


def test_help_completion_handler_single_source() -> None:
    reg = CommandRegistry()
    reg.register(CommandSpec(name="alpha", summary="first", handler=_h, keybind="ctrl+a"))
    reg.register(CommandSpec(name="beta", summary="second", handler=_h, group="view"))

    help_text = "\n".join(reg.help_lines())
    assert "alpha" in help_text and "first" in help_text
    assert "beta" in help_text and "second" in help_text

    assert reg.completion_names() == ["/alpha", "/beta"]

    assert reg.get("alpha").handler is _h
    assert reg.by_keybind("Ctrl+A").name == "alpha"


def test_aliases_resolve() -> None:
    reg = CommandRegistry()
    reg.register(CommandSpec(name="quit", summary="leave", handler=_h, aliases=("exit",)))
    assert reg.get("exit").name == "quit"


def test_duplicate_name_raises() -> None:
    reg = CommandRegistry()
    reg.register(CommandSpec(name="x", summary="s", handler=_h))
    with pytest.raises(ValueError):
        reg.register(CommandSpec(name="x", summary="s2", handler=_h))


def test_duplicate_keybind_raises() -> None:
    reg = CommandRegistry()
    reg.register(CommandSpec(name="a", summary="s", handler=_h, keybind="ctrl+z"))
    with pytest.raises(ValueError):
        reg.register(CommandSpec(name="b", summary="s", handler=_h, keybind="Ctrl+Z"))


def test_hidden_excluded_from_help_and_completion() -> None:
    reg = CommandRegistry()
    reg.register(CommandSpec(name="secret", summary="s", handler=_h, hidden=True))
    assert "/secret" not in reg.completion_names()
    assert "secret" not in "\n".join(reg.help_lines())
    # Still dispatchable.
    assert reg.get("secret") is not None


async def test_command_decorator_registers() -> None:
    reg = CommandRegistry()

    @command("deco", "decorated", registry=reg)
    async def _d(ctx: Any, args: str) -> CommandResult:
        return CommandResult(display="d")

    assert reg.get("deco").handler is _d
    res = await dispatch("/deco", object(), reg)
    assert res.display == "d"


async def test_unknown_command_is_error_not_skill_fallback() -> None:
    reg = CommandRegistry()
    res = await dispatch("/ghost arg", object(), reg)
    assert res.is_error
