"""Unit: interactive command dialogs (docs/reference/commands.md §8 implementation).

Covers the L1 foundation (fuzzy matcher, SelectScreen, ConfirmScreen,
CompletionMenu) and the L2 wiring (`/model` opens a select overlay via the
DialogHost facade; the command palette feeds palette_entries into a SelectScreen).
Textual-dependent tests are skipped when textual is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from autoverse.frontends.repl.dialogs.fuzzy import fuzzy_filter, fuzzy_score


# ── fuzzy matcher (pure) ───────────────────────────────────────────────────────

def test_fuzzy_subsequence_match_and_miss() -> None:
    assert fuzzy_score("mod", "model") is not None
    assert fuzzy_score("ml", "model") is not None  # subsequence m..l
    assert fuzzy_score("xyz", "model") is None


def test_fuzzy_contiguous_beats_scattered() -> None:
    contiguous = fuzzy_score("mod", "model")
    scattered = fuzzy_score("mod", "my-old-door")
    assert contiguous is not None and scattered is not None
    assert contiguous > scattered


def test_fuzzy_boundary_bonus() -> None:
    # "sl" matching at a word boundary ("session-list") should beat mid-word.
    boundary = fuzzy_score("sl", "session-list")
    midword = fuzzy_score("sl", "newsletter")  # 'sl' inside 'newsletter'? n-e-w-s-l
    assert boundary is not None
    # both match; boundary should not be lower
    if midword is not None:
        assert boundary >= midword


def test_fuzzy_filter_orders_and_keeps_all_on_empty() -> None:
    items = ["model", "memory", "mcp", "help"]
    assert fuzzy_filter("", items, key=str) == items  # empty → original order
    out = fuzzy_filter("m", items, key=str)
    assert set(out) == {"model", "memory", "mcp"}
    assert "help" not in out


# ── SelectScreen (Textual pilot) ───────────────────────────────────────────────

async def test_select_screen_returns_value_on_enter() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import SelectOption, SelectScreen

    result: list[object] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            options = [
                SelectOption("Model A", "a", description="first"),
                SelectOption("Model B", "b", description="second"),
            ]
            value = await self.push_screen_wait(SelectScreen(options, title="Pick"))
            result.append(value)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # select highlighted (first)
        await pilot.pause()
    assert result == ["a"]


async def test_select_screen_filter_then_enter() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import SelectOption, SelectScreen

    result: list[object] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            options = [
                SelectOption("alpha", "a"),
                SelectOption("beta", "b"),
                SelectOption("gamma", "g"),
            ]
            value = await self.push_screen_wait(
                SelectScreen(options, title="Pick", show_categories=False)
            )
            result.append(value)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "bet":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert result == ["b"]


async def test_select_screen_escape_returns_none() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import SelectOption, SelectScreen

    result: list[object] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            value = await self.push_screen_wait(
                SelectScreen([SelectOption("x", "x")], title="Pick")
            )
            result.append(value)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert result == [None]


async def test_select_screen_multi_toggle_then_confirm() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import SelectOption, SelectScreen

    result: list[object] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            options = [
                SelectOption("alpha", "a"),
                SelectOption("beta", "b"),
                SelectOption("gamma", "g"),
            ]
            value = await self.push_screen_wait(
                SelectScreen(options, title="Pick many", multi=True,
                             show_categories=False)
            )
            result.append(value)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")        # check 'a' (highlighted first)
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("space")        # check 'g'
        await pilot.press("ctrl+s")       # confirm
        await pilot.pause()
    assert result == [["a", "g"]]


async def test_select_screen_multi_escape_returns_none() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import SelectOption, SelectScreen

    result: list[object] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            value = await self.push_screen_wait(
                SelectScreen([SelectOption("x", "x"), SelectOption("y", "y")],
                             title="Pick", multi=True, show_categories=False)
            )
            result.append(value)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")        # check something
        await pilot.press("escape")       # but cancel
        await pilot.pause()
    assert result == [None]


async def test_select_screen_multi_confirm_none_checked_is_empty_list() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import SelectOption, SelectScreen

    result: list[object] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            value = await self.push_screen_wait(
                SelectScreen([SelectOption("x", "x")], title="Pick",
                             multi=True, show_categories=False)
            )
            result.append(value)

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")       # confirm with nothing checked
        await pilot.pause()
    assert result == [[]]


# ── ConfirmScreen (Textual pilot) ──────────────────────────────────────────────

async def test_confirm_screen_y_confirms() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import ConfirmScreen

    result: list[bool] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            result.append(await self.push_screen_wait(ConfirmScreen("Sure?")))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert result == [True]


async def test_confirm_screen_escape_cancels() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import ConfirmScreen

    result: list[bool] = []

    from textual import work

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            result.append(await self.push_screen_wait(ConfirmScreen("Sure?")))

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert result == [False]


# ── CompletionMenu (logic) ─────────────────────────────────────────────────────

async def test_completion_menu_shows_filters_hides() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import CompletionMenu

    items = (("/model", "switch model", ()), ("/memory", "show memory", ()),
             ("/help", "show help", ()))

    class _Harness(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield CompletionMenu(items, id="completion")

    app = _Harness()
    async with app.run_test() as pilot:
        menu = app.query_one("#completion", CompletionMenu)
        assert menu.update_for("/m") is True
        assert menu.visible_now is True
        assert menu.current_name() in ("/model", "/memory", "/mcp")
        # non-slash text hides it
        assert menu.update_for("hello") is False
        assert menu.visible_now is False
        # slash with a space (already past the command word) hides it
        assert menu.update_for("/model foo") is False


# ── L2 integration: /model overlay + ctrl+p palette (full app pilot) ──────────

async def _build_app():  # type: ignore[no-untyped-def]
    from pathlib import Path
    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=Path.cwd(),
                             session_mode="interactive", event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    return AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                        model="echo")


async def test_slash_tiers_opens_select_overlay() -> None:
    from autoverse.frontends.repl.dialogs import SelectScreen

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "/tiers":
            await pilot.press(ch)
        await pilot.press("enter")
        # The handler runs as a worker and pushes the SelectScreen.
        opened = False
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, SelectScreen):
                opened = True
                break
        assert opened, "/tiers with no args should open a SelectScreen overlay"


async def test_ctrl_p_opens_command_palette() -> None:
    from autoverse.frontends.repl.dialogs import SelectScreen

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.press("ctrl+p")
        opened = False
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, SelectScreen):
                opened = True
                break
        assert opened, "ctrl+p should open the command palette SelectScreen"


async def _wait_choice_picking(app, pilot, *, want: bool = True) -> bool:
    """Pump the loop until the inline choice menu is (not) in its picking state."""
    for _ in range(40):
        await pilot.pause(0.05)
        try:
            picking = bool(app.query_one("#choice").picking_now)
        except Exception:
            picking = False
        if picking == want:
            return True
    return False


async def test_ask_user_callback_wired_and_handles_multi() -> None:
    """The TUI must register ``app.extra['ask_user']`` so AskUserQuestion resolves
    via the inline choice menu (not the 'no interactive UI' error) — and an
    allow_multiple question must collect multiple checked options."""
    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        callback = app._app_ctx.extra.get("ask_user")  # type: ignore[attr-defined]
        assert callable(callback), "TUI must wire app.extra['ask_user']"

        payload = {
            "title": "Pick",
            "questions": [
                {
                    "id": "q1",
                    "prompt": "Choose many",
                    "allow_multiple": True,
                    "options": [
                        {"id": "a", "label": "Alpha"},
                        {"id": "b", "label": "Beta"},
                        {"id": "c", "label": "Gamma"},
                    ],
                }
            ],
        }
        # The callback opens the inline menu and awaits a Future, which must run
        # from a Textual worker — the same context the engine task runs in (see
        # permission_adapter.py). Drive it through a worker here.
        worker = app.run_worker(callback(payload))  # type: ignore[arg-type]
        assert await _wait_choice_picking(app, pilot), "ask_user should open the inline choice menu"
        # Rows: [prompt] Alpha Beta Gamma [Other] [hint]; highlight starts on Alpha.
        await pilot.press("space")     # check Alpha
        await pilot.press("down")      # → Beta
        await pilot.press("down")      # → Gamma
        await pilot.press("space")     # check Gamma
        await pilot.press("enter")     # confirm the checked set
        for _ in range(40):
            await pilot.pause(0.05)
            if worker.is_finished:
                break
        assert worker.result == {"answers": [{"id": "q1", "selected": ["a", "c"]}], "cancelled": False}


async def test_ask_user_custom_answer_reuses_input_box() -> None:
    """Each question offers an 'Other' escape hatch: picking it
    hands free-text entry to the main input box and the typed answer is returned."""
    from autoverse.frontends.repl.widgets import CommandInput

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        callback = app._app_ctx.extra.get("ask_user")  # type: ignore[attr-defined]
        assert callable(callback)

        payload = {
            "questions": [
                {
                    "id": "q1",
                    "prompt": "Pick one",
                    "options": [
                        {"id": "a", "label": "Alpha"},
                        {"id": "b", "label": "Beta"},
                    ],
                }
            ],
        }
        worker = app.run_worker(callback(payload))  # type: ignore[arg-type]
        assert await _wait_choice_picking(app, pilot)
        # Rows: [prompt] Alpha Beta [Other] [hint]; arrow down to the Other row.
        await pilot.press("down")      # → Beta
        await pilot.press("down")      # → Other
        await pilot.press("enter")     # choose "Other" → free-text mode
        # The menu closes and the input box becomes the answer box.
        assert await _wait_choice_picking(app, pilot, want=False)
        for _ in range(40):
            await pilot.pause(0.05)
            if app._choice_awaiting_custom:  # type: ignore[attr-defined]
                break
        assert app._choice_awaiting_custom, "Other should switch to free-text input"  # type: ignore[attr-defined]
        for ch in "my own answer":
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if worker.is_finished:
                break
        assert worker.result == {
            "answers": [{"id": "q1", "selected": ["my own answer"]}],
            "cancelled": False,
        }
        # The input placeholder is restored once the answer is captured.
        assert (
            app.query_one("#input", CommandInput).placeholder
            == CommandInput.DEFAULT_PLACEHOLDER
        )


async def test_permission_inline_allow_and_deny() -> None:
    """Permission approval runs through the same inline choice menu: ``y`` allows
    once, Esc denies — replacing the old full-screen PermissionScreen modal."""
    from autoverse.types.capability import CapabilityTier
    from autoverse.types.tools import ToolCall

    def _call() -> ToolCall:
        return ToolCall(
            id="c1", name="bash", input={"command": "ls"},
            tier=CapabilityTier.READ_ONLY,
        )

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)

        # Allow once via the "y" shortcut.
        worker = app.run_worker(app.request_permission(_call()))  # type: ignore[arg-type]
        assert await _wait_choice_picking(app, pilot), "permission should open the inline menu"
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause(0.05)
            if worker.is_finished:
                break
        decision = worker.result
        assert decision.allowed is True
        assert decision.rule == "user-approved"

        # Deny via Esc.
        worker2 = app.run_worker(app.request_permission(_call()))  # type: ignore[arg-type]
        assert await _wait_choice_picking(app, pilot)
        await pilot.press("escape")
        for _ in range(40):
            await pilot.pause(0.05)
            if worker2.is_finished:
                break
        assert worker2.result.allowed is False


# ── command interaction wiring: confirm / toast / workflows (fake host) ───────

class _FakeHost:
    """A DialogHost stand-in for testing command handlers without Textual."""

    def __init__(self, *, select_value=None, confirm_value=True,
                 prompt_value=None) -> None:
        self._select_value = select_value
        self._confirm_value = confirm_value
        self._prompt_value = prompt_value
        self.toasts: list[tuple[str, str]] = []
        self.confirmed: list[str] = []
        self.selected: list[str] = []
        self.select_options: list[object] = []
        self.texts: list[tuple[str, str]] = []
        self.prompted: list[str] = []

    async def select(self, options, *, title="Select", placeholder="…",
                     multi=False):  # type: ignore[no-untyped-def]
        self.selected.append(title)
        self.select_options = list(options)
        return self._select_value

    async def confirm(self, message, *, title="Confirm", confirm_label="Confirm",
                      cancel_label="Cancel"):  # type: ignore[no-untyped-def]
        self.confirmed.append(message)
        return self._confirm_value

    async def prompt(self, title, *, placeholder="", initial="",
                     password=False):  # type: ignore[no-untyped-def]
        self.prompted.append(title)
        return self._prompt_value

    def toast(self, message, *, variant="info"):  # type: ignore[no-untyped-def]
        self.toasts.append((message, variant))

    async def show_text(self, body, *, title=""):  # type: ignore[no-untyped-def]
        self.texts.append((title, body))


def _ctx_with_host(host, **extra):  # type: ignore[no-untyped-def]
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.frontends.repl.commands import CommandContext

    reg = build_default_registry()
    base = {"ui": host}
    base.update(extra)
    return CommandContext(engine=extra.get("engine", object()), app=extra.get("app"),
                          registry=reg, extra=base), reg


class _ClearEngine:
    model = "echo"
    def __init__(self) -> None:
        self.cleared = 0
    async def clear_in_place(self) -> int:
        self.cleared = 7
        return 7


async def test_clear_confirm_cancel_keeps_history() -> None:
    from autoverse.frontends.repl.builtin_cmds import _cmd_clear

    host = _FakeHost(confirm_value=False)
    engine = _ClearEngine()
    ctx, _ = _ctx_with_host(host, engine=engine)
    res = await _cmd_clear(ctx, "")
    assert host.confirmed, "should ask for confirmation via overlay"
    assert engine.cleared == 0, "cancelled confirm must NOT clear history"
    assert res.display == ""


async def test_clear_confirm_yes_clears_and_toasts() -> None:
    from autoverse.frontends.repl.builtin_cmds import _cmd_clear

    host = _FakeHost(confirm_value=True)
    engine = _ClearEngine()
    ctx, _ = _ctx_with_host(host, engine=engine)
    res = await _cmd_clear(ctx, "")
    assert engine.cleared == 7
    assert res.display == ""  # feedback goes to toast, not scroll
    assert res.should_clear is True  # frontend must wipe the visible scrollback
    assert any("cleared" in m for m, _v in host.toasts)


async def test_clear_no_host_falls_back_to_text() -> None:
    from autoverse.frontends.repl.builtin_cmds import _cmd_clear
    from autoverse.frontends.repl.commands import CommandContext

    engine = _ClearEngine()
    ctx = CommandContext(engine=engine, app=None, registry=None, extra={})  # no "ui"
    res = await _cmd_clear(ctx, "")
    assert engine.cleared == 7  # no host → proceeds (headless default)
    assert "cleared 7" in res.display
    assert res.should_clear is True  # frontend must wipe the visible scrollback


async def test_goal_set_starts_turn_and_clear_alias_cancels(tmp_path) -> None:
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from autoverse.app.workflow.goal import GoalStore
    from autoverse.frontends.repl.builtin_cmds import _cmd_goal, build_default_registry
    from autoverse.frontends.repl.commands import CommandContext
    from autoverse.infra.persist.jsonl_store import JSONLStore
    from autoverse.ports import SessionMeta

    store = JSONLStore(tmp_path)
    sid = "goal-session"
    await store.create(sid, SessionMeta(session_id=sid))
    goal = GoalStore(store=store)
    app = AppContext(
        config=Config(),
        store=store,
        cwd=tmp_path,
        model="echo",
        events=None,  # type: ignore[arg-type]
    )
    app.goal = goal
    app.extra["session_id"] = sid
    ctx = CommandContext(
        engine=object(), app=app, registry=build_default_registry(), extra={}
    )

    result = await _cmd_goal(ctx, "all tests pass")
    assert result.prompt == "all tests pass"
    assert goal.get(sid) is not None
    meta = await store.load_meta(sid)
    state = meta.extra["goal_state"]
    assert isinstance(state, dict)
    assert state["active"]["condition"] == "all tests pass"  # type: ignore[index]

    result = await _cmd_goal(ctx, "cancel")
    assert goal.get(sid) is None
    assert "cleared" in result.display.lower()


class _CompactEngine:
    model = "echo"
    async def compact(self):  # type: ignore[no-untyped-def]
        return {"tokens_before": 7366, "tokens_after": 1347}


async def test_compact_in_tui_confirms_then_no_toast_no_clear() -> None:
    """In the TUI (DialogHost present) /compact first asks for confirmation
    (mirroring /clear), then on approval adds NO toast and does NOT clear the
    scrollback — the COMPACTING divider is the single, persistent cue and the
    history above it stays visible (MiMo parity, fixes the old duplicate pair)."""
    from autoverse.frontends.repl.builtin_cmds import _cmd_compact

    host = _FakeHost()  # confirm_value=True by default
    engine = _CompactEngine()
    ctx, _ = _ctx_with_host(host, engine=engine)
    res = await _cmd_compact(ctx, "")
    assert host.confirmed, "should ask for confirmation via overlay"
    assert res.display == ""           # nothing written to the scroll region
    assert host.toasts == []           # and NO toast (divider is the only cue)
    assert res.should_clear is False   # history is preserved, not wiped


async def test_compact_confirm_cancel_does_not_compact() -> None:
    """Cancelling the confirmation overlay aborts the compaction entirely."""
    from autoverse.frontends.repl.builtin_cmds import _cmd_compact

    host = _FakeHost(confirm_value=False)
    compacted: list[bool] = []

    class _Engine:
        model = "echo"
        async def compact(self):  # type: ignore[no-untyped-def]
            compacted.append(True)
            return {"tokens_before": 1, "tokens_after": 0}

    ctx, _ = _ctx_with_host(host, engine=_Engine())
    res = await _cmd_compact(ctx, "")
    assert host.confirmed, "should ask for confirmation via overlay"
    assert compacted == [], "cancelled confirm must NOT compact"
    assert res.display == ""


async def test_compact_headless_prints_one_line() -> None:
    """Headless / non-TUI (no DialogHost): a single confirmation line for scripts."""
    from autoverse.frontends.repl.builtin_cmds import _cmd_compact
    from autoverse.frontends.repl.commands import CommandContext

    engine = _CompactEngine()
    ctx = CommandContext(engine=engine, app=None, registry=None, extra={})  # no "ui"
    res = await _cmd_compact(ctx, "")
    assert "compacted 7,366" in res.display and "1,347" in res.display
    assert res.should_clear is False


async def test_workflows_select_runs_chosen() -> None:
    from autoverse.frontends.repl.builtin_cmds import _cmd_workflows
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from pathlib import Path

    class _Wf:
        def __init__(self, name: str) -> None:
            self.name = name
            self.summary = f"{name} pipeline"

    class _Workflows:
        def all(self):  # type: ignore[no-untyped-def]
            return [_Wf("commit"), _Wf("review"), _Wf("dream")]

    class _RunEngine:
        model = "echo"
        def __init__(self) -> None:
            self.ran: list[str] = []
        async def run_workflow(self, name: str, args: str) -> str:
            self.ran.append(name)
            return f"ran {name}"

    host = _FakeHost(select_value="dream")
    engine = _RunEngine()

    class _Store:
        async def list_sessions(self): return []  # type: ignore[no-untyped-def]

    app = AppContext(config=Config(), store=_Store(), cwd=Path.cwd(),  # type: ignore[arg-type]
                     model="echo", events=None)  # type: ignore[arg-type]
    app.extra = {"workflows": _Workflows()}
    from autoverse.frontends.repl.commands import CommandContext
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    reg = build_default_registry()
    ctx = CommandContext(engine=engine, app=app, registry=reg, extra={"ui": host})

    res = await _cmd_workflows(ctx, "")
    assert host.selected, "workflows should open a select overlay"
    titles = [getattr(option, "title") for option in host.select_options]
    assert "/review" in titles
    assert "dream" in titles
    assert "/dream" not in titles
    assert engine.ran == ["dream"], "selecting a non-slash workflow should run it"
    assert "ran dream" in res.display


async def test_workflows_headless_does_not_render_non_commands_as_slash_commands() -> None:
    from autoverse.frontends.repl.builtin_cmds import _cmd_workflows, build_default_registry
    from autoverse.frontends.repl.commands import CommandContext
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from pathlib import Path

    class _Wf:
        def __init__(self, name: str) -> None:
            self.name = name
            self.summary = f"{name} pipeline"

    class _Workflows:
        def all(self):  # type: ignore[no-untyped-def]
            return [_Wf("review"), _Wf("dream")]

    class _Engine:
        model = "echo"

    class _Store:
        async def list_sessions(self): return []  # type: ignore[no-untyped-def]

    app = AppContext(config=Config(), store=_Store(), cwd=Path.cwd(),  # type: ignore[arg-type]
                     model="echo", events=None)  # type: ignore[arg-type]
    app.extra = {"workflows": _Workflows()}
    ctx = CommandContext(
        engine=_Engine(),
        app=app,
        registry=build_default_registry(),
        extra={},
    )

    res = await _cmd_workflows(ctx, "")
    assert "  /review" in res.display
    assert "  dream" in res.display
    assert "run_workflow target" in res.display
    assert "  /dream" not in res.display


async def test_tiers_overlay_selection_updates_subagent_models_only() -> None:
    """End-to-end: ``/tiers`` opens THREE sequential tier pickers (deep →
    balanced → fast). Picking a model in each populates ``config.model_tiers``
    without changing the main-agent model."""
    from autoverse.frontends.repl.dialogs import SelectScreen

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "/tiers":
            await pilot.press(ch)
        await pilot.press("enter")
        # Three sequential pickers: pick the highlighted model in each.
        for _step in range(3):
            for _ in range(40):
                await pilot.pause(0.05)
                if isinstance(app.screen, SelectScreen):
                    break
            assert isinstance(app.screen, SelectScreen), f"picker {_step} should open"
            await pilot.press("enter")  # pick highlighted model
            # Let this overlay dismiss before the next one opens.
            for _ in range(40):
                await pilot.pause(0.05)
                if not isinstance(app.screen, SelectScreen):
                    break
        await pilot.pause(0.2)
        # Main agent is independent from the sub-agent tier mapping.
        live = getattr(app._app_ctx, "model", None)  # type: ignore[attr-defined]
        assert live == "echo"
        assert app._current_model() == "echo"  # type: ignore[attr-defined]
        # All three tiers were persisted onto the live (hot-swapped) config tree.
        tiers = app._app_ctx.config.model_tiers  # type: ignore[attr-defined]
        assert tiers.deep and tiers.balanced and tiers.fast
        from autoverse.frontends.repl.widgets import PromptMeta

        assert "echo" in app.query_one("#input-meta", PromptMeta).text_line


async def test_inline_completion_enter_accepts_best_match_and_runs() -> None:
    """Typing a command prefix + Enter accepts the best fuzzy match and runs it
    (regression: clearing the input hides the menu before Entered fires, so the
    accept must recompute from the raw text, not the menu highlight)."""
    from autoverse.frontends.repl.dialogs import SelectScreen, CompletionMenu

    app = await _build_app()
    seen: list[str] = []
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        orig = app._run_command  # type: ignore[attr-defined]

        def _patched(raw: str):  # type: ignore[no-untyped-def]
            seen.append(raw)
            return orig(raw)

        app._run_command = _patched  # type: ignore[attr-defined]
        await pilot.click("#input")
        for ch in "/mode":  # fuzzy-matches /model
            await pilot.press(ch)
        await pilot.pause(0.1)
        assert app.query_one("#completion", CompletionMenu).visible_now
        await pilot.press("enter")  # accept best match (/model) + run
        await pilot.pause(0.2)
        assert seen == ["/model"], f"Enter should accept+run /model, got {seen}"
        # /model with no args now prints model information; /tiers owns the
        # sub-agent picker.
        await pilot.pause(0.1)
        assert not isinstance(app.screen, SelectScreen)


async def test_inline_completion_up_navigates_menu_not_history() -> None:
    """With the menu open, ↑ moves its highlight up — it must NOT be swallowed
    by the input's history recall (regression: ↑ on the first row hit history
    recall and stopped the event, so the menu could only ever move down)."""
    from autoverse.frontends.repl.dialogs import CompletionMenu
    from autoverse.frontends.repl.widgets import CommandInput

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        inp = app.query_one("#input", CommandInput)
        # Seed history so the old buggy ↑ branch (needs self._history) would fire.
        inp._history = ["echo hi"]  # type: ignore[attr-defined]
        await pilot.click("#input")
        for ch in "/m":  # matches several commands → multi-row menu
            await pilot.press(ch)
        await pilot.pause(0.1)
        menu = app.query_one("#completion", CompletionMenu)
        assert menu.visible_now and menu.highlighted == 0
        await pilot.press("down")
        await pilot.pause(0.05)
        assert menu.highlighted == 1, "↓ should move the menu highlight down"
        await pilot.press("up")
        await pilot.pause(0.05)
        assert menu.highlighted == 0, "↑ should move the menu highlight back up"
        # The input was NOT clobbered by history recall and the menu is still up.
        assert inp.value == "/m"
        assert menu.visible_now


async def test_inline_completion_tab_completes_without_running() -> None:
    from autoverse.frontends.repl.dialogs import CompletionMenu
    from autoverse.frontends.repl.widgets import CommandInput

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "/mod":
            await pilot.press(ch)
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app.query_one("#input", CommandInput).value == "/model "
        assert not app.query_one("#completion", CompletionMenu).visible_now


# ── TextScreen + long read-only reports (§8.5/§8.6) ───────────────────────────

async def test_text_screen_opens_and_escape_closes() -> None:
    from textual.app import App
    from autoverse.frontends.repl.dialogs import TextScreen

    from textual import work

    done: list[object] = []

    class _Harness(App[None]):
        def on_mount(self) -> None:
            self._go()

        @work
        async def _go(self) -> None:
            done.append(await self.push_screen_wait(
                TextScreen("line1\nline2\nline3", title="Report")))

    app = _Harness()
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, TextScreen):
                break
        assert isinstance(app.screen, TextScreen)
        await pilot.press("escape")
        closed = False
        for _ in range(20):
            await pilot.pause(0.05)
            if not isinstance(app.screen, TextScreen):
                closed = True
                break
        assert closed, "Esc should close the TextScreen"
        assert done == [None]


async def test_config_opens_text_viewer_not_scroll() -> None:
    """/config must open a scrollable TextScreen, not dump JSON into the log."""
    from autoverse.frontends.repl.dialogs import TextScreen

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "/config":
            await pilot.press(ch)
        await pilot.press("enter")
        opened = False
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, TextScreen):
                opened = True
                break
        assert opened, "/config should open a TextScreen viewer"


async def test_report_helper_degrades_without_host() -> None:
    from autoverse.frontends.repl.builtin_cmds import _report
    from autoverse.frontends.repl.commands import CommandContext

    ctx = CommandContext(engine=object(), app=None, registry=None, extra={})
    res = await _report(ctx, "Title", "a\nb\nc")
    assert res.display == "a\nb\nc"  # printed, since no overlay host

async def test_report_long_body_uses_viewer_with_host() -> None:
    """A long report (>= 4 lines) opens the viewer and writes nothing to scroll."""
    from autoverse.frontends.repl.builtin_cmds import _report
    from autoverse.frontends.repl.commands import CommandContext

    host = _FakeHost()
    ctx = CommandContext(engine=object(), app=None, registry=None, extra={"ui": host})
    res = await _report(ctx, "Effective config", "a\nb\nc\nd\ne")
    assert res.display == ""  # nothing into the scroll region
    assert host.texts == [("Effective config", "a\nb\nc\nd\ne")]


async def test_report_short_body_stays_inline_no_viewer() -> None:
    """A short report (< 4 lines) is printed inline — a viewer for 1-2 lines is
    annoying. It auto-promotes to the viewer once it grows past the threshold."""
    from autoverse.frontends.repl.builtin_cmds import _report
    from autoverse.frontends.repl.commands import CommandContext

    host = _FakeHost()
    ctx = CommandContext(engine=object(), app=None, registry=None, extra={"ui": host})
    res = await _report(ctx, "Session tree", "root\n  child")
    assert res.display == "root\n  child"  # inline, not a popup
    assert host.texts == []  # viewer NOT opened for a 2-line report


async def test_get_dialog_host_accepts_host_missing_optional_prompt() -> None:
    """A host exposing the core overlay surface but lacking the *optional*
    ``prompt`` method must still be detected — capability probing degrades per
    method, not per host (regression: ``prompt`` was added to the protocol and
    the all-or-nothing ``isinstance`` check silently disabled overlay routing
    for every host missing it, so commands flooded the scroll region)."""
    from autoverse.frontends.repl.commands import CommandContext
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    class _NoPromptHost:
        async def select(self, options, *, title="Select", placeholder="…",
                         multi=False):  # type: ignore[no-untyped-def]
            return None

        async def confirm(self, message, *, title="Confirm", confirm_label="Confirm",
                          cancel_label="Cancel"):  # type: ignore[no-untyped-def]
            return True

        def toast(self, message, *, variant="info"):  # type: ignore[no-untyped-def]
            ...

        async def show_text(self, body, *, title=""):  # type: ignore[no-untyped-def]
            ...

    host = _NoPromptHost()
    assert not hasattr(host, "prompt")
    ctx = CommandContext(engine=object(), app=None, registry=None, extra={"ui": host})
    assert get_dialog_host(ctx) is host


async def test_get_dialog_host_rejects_host_missing_core_method() -> None:
    """A host missing a *core* overlay method (e.g. ``confirm``) is rejected:
    routing through it would raise at call time, so falling back to inline is
    the correct, safe behaviour."""
    from autoverse.frontends.repl.commands import CommandContext
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    class _NoConfirmHost:
        async def select(self, options, **kw):  # type: ignore[no-untyped-def]
            return None

        def toast(self, message, *, variant="info"):  # type: ignore[no-untyped-def]
            ...

        async def show_text(self, body, *, title=""):  # type: ignore[no-untyped-def]
            ...

    ctx = CommandContext(engine=object(), app=None, registry=None,
                         extra={"ui": _NoConfirmHost()})
    assert get_dialog_host(ctx) is None


# ── invariant: display/report commands never enter the LLM context window ─────

def _history_len(app) -> int:  # type: ignore[no-untyped-def]
    return len(app._engine._loop.conversation._messages)  # type: ignore[attr-defined]


async def test_display_commands_do_not_pollute_llm_context() -> None:
    """A read-only / report / toast command writes to the *display* region only;
    it must NOT add anything to the engine's message history (the LLM context
    window). Only real turns and prompt-template commands enter context."""
    from autoverse.frontends.repl.dialogs import TextScreen, SelectScreen

    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        before = _history_len(app)
        for cmd in ("/help", "/cost", "/usage", "/doctor", "/config",
                    "/memory", "/trace", "/queue", "/context", "/diff",
                    "/secrets", "/permissions"):
            await pilot.click("#input")
            for ch in cmd:
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.08)
            if isinstance(app.screen, (TextScreen, SelectScreen)):
                await pilot.press("escape")
                await pilot.pause(0.08)
        after = _history_len(app)
        assert after == before, (
            f"display/report commands must not enter LLM context "
            f"(history {before}→{after})"
        )
        # Sanity: a real conversational turn DOES grow the history.
        await pilot.click("#input")
        for ch in "hello":
            await pilot.press(ch)
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if _history_len(app) > before:
                break
        assert _history_len(app) > before, "a real turn must enter LLM context"


# ── invariant: no command floods the scroll region (visual pollution) ─────────

async def test_no_command_floods_scroll_region() -> None:
    """Every built-in command, in the TUI path (DialogHost present), must write
    at most 2 lines to the scroll region — long reports go to the TextScreen
    viewer, status goes to Toast/overlays. Guards against visual pollution
    (e.g. /config's 55-line JSON, /plugin's growing list) regressing."""
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.frontends.repl.commands import CommandContext
    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from pathlib import Path

    eng = await EngineBuilder(
        EngineOptions(model="echo", cwd=Path.cwd(), session_mode="interactive")
    ).build()
    app_ctx = getattr(eng, "app", None)
    reg = build_default_registry()
    host = _FakeHost()
    ctx = CommandContext(engine=eng, app=app_ctx, registry=reg, extra={"ui": host})

    offenders: list[tuple[str, int]] = []
    for name in sorted({n.lstrip("/") for n in reg.completion_names()}):
        spec = reg.get(name)
        assert spec is not None
        if spec.busy_label and spec.busy_label.startswith("Running /"):
            continue  # write-capable workflows are not safe catalogue probes
        try:
            res = await spec.handler(ctx, "")
        except Exception:
            continue
        if res.prompt is not None:
            continue  # prompt commands legitimately enter the conversation
        lines = (res.display.count("\n") + 1) if res.display else 0
        if lines >= 3:
            offenders.append((name, lines))
    assert not offenders, f"commands flooding the scroll region: {offenders}"


# ── prompt-template command routing (docs/reference/commands.md §4.4) ───────────────────

async def test_record_routing_only_when_routed() -> None:
    """``_record_routing`` records (agent, model, subtask) keyed by prompt text
    only when at least one routing field is set; plain prompts are not recorded."""
    from autoverse.frontends.repl.commands import CommandResult

    app = await _build_app()
    app._record_routing(CommandResult(prompt="hi", model="m"))  # type: ignore[attr-defined]
    assert app._turn_routing.get("hi") == (None, "m", False)  # type: ignore[attr-defined]
    app._record_routing(CommandResult(prompt="plain"))  # type: ignore[attr-defined]
    assert "plain" not in app._turn_routing  # type: ignore[attr-defined]


async def test_prompt_command_model_routing_overrides_and_restores() -> None:
    """A prompt-template command's ``model`` is applied per-turn via switch_model
    and restored afterwards; ``agent``/``subtask`` do not change the model."""
    app = await _build_app()
    async with app.run_test() as pilot:
        for _ in range(60):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._current_model() == "echo"  # type: ignore[attr-defined]

        # model routing → per-turn override + restore
        prev = app._apply_turn_routing((None, "routed-model", False))  # type: ignore[attr-defined]
        assert app._current_model() == "routed-model"  # type: ignore[attr-defined]
        assert prev == "echo"
        app._restore_turn_routing(prev)  # type: ignore[attr-defined]
        assert app._current_model() == "echo"  # type: ignore[attr-defined]

        # agent/subtask routing → no model change, nothing to restore, no crash
        prev2 = app._apply_turn_routing(("validator", None, True))  # type: ignore[attr-defined]
        assert prev2 is None
        assert app._current_model() == "echo"  # type: ignore[attr-defined]
