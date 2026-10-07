"""Unit: Sidebar dashboard — state→markup rendering, panel skipping, data
collection in the sink, and the wide-auto / Ctrl+G toggle in the app."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from pathlib import Path  # noqa: E402

from rich.cells import cell_len  # noqa: E402
from textual.content import Content  # noqa: E402

from autoverse.app.builder import EngineBuilder  # noqa: E402
from autoverse.app.extension_admin import ExtensionListItem  # noqa: E402
from autoverse.app.options import EngineOptions  # noqa: E402
from autoverse.frontends.repl.app import AutoVerseApp  # noqa: E402
from autoverse.frontends.repl.builtin_cmds import build_default_registry  # noqa: E402
from autoverse.frontends.repl.renderer import RenderContext  # noqa: E402
from autoverse.frontends.repl.sidebar import (  # noqa: E402
    Sidebar,
    SidebarExtension,
    SidebarState,
    _agent_row_markup,
    _todo_row_markup,
)
from autoverse.frontends.repl.theme import DEFAULT_THEME  # noqa: E402
from autoverse.frontends.repl.tui_sink import TuiEventSink  # noqa: E402
from autoverse.types import make_event  # noqa: E402
from autoverse.types.events import (  # noqa: E402
    RequestPayload,
    SubagentActivityPayload,
    ToolEndPayload,
    UsagePayload,
)
from autoverse.types.messages import Usage  # noqa: E402


def _plain(content) -> str:
    # Content has a .plain attribute with the unstyled text.
    return content.plain if hasattr(content, "plain") else str(content)


def test_sidebar_renders_all_dashboard_panels() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        model="dsv4", version="0.2.0", cwd="/x", context_window=128_000,
        tokens_used=12_500, cost_usd=0.0042, tps=42.0,
        todos=(("completed", "design"), ("in_progress", "diff bg"), ("pending", "costs")),
    )
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Context" in text and "12,500 tokens" in text
    assert "10% of 128.0k window" in text
    assert "42 tok/s" in text and "$0.0042 spent" in text
    assert "Directory" in text and "/x" in text
    assert "Todo  1/3" in text
    assert "✓ design" in text and "● diff bg" in text and "○ costs" in text
    # Brand line is its own pinned footer widget.
    assert "◆ AutoVerse v0.2.0" in _plain(sb._render_brand())  # type: ignore[attr-defined]


def test_sidebar_renders_loaded_extensions_panel() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        extensions=(
            SidebarExtension(kind="skill", name="excalidraw-diagram", source="user"),
            SidebarExtension(
                kind="plugin",
                name="browser",
                state="disabled",
                diagnostics_count=1,
            ),
        ),
    )
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Extensions  2" in text
    assert "Skill" in text and "excalidraw-diagram" in text
    assert "Plugin" in text and "browser [disabled, !1]" in text


def test_app_collects_loaded_extensions_from_admin_service() -> None:
    class _Admin:
        def list_skills(self) -> list[ExtensionListItem]:
            return [
                ExtensionListItem(
                    id="excalidraw-diagram",
                    kind="skill",
                    display_name="excalidraw-diagram",
                    source="user",
                    state="enabled",
                    enabled=True,
                    trusted=False,
                    summary="",
                )
            ]

        def list_plugins(self) -> list[ExtensionListItem]:
            return [
                ExtensionListItem(
                    id="browser",
                    kind="plugin",
                    display_name="browser",
                    source="plugin",
                    state="disabled",
                    enabled=False,
                    trusted=False,
                    summary="offline",
                    diagnostics_count=1,
                )
            ]

    async def _factory(sink: object) -> object:
        return object()

    app = AutoVerseApp(
        _factory,
        app_ctx=SimpleNamespace(extra={"extension_admin": _Admin()}),
        registry=build_default_registry(),
        cwd="/x",
        model="echo",
        context_window=8192,
    )
    assert app._loaded_extensions() == (  # type: ignore[attr-defined]
        SidebarExtension(kind="skill", name="excalidraw-diagram", source="user"),
        SidebarExtension(
            kind="plugin",
            name="browser",
            state="disabled",
            source="plugin",
            diagnostics_count=1,
        ),
    )


def test_sidebar_todo_long_rows_use_hanging_wrap() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        todos=(
            (
                "pending",
                "check/08-repl-isolation-contracts.md outdated (5/29 vs 33)",
            ),
        ),
    )
    lines = _plain(sb._render_dashboard()).splitlines()  # type: ignore[attr-defined]
    assert "Todo  0/1" in lines
    row = lines[lines.index("Todo  0/1") + 1:]
    assert row[0].startswith("○ check/08-repl-isolation-")
    assert row[1].startswith("  outdated")
    assert not row[1].startswith("○")


def test_sidebar_todo_path_like_text_is_not_split_into_fragments() -> None:
    plain = _plain(
        Content.from_markup(
            _todo_row_markup(
                DEFAULT_THEME,
                "pending",
                "_ALWAYS_COLLAPSE_TOOLS: read_image in tui_sink.py, docs missing",
            )
        )
    )
    assert "○ _ALWAYS_COLLAPSE_TOOLS:" in plain
    assert "  read_image in tui_sink.py, docs…" in plain


def test_sidebar_todo_cjk_rows_fit_display_cells() -> None:
    plain = _plain(
        Content.from_markup(
            _todo_row_markup(
                DEFAULT_THEME,
                "in_progress",
                "修复右侧栏待办列表换行后文本没有对齐的问题并保持可读",
            )
        )
    )
    lines = plain.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("● 修复")
    assert lines[1].startswith("  ")
    assert all(cell_len(line) <= 34 for line in lines)


def test_sidebar_todo_hides_when_all_items_completed() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        todos=(("completed", "design"), ("completed", "ship")),
    )
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Todo" not in text


def test_sidebar_agent_row_markup_carries_glyph_and_meta() -> None:
    # Running row: ● glyph + [type] activity + turn/elapsed meta.
    running = _agent_row_markup(DEFAULT_THEME, "builder", "bash: pytest", "running", 3, 8500.0)
    assert "● \\[builder] bash: pytest" in running and "3t" in running and "8s" in running
    # Terminal row: ✓ glyph for a done agent.
    done = _agent_row_markup(DEFAULT_THEME, "scout", "grep auth", "done", 2, 1200.0)
    assert "✓ \\[scout] grep auth" in done


def test_sidebar_dashboard_skips_empty_panels() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(model="echo", tokens_used=5)  # type: ignore[attr-defined]
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Context" in text          # always
    assert "Todo" not in text         # no todos → skipped
    assert "Agents" not in text       # agents are a separate widget, not in dashboard


def test_sidebar_percent_of_window() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(context_window=100_000, tokens_used=25_000)  # type: ignore[attr-defined]
    assert "25% of 100.0k window" in _plain(sb._render_dashboard())  # type: ignore[attr-defined]


def test_sidebar_renders_cache_hit_rate() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        context_window=100_000, tokens_used=10_000,
        cache_read_tokens=5_120, cache_write_tokens=100, cache_hit_rate=5120 / 5260,
    )
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "cache 97% hit" in text       # round(5120/5260*100) == 97
    assert "5.1k read / 100 write" in text


def test_sidebar_renders_changed_files_panel() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        changed_files=(
            ("src/foo.py", "changed", 12, 3),
            ("src/bar.py", "changed", 5, 0),
            ("old.py", "deleted", 0, 0),
        ),
    )
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Changed  3" in text
    assert "src/foo.py" in text and "+12" in text and "-3" in text
    assert "src/bar.py" in text and "+5" in text
    # Deleted file's path still appears (strike is a style, not text) but with no
    # +/- counts.
    assert "old.py" in text


def test_sidebar_skips_changed_panel_when_empty() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(tokens_used=5)  # type: ignore[attr-defined]
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Changed" not in text


def test_sidebar_renders_model_tiers_panel() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(  # type: ignore[attr-defined]
        model="main-model",
        model_tiers=(
            ("deep", "claude-opus"),
            ("balanced", "claude-sonnet"),
            ("fast", ""),  # unset → falls back to the main model
        ),
    )
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Sub-agent models" in text
    assert "deep" in text and "claude-opus" in text
    assert "balanced" in text and "claude-sonnet" in text
    # Unset tier renders the (main) fallback marker, not an empty value.
    assert "fast" in text and "(main)" in text


def test_sidebar_skips_tiers_panel_when_empty() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(tokens_used=5)  # type: ignore[attr-defined]
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "Sub-agent models" not in text


def test_sidebar_skips_cache_row_when_no_cache_activity() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(context_window=100_000, tokens_used=10_000)  # type: ignore[attr-defined]
    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]
    assert "cache" not in text           # no cache_read/write → row skipped


def test_sidebar_does_not_render_unknown_price_as_zero_cost() -> None:
    sb = Sidebar(theme=None)
    sb._state = SidebarState(tokens_used=10_000, cost_known=False)  # type: ignore[attr-defined]

    text = _plain(sb._render_dashboard())  # type: ignore[attr-defined]

    assert "cost unavailable" in text
    assert "$0.0000 spent" not in text


async def test_sink_collects_usage_todos_agents() -> None:
    class _FakeLog:
        def write(self, *_a: object) -> None: ...

    sink = TuiEventSink(_FakeLog(), RenderContext())  # type: ignore[arg-type]
    # cost_usd is intentionally NOT set on the payload — the codecs never set it,
    # so the sink must price the turn itself via its Meter using the model id.
    await sink.emit(make_event(UsagePayload(
        usage=Usage(input_tokens=100, output_tokens=40, cache_read_tokens=10),
        model="deepseek/deepseek-v4-pro")))
    assert sink.tokens_used == 150
    # Real cost from the registry pricing table (not the always-0 payload field):
    # input 100 @0.435/Mtok + cache_read 10 @0.0435/Mtok > 0.
    from autoverse.infra.observ.meter import Meter
    expected = Meter().cost_for(
        "deepseek/deepseek-v4-pro",
        Usage(input_tokens=100, output_tokens=40, cache_read_tokens=10),
    ).total_usd
    assert expected > 0.0
    assert abs(sink.cost_usd - expected) < 1e-12
    # Cumulative prompt-cache accounting: cache_read=10, prompt=input+cache_read=110.
    assert sink.cum_cache_read == 10
    assert sink.cum_prompt == 110
    assert abs(sink.cache_hit_rate - 10 / 110) < 1e-12

    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="t", tool_name="todo_write", output="Updated 3 todos.",
        metadata={"todos": [
            {"status": "completed", "content": "a"},
            {"status": "in_progress", "content": "b"},
            {"status": "pending", "content": "c"},
        ]})))
    assert sink.todos == (("completed", "a"), ("in_progress", "b"), ("pending", "c"))

    # A running heartbeat tracks a status-aware row keyed on job_id. The elapsed
    # is recomputed LIVE from the dispatch start (not the snapshot in the event),
    # so assert the structural fields and that the timer is a fresh positive value.
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="j1", subagent_type="builder", activity="bash: pytest",
        status="running", turn=2, elapsed_ms=3400.0)))
    rows = sink.agents
    assert len(rows) == 1
    assert rows[0][:5] == ("j1", "builder", "bash: pytest", "running", 2)
    assert rows[0][5] >= 0.0  # live elapsed (ms since dispatch), not the snapshot
    # A later heartbeat for the SAME job updates the row in place (no duplicate).
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="j1", subagent_type="builder", activity="running",
        status="running", turn=3, elapsed_ms=5000.0)))
    rows = sink.agents
    assert len(rows) == 1
    assert rows[0][:5] == ("j1", "builder", "running", "running", 3)
    # Terminal status drops it from the active set.
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="j1", subagent_type="builder", activity="done", status="done",
        detail="ok", elapsed_ms=5200.0)))
    assert sink.agents == ()

    # A failed terminal also clears the sidebar row (no #log card is committed
    # for SUBAGENT_ACTIVITY — the agent tool's TOOL_END is the canonical card).
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="j2", subagent_type="scout", activity="grep",
        status="running", turn=1, elapsed_ms=100.0)))
    rows = sink.agents
    assert len(rows) == 1
    assert rows[0][:5] == ("j2", "scout", "grep", "running", 1)
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="j2", subagent_type="scout", activity="failed", status="failed",
        detail="boom")))
    assert sink.agents == ()


async def test_sink_preserves_prompt_cache_across_split_usage_events() -> None:
    """A trailing output-only usage fragment must not erase prompt cache."""

    class _FakeLog:
        def write(self, *_a: object) -> None: ...

    sink = TuiEventSink(_FakeLog(), RenderContext())  # type: ignore[arg-type]
    await sink.emit(
        make_event(RequestPayload(0, "", "", (), "", "deepseek/deepseek-v4-pro", 1, 0.0, 0))
    )
    await sink.emit(make_event(UsagePayload(
        usage=Usage(input_tokens=100, output_tokens=1, cache_read_tokens=900),
        model="deepseek/deepseek-v4-pro",
    )))
    await sink.emit(make_event(UsagePayload(
        usage=Usage(output_tokens=40), model="deepseek/deepseek-v4-pro",
    )))

    assert sink.tokens_used == 1_040
    assert sink.last_input == 100
    assert sink.last_output == 40
    assert sink.last_cache_read == 900
    assert sink.usage_kind == "context"


def test_dashboard_snapshot_round_trip_restores_all_persisted_panels() -> None:
    class _FakeLog:
        def write(self, *_a: object) -> None: ...

    source = TuiEventSink(_FakeLog(), RenderContext())  # type: ignore[arg-type]
    source.tokens_used = 1_234
    source.last_input = 200
    source.last_output = 34
    source.last_cache_read = 1_000
    source.cost_usd = 0.125
    source.usage_seq = 3
    source.usage_kind = "context"
    source.cum_cache_read = 4_000
    source.cum_cache_write = 50
    source.cum_prompt = 5_000
    source.todos = (("in_progress", "verify"),)
    source._changed_files["/tmp/a.py"] = ("changed", 2, 1)  # type: ignore[attr-defined]

    target = TuiEventSink(_FakeLog(), RenderContext())  # type: ignore[arg-type]
    assert target.restore_dashboard_snapshot(source.dashboard_snapshot()) is True
    assert target.dashboard_snapshot() == source.dashboard_snapshot()


async def test_sink_marks_unpriced_usage_cost_as_unavailable() -> None:
    class _FakeLog:
        def write(self, *_a: object) -> None: ...

    sink = TuiEventSink(_FakeLog(), RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(UsagePayload(
        usage=Usage(input_tokens=100), model="unknown/unpriced-model",
    )))

    assert sink.tokens_used == 100
    assert sink.cost_usd == 0.0
    assert sink.cost_known is False


async def test_sink_collects_changed_files() -> None:
    class _FakeLog:
        def write(self, *_a: object, **_k: object) -> None: ...
        def write_collapsible(self, *_a: object, **_k: object) -> None: ...

    sink = TuiEventSink(_FakeLog(), RenderContext())  # type: ignore[arg-type]

    # write_file → a "changed" entry with +/- counts.
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="w1", tool_name="write_file", output="ok",
        metadata={"file_path": "/proj/a.py", "added": 10, "removed": 0})))
    assert sink.changed_files == (("/proj/a.py", "changed", 10, 0),)

    # A second edit to the SAME file accumulates +/-.
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="e1", tool_name="edit_file", output="ok",
        metadata={"file_path": "/proj/a.py", "added": 4, "removed": 2})))
    assert sink.changed_files == (("/proj/a.py", "changed", 14, 2),)

    # A different file appends a new row (insertion order preserved).
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="w2", tool_name="write_file", output="ok",
        metadata={"file_path": "/proj/b.py", "added": 3, "removed": 1})))
    assert sink.changed_files == (
        ("/proj/a.py", "changed", 14, 2),
        ("/proj/b.py", "changed", 3, 1),
    )

    # delete_file flips the entry to "deleted" and zeroes the counts.
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="d1", tool_name="delete_file", output="ok",
        metadata={"file_path": "/proj/b.py", "is_dir": False})))
    assert sink.changed_files == (
        ("/proj/a.py", "changed", 14, 2),
        ("/proj/b.py", "deleted", 0, 0),
    )

    # A FAILED write is not counted (no real mutation).
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="w3", tool_name="write_file", output="boom", is_error=True,
        error_kind="io_error",
        metadata={"file_path": "/proj/c.py", "added": 99, "removed": 0})))
    assert all(p != "/proj/c.py" for p, *_ in sink.changed_files)

    # Non-file tools (e.g. todo_write, read_file) never appear.
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="r1", tool_name="read_file", output="...",
        metadata={"file_path": "/proj/a.py", "lines": 20})))
    assert len(sink.changed_files) == 2  # unchanged by a read


async def test_subagent_one_live_card_updated_in_place_no_duplicate() -> None:
    """A dispatched sub-agent produces exactly ONE main-log card: SUBAGENT_ACTIVITY
    mounts it on dispatch (running) and UPDATES it in place to the completion
    summary. The ``agent`` tool's TOOL_END is suppressed (no second card / no
    duplicate). Plain ``write`` must NOT be used for either."""
    agent_calls: list[tuple[str, str, bool]] = []  # (job_id, text, done)
    plain_writes: list[str] = []

    class _FakeLog:
        def write(self, r: object, *, classes: str = "") -> None:
            plain_writes.append(str(r))

        def write_markdown(self, t: str) -> None:
            plain_writes.append(t)

        def write_collapsible(self, title: object, body: object, *,
                              collapsed: bool = True, classes: str = "") -> None:
            plain_writes.append(str(title))

        def write_or_update_agent(self, job_id: str, renderable: object, *,
                                  done: bool = False) -> None:
            agent_calls.append((job_id, str(renderable), done))

    sink = TuiEventSink(_FakeLog(), RenderContext(width=100))  # type: ignore[arg-type]
    # Foreground sequence: running → done (SUBAGENT_ACTIVITY) → TOOL_END(agent).
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="sa-1", subagent_type="scout", activity="grep",
        status="running", turn=1, detail="find the file", task="find the file")))
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="sa-1", subagent_type="scout", activity="done", status="done",
        turn=2, detail="File: x.py", elapsed_ms=4600.0)))
    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="sa-1", tool_name="agent", output="File: x.py:45",
        metadata={"subagent_type": "scout", "description": "find the file"})))

    # Two activity events → two in-place updates of the SAME job's card; the
    # agent TOOL_END added NOTHING. Plain write was never used for the agent.
    assert [c[0] for c in agent_calls] == ["sa-1", "sa-1"]
    assert agent_calls[0][2] is False   # running: not done
    assert agent_calls[1][2] is True    # terminal: done → releases the card key
    # Head shows the persistent task brief (MiMo "{Type} Task — {desc}") on BOTH
    # the live and the terminal card — the brief survives the heartbeat/terminal
    # emits that don't repeat it (injected from the per-job record).
    assert "Scout Task — find the file" in agent_calls[0][1]
    assert "Scout Task — find the file" in agent_calls[1][1]
    assert plain_writes == []           # no duplicate via the plain path


async def test_app_sidebar_auto_show_and_toggle() -> None:
    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    # WIDE terminal → sidebar auto-shown
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        sb = app.query_one("#sidebar", Sidebar)
        assert not sb.has_class("-hidden")          # auto-shown on wide
        await app.run_action("toggle_sidebar")
        await pilot.pause(0.05)
        assert sb.has_class("-hidden")              # Ctrl+G hides
        await app.run_action("toggle_sidebar")
        await pilot.pause(0.05)
        assert not sb.has_class("-hidden")          # toggles back


async def test_app_sidebar_hidden_on_narrow() -> None:
    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        sb = app.query_one("#sidebar", Sidebar)
        assert sb.has_class("-hidden")              # narrow → hidden by default


async def test_agent_rows_persist_identity_across_ticks() -> None:
    """The 10 Hz sidebar tick (set_state with only elapsed changing) must NOT
    re-mount the clickable AgentRow widgets — re-mounting them 10×/s swallows
    real mouse clicks (down+up spanning a tick). Identity must stay stable, with
    text updated in place."""
    from textual.app import App, ComposeResult

    from autoverse.frontends.repl.sidebar import AgentRow

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield Sidebar(id="sidebar")

    app = _Host()
    async with app.run_test(size=(60, 30)) as pilot:
        sb = app.query_one("#sidebar", Sidebar)
        sb.set_state(SidebarState(
            agents=(("job-1", "scout", "grep", "running", 1, 1000.0),)))
        await pilot.pause(0.05)
        row_before = app.query_one(AgentRow)
        id_before = id(row_before)

        # Simulate many ticks: same agent, only elapsed/turn advancing.
        for ms in (2000.0, 3000.0, 4000.0, 5000.0):
            sb.set_state(SidebarState(
                agents=(("job-1", "scout", "grep", "running", 2, ms),)))
            await pilot.pause(0.02)

        rows = list(app.query(AgentRow))
        assert len(rows) == 1
        assert id(rows[0]) == id_before          # SAME widget instance (not re-mounted)
        assert rows[0].job_id == "job-1"
        # Text was updated in place to the latest elapsed.
        assert "5s" in str(rows[0].render())


async def test_agent_row_click_enters_under_ticking() -> None:
    """A real coordinate click on an agent row enters its context even though the
    sidebar is being refreshed on every tick (the regression: rows used to be
    rebuilt each tick, so the click missed)."""
    from textual.app import App, ComposeResult

    from autoverse.frontends.repl.sidebar import AgentRow

    class _Host(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.clicked: list[str] = []

        def compose(self) -> ComposeResult:
            yield Sidebar(id="sidebar")

        def on_sidebar_agent_clicked(self, msg: "Sidebar.AgentClicked") -> None:
            self.clicked.append(msg.job_id)

    app = _Host()
    async with app.run_test(size=(60, 30)) as pilot:
        sb = app.query_one("#sidebar", Sidebar)
        sb.set_state(SidebarState(
            agents=(("job-1", "scout", "grep", "running", 1, 1000.0),)))
        await pilot.pause(0.05)
        row = app.query_one(AgentRow)
        cx = row.region.x + row.region.width // 2
        cy = row.region.y + row.region.height // 2
        # Tick the sidebar, then click — under the old code the row would have
        # been re-mounted between renders and the click would miss.
        sb.set_state(SidebarState(
            agents=(("job-1", "scout", "grep", "running", 1, 1100.0),)))
        await pilot.click(offset=(cx, cy))
        await pilot.pause(0.05)
        assert app.clicked == ["job-1"]


async def test_log_agent_card_click_enters_subagent() -> None:
    """Clicking a sub-agent card in the main conversation log drills into its
    context — MiMo's clickable Task parity (the card posts AgentCardClicked)."""
    from textual.app import App, ComposeResult

    from autoverse.frontends.repl.conversation_log import AgentCard, ConversationLog

    class _Host(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.clicked: list[str] = []

        def compose(self) -> ComposeResult:
            yield ConversationLog(id="log")

        def on_conversation_log_agent_card_clicked(
            self, msg: "ConversationLog.AgentCardClicked"
        ) -> None:
            self.clicked.append(msg.job_id)

    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one("#log", ConversationLog)
        log.write_or_update_agent("job-7", "│ Scout Task — find the file")
        await pilot.pause(0.05)
        card = app.query_one(AgentCard)
        cx = card.region.x + card.region.width // 2
        cy = card.region.y + card.region.height // 2
        await pilot.click(offset=(cx, cy))
        await pilot.pause(0.05)
        assert app.clicked == ["job-7"]
