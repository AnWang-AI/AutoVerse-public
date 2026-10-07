"""Unit: line-based REPL fallback (docs/guides/using-cli-and-tui.md §8 falsifiable boundaries).

Covers the append-only line REPL that reuses the TUI render pipeline
(``TuiEventSink`` + ``render_event``) without importing ``textual``: the degrade
chain (B5), an end-to-end echo turn + EOF exit (B6/B2), and the row-based
permission adapter's y/a/N semantics (B3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest
from rich.console import Console

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.frontends.cli.main import _dispatch, _resolve_ui_mode, build_parser
from autoverse.frontends.repl.builtin_cmds import build_default_registry
from autoverse.frontends.repl.line_repl import LinePermissionAdapter, LineRepl
from autoverse.frontends.repl.theme import Theme
from autoverse.types.tools import CapabilityTier, ToolCall


def _scripted_reader(lines: Iterable[str | None]):
    """An async ``read_line`` that yields *lines*; ``None`` raises EOFError."""
    it = iter(lines)

    async def read_line(_prompt: str) -> str:
        try:
            value = next(it)
        except StopIteration:
            raise EOFError
        if value is None:
            raise EOFError
        return value

    return read_line


async def _echo_factory(sink, permissions):
    opts = EngineOptions(
        model="echo",
        cwd=Path.cwd(),
        session_mode="interactive",
        event_sink=sink,
        permission_engine=permissions,
    )
    return await EngineBuilder(opts).build()


# ── B1: the line path must not import textual ─────────────────────────────────
def test_line_repl_does_not_import_textual() -> None:
    import os
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import autoverse.frontends.repl.line_repl  # noqa: F401\n"
        "assert 'textual' not in sys.modules, 'line REPL must not import textual'\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# ── B5: degrade-chain resolution (now five-valued, docs/guides/using-cli-and-tui.md §10.1) ───────
def test_resolve_ui_mode() -> None:
    p = build_parser()
    assert _resolve_ui_mode(p.parse_args([])) == "auto"            # default → auto (probe decides)
    assert _resolve_ui_mode(p.parse_args(["--no-tui"])) == "line"  # legacy alias
    assert _resolve_ui_mode(p.parse_args(["--ui", "line"])) == "line"
    assert _resolve_ui_mode(p.parse_args(["--ui", "tui"])) == "tui"
    assert _resolve_ui_mode(p.parse_args(["--ui", "inline"])) == "inline"
    assert _resolve_ui_mode(p.parse_args(["--ui", "plain"])) == "plain"
    # explicit --ui wins over --no-tui
    assert _resolve_ui_mode(p.parse_args(["--ui", "tui", "--no-tui"])) == "tui"


async def test_dispatch_auto_lands_on_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto + a probe that recommends LINE runs the line REPL (docs/guides/using-cli-and-tui.md §10.2)."""
    import autoverse.frontends.cli.main as cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        cli.capabilities, "recommend_tier", lambda caps: cli.capabilities.UITier.LINE
    )

    called = {"line": False}

    async def fake_line(args, theme):
        called["line"] = True
        return 0

    monkeypatch.setattr(cli, "_run_line_repl", fake_line)
    rc = await _dispatch(build_parser().parse_args([]))
    assert rc == 0 and called["line"] is True


async def test_dispatch_ui_tui_errors_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autoverse.frontends.cli.main as cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "_supports_tui", lambda args: False)
    # Explicit --ui tui must NOT silently fall back to line.
    rc = await _dispatch(build_parser().parse_args(["--ui", "tui"]))
    assert rc == 2


# ── B6/B2: end-to-end echo turn + EOF, rendering via the reused sink ───────────
async def test_line_repl_echo_turn_then_eof() -> None:
    console = Console(force_terminal=True, width=80, record=True)
    repl = LineRepl(
        _echo_factory,
        build_default_registry(),
        console=console,
        theme=Theme(),
        model="echo",
        read_line=_scripted_reader(["hello there", None]),
    )
    rc = await repl.run()
    out = console.export_text()
    assert rc == 0
    assert "AutoVerse" in out          # welcome banner
    assert "hello there" in out        # user echo
    assert "echo" in out               # echo model reply (reused renderer)
    assert "▪" in out                  # per-turn footer marker


async def test_line_repl_slash_help_degrades_to_text() -> None:
    console = Console(force_terminal=True, width=80, record=True)
    repl = LineRepl(
        _echo_factory,
        build_default_registry(),
        console=console,
        theme=Theme(),
        model="echo",
        read_line=_scripted_reader(["/help", None]),
    )
    rc = await repl.run()
    out = console.export_text()
    assert rc == 0
    assert "/model" in out             # help list printed inline (no DialogHost)


# ── B3: row-based permission adapter y/a/N semantics ───────────────────────────
def _call() -> ToolCall:
    return ToolCall(id="c1", name="bash", input={"command": "ls"}, tier=CapabilityTier.READ_ONLY)


async def test_permission_allow_once() -> None:
    adapter = LinePermissionAdapter(_scripted_reader(["y"]), Console())
    decision = await adapter.request_approval(_call())
    assert decision.allowed is True
    # "allow once" must NOT persist an explicit rule (no scoped allow added).
    action, _rule = adapter._matcher.match_explained(_call())
    assert action is None


async def test_permission_wait_callbacks_wrap_user_approval() -> None:
    calls: list[str] = []
    adapter = LinePermissionAdapter(
        _scripted_reader(["y"]),
        Console(),
        on_wait_start=lambda: calls.append("start"),
        on_wait_end=lambda: calls.append("end"),
    )

    decision = await adapter.request_approval(_call())

    assert decision.allowed is True
    assert calls == ["start", "end"]


async def test_permission_always_persists_rule() -> None:
    adapter = LinePermissionAdapter(_scripted_reader(["a"]), Console())
    decision = await adapter.request_approval(_call())
    assert decision.allowed is True
    # "always" wrote a scoped allow rule → it now matches explicitly.
    action, _rule = adapter._matcher.match_explained(_call())
    assert action == "allow"


async def test_permission_deny() -> None:
    adapter = LinePermissionAdapter(_scripted_reader(["n"]), Console())
    decision = await adapter.request_approval(_call())
    assert decision.allowed is False


async def test_permission_cancel_all_denies() -> None:
    adapter = LinePermissionAdapter(_scripted_reader(["y"]), Console())
    adapter.cancel_all()
    decision = await adapter.request_approval(_call())
    assert decision.allowed is False


async def test_line_plan_approval_runs_execution_prompt() -> None:
    from types import SimpleNamespace

    repl = LineRepl(
        _echo_factory,
        build_default_registry(),
        console=Console(force_terminal=True, width=80, record=True),
        read_line=_scripted_reader(["y"]),
    )
    repl._app_ctx = SimpleNamespace(
        extra={
            "plan_mode": {
                "active": False,
                "pending_approval": True,
                "plan": "1. edit\n2. test",
            }
        }
    )
    prompts: list[str] = []

    async def _capture(prompt: str) -> None:
        prompts.append(prompt)

    repl._run_turn = _capture  # type: ignore[method-assign]

    await repl._handle_plan_proposed()

    assert repl._app_ctx.extra["plan_mode"]["approved"] is True
    assert prompts
    assert "Approved plan:" in prompts[0]
    assert "1. edit" in prompts[0]


# ── LineDialogHost (docs/guides/using-cli-and-tui.md §4.5): inline overlay facade ────────────────


def _host(reader):  # type: ignore[no-untyped-def]
    """Build a LineDialogHost over a recording console + scripted reader."""
    from autoverse.frontends.repl.line_repl import LineDialogHost

    console = Console(force_terminal=True, width=80, record=True)
    host = LineDialogHost(reader, console, Theme.load(None))
    return host, console


async def test_line_dialog_host_confirm_yes_and_no() -> None:
    host, _ = _host(_scripted_reader(["y"]))
    assert await host.confirm("Delete?", title="Confirm") is True
    host, _ = _host(_scripted_reader(["n"]))
    assert await host.confirm("Delete?", title="Confirm") is False
    # Empty / EOF defaults to False (safe: never run a destructive op by default).
    host, _ = _host(_scripted_reader([None]))
    assert await host.confirm("Delete?", title="Confirm") is False


async def test_line_dialog_host_select_by_index() -> None:
    from autoverse.frontends.repl.dialogs.select_screen import SelectOption

    opts = [
        SelectOption(title="alpha", value="A"),
        SelectOption(title="beta", value="B"),
        SelectOption(title="gamma", value="C"),
    ]
    host, console = _host(_scripted_reader(["2"]))
    assert await host.select(opts, title="Pick") == "B"
    # The numbered list is shown.
    out = console.export_text()
    assert "alpha" in out and "2" in out
    # Empty input cancels (None).
    host, _ = _host(_scripted_reader([""]))
    assert await host.select(opts, title="Pick") is None
    # Out-of-range index cancels (None), never an IndexError.
    host, _ = _host(_scripted_reader(["99"]))
    assert await host.select(opts, title="Pick") is None


async def test_line_dialog_host_select_multi() -> None:
    from autoverse.frontends.repl.dialogs.select_screen import SelectOption

    opts = [SelectOption(title=str(i), value=i) for i in range(1, 4)]
    host, _ = _host(_scripted_reader(["1,3"]))
    assert await host.select(opts, title="Pick", multi=True) == [1, 3]
    # Multi with no valid picks → empty list (not None).
    host, _ = _host(_scripted_reader([""]))
    assert await host.select(opts, title="Pick", multi=True) == []


async def test_line_dialog_host_prompt_and_toast() -> None:
    host, _ = _host(_scripted_reader(["new title"]))
    assert await host.prompt("Rename", placeholder="title…") == "new title"
    # Empty → None (cancel).
    host, _ = _host(_scripted_reader([""]))
    assert await host.prompt("Rename") is None
    # toast prints one colored line (fire-and-forget, no return).
    host, console = _host(_scripted_reader([]))
    host.toast("done", variant="success")
    assert "done" in console.export_text()


async def test_line_dialog_host_show_text_frames_body() -> None:
    host, console = _host(_scripted_reader([]))
    await host.show_text("a\nb\nc", title="Report")
    out = console.export_text()
    assert "Report" in out and "a" in out and "─" in out  # framed, not bare


async def test_line_repl_wires_dialog_host_into_command_ctx() -> None:
    """The REPL must expose its LineDialogHost as ``extra['ui']`` so commands
    route through overlays (confirm/select/report) instead of flooding."""
    from autoverse.frontends.repl.dialogs.host import get_dialog_host
    from autoverse.frontends.repl.line_repl import LineDialogHost

    repl = LineRepl(
        _echo_factory,
        build_default_registry(),
        console=Console(force_terminal=True, width=80, record=True),
        read_line=_scripted_reader([]),
    )
    ctx = repl._command_ctx()
    host = get_dialog_host(ctx)
    assert isinstance(host, LineDialogHost)
