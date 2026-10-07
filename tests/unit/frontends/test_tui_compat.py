"""Unit: terminal capability probing & the four-tier fallback ladder.

Covers docs/guides/using-cli-and-tui.md §12 falsifiable boundaries:
* B1 — ``recommend_tier`` is a pure, table-driven decision.
* B2 — ``probe`` never raises (non-TTY → conservative defaults).
* B3 — ``auto`` never selects INLINE (alt-screen unprobeable, N2).
* B4 — the runtime net descends on a *startup* failure, but re-raises a
       *session* crash (mounted flag set).
* B5 — explicit ``--ui tui`` is strict (errors, no fallback); ``--ui inline``
       descends to line on startup failure.
* B6 — ``presentation="inline"`` force-hides the sidebar.
* B8 — ``textual`` is importable (it is now a core dependency).
"""

from __future__ import annotations

import pytest

from autoverse.frontends.cli import capabilities as caps_mod
from autoverse.frontends.cli.capabilities import (
    UITier,
    describe,
    descend_from,
    probe,
    recommend_tier,
)


class _FakeTTY:
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


def _caps(**over: object) -> caps_mod.TerminalCapabilities:
    """A 'good' capabilities snapshot with selective overrides for B1/B3."""
    base = dict(
        stdin_tty=True,
        stdout_tty=True,
        term="xterm-256color",
        is_dumb=False,
        color="truecolor",
        columns=120,
        lines=40,
        encoding="utf-8",
        is_utf8=True,
        in_multiplexer=False,
        in_ssh=False,
        in_ci=False,
        is_windows=False,
    )
    base.update(over)
    return caps_mod.TerminalCapabilities(**base)  # type: ignore[arg-type]


# ── B1: recommend_tier is a pure table ────────────────────────────────────────
@pytest.mark.parametrize(
    "over, expected",
    [
        ({}, UITier.FULLSCREEN),                              # good terminal
        ({"is_dumb": True, "color": "none"}, UITier.LINE),    # dumb
        ({"color": "none"}, UITier.LINE),                     # no colour
        ({"lines": 8}, UITier.LINE),                          # too short
        ({"columns": 50}, UITier.LINE),                       # too narrow
        ({"stdin_tty": False}, UITier.LINE),                  # not a tty (defensive)
        ({"in_multiplexer": True}, UITier.FULLSCREEN),        # tmux does NOT downgrade
        ({"in_ssh": True}, UITier.FULLSCREEN),                # ssh does NOT downgrade
    ],
)
def test_recommend_tier_table(over: dict, expected: UITier) -> None:
    assert recommend_tier(_caps(**over)) is expected


# ── B3: auto never selects INLINE ─────────────────────────────────────────────
def test_auto_never_picks_inline() -> None:
    for over in ({}, {"columns": 50}, {"lines": 8}, {"color": "256"}, {"in_ssh": True}):
        assert recommend_tier(_caps(**over)) in (UITier.FULLSCREEN, UITier.LINE)


# ── B2: probe is defensive (non-TTY → conservative defaults, never raises) ────
def test_probe_non_tty_defaults() -> None:
    c = probe(environ={}, stdin=None, stdout=None)
    assert c.stdin_tty is False and c.stdout_tty is False
    assert c.columns >= 1 and c.lines >= 1     # fallback size, not a crash
    assert c.color in ("none", "16", "256", "truecolor")


def test_probe_reads_env_and_tty() -> None:
    c = probe(
        environ={"TERM": "xterm-256color", "COLORTERM": "truecolor", "TMUX": "/tmp/x"},
        stdin=_FakeTTY(),
        stdout=_FakeTTY(),
    )
    assert c.stdin_tty and c.stdout_tty
    assert c.color == "truecolor"
    assert c.in_multiplexer is True
    assert c.is_dumb is False


def test_probe_dumb_and_no_color() -> None:
    assert probe(environ={"TERM": "dumb"}, stdin=_FakeTTY(), stdout=_FakeTTY()).is_dumb
    assert (
        probe(environ={"TERM": "xterm", "NO_COLOR": "1"}, stdin=_FakeTTY(), stdout=_FakeTTY()).color
        == "none"
    )


def test_descend_from_ladder() -> None:
    assert descend_from(UITier.FULLSCREEN) == [UITier.FULLSCREEN, UITier.INLINE, UITier.LINE]
    assert descend_from(UITier.INLINE) == [UITier.INLINE, UITier.LINE]
    assert descend_from(UITier.LINE) == [UITier.LINE]


def test_describe_is_one_line() -> None:
    s = describe(_caps(), UITier.FULLSCREEN)
    assert "\n" not in s and "→ tui" in s


# ── B4: runtime fallback net (startup-failure descend vs session-crash raise) ─
async def test_fallback_descends_on_startup_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import autoverse.frontends.cli.main as cli

    attempts: list[str] = []

    async def fake_tui(args, theme, *, presentation, mounted):
        attempts.append(presentation)
        raise RuntimeError(f"{presentation} cannot start")  # never sets mounted

    async def fake_line(args, theme):
        attempts.append("line")
        return 0

    monkeypatch.setattr(cli, "_run_tui", fake_tui)
    monkeypatch.setattr(cli, "_run_line_repl", fake_line)

    rc = await cli._run_interactive_with_fallback(
        cli.build_parser().parse_args([]), cli.Theme(), UITier.FULLSCREEN
    )
    assert rc == 0
    assert attempts == ["fullscreen", "inline", "line"]   # descended the whole ladder
    err = capsys.readouterr().err
    assert "tui 启动失败" in err and "inline 启动失败" in err   # tier tokens


async def test_fallback_reraises_session_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoverse.frontends.cli.main as cli

    async def fake_tui(args, theme, *, presentation, mounted):
        mounted.set()                       # mounted → this is a *session* crash
        raise RuntimeError("mid-session boom")

    monkeypatch.setattr(cli, "_run_tui", fake_tui)
    with pytest.raises(RuntimeError, match="mid-session boom"):
        await cli._run_interactive_with_fallback(
            cli.build_parser().parse_args([]), cli.Theme(), UITier.FULLSCREEN
        )


async def test_fallback_inline_start_descends_to_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autoverse.frontends.cli.main as cli

    attempts: list[str] = []

    async def fake_tui(args, theme, *, presentation, mounted):
        attempts.append(presentation)
        raise RuntimeError("no inline driver")

    async def fake_line(args, theme):
        attempts.append("line")
        return 0

    monkeypatch.setattr(cli, "_run_tui", fake_tui)
    monkeypatch.setattr(cli, "_run_line_repl", fake_line)
    rc = await cli._run_interactive_with_fallback(
        cli.build_parser().parse_args(["--ui", "inline"]), cli.Theme(), UITier.INLINE
    )
    assert rc == 0
    assert attempts == ["inline", "line"]   # inline start never tries fullscreen


# ── B5: explicit --ui tui is strict (no fallback net) ─────────────────────────
async def test_dispatch_ui_tui_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoverse.frontends.cli.main as cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "_supports_tui", lambda args: False)
    rc = await cli._dispatch(cli.build_parser().parse_args(["--ui", "tui"]))
    assert rc == 2   # errors, does not fall back


async def test_dispatch_auto_starts_fullscreen(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoverse.frontends.cli.main as cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.capabilities, "recommend_tier", lambda caps: UITier.FULLSCREEN)

    start_seen: list[UITier] = []

    async def fake_net(args, theme, start):
        start_seen.append(start)
        return 0

    monkeypatch.setattr(cli, "_run_interactive_with_fallback", fake_net)
    rc = await cli._dispatch(cli.build_parser().parse_args([]))
    assert rc == 0 and start_seen == [UITier.FULLSCREEN]


# ── B6: inline presentation force-hides the sidebar ───────────────────────────
def test_inline_presentation_hides_sidebar() -> None:
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry

    async def _factory(sink):  # pragma: no cover - never invoked (no run)
        raise AssertionError

    inline = AutoVerseApp(
        _factory, app_ctx=None, registry=build_default_registry(),
        presentation="inline",
    )
    assert inline._sidebar_mode == "hidden"
    assert inline._sidebar_visible() is False

    full = AutoVerseApp(
        _factory, app_ctx=None, registry=build_default_registry(),
        presentation="fullscreen",
    )
    assert full._sidebar_mode == "auto"


# ── B8: textual is a core dependency now ──────────────────────────────────────
def test_textual_is_importable() -> None:
    import importlib

    assert importlib.import_module("textual") is not None
