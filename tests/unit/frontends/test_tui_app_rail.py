"""Pilot: SessionRail integration in AutoVerseApp (docs/architecture/frontend-adapters.md §4/§5).

Headless Textual ``run_test`` exercises the wired-up rail: it mounts and shows
on a wide terminal, lists the injected sessions, switches engines on a row
click (replaying the target's stored history into ``#log``), and honours the
``ctrl+s`` focus/hide toggle + Esc return. A shared :class:`JSONLStore` backs both
the listing query and the switch factory, exactly as the CLI wires it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from pathlib import Path  # noqa: E402

from autoverse.app.builder import EngineBuilder  # noqa: E402
from autoverse.app.options import EngineOptions  # noqa: E402
from autoverse.app.session_query import StoreSessionQuery  # noqa: E402
from autoverse.frontends.repl.app import AutoVerseApp  # noqa: E402
from autoverse.frontends.repl.builtin_cmds import build_default_registry  # noqa: E402
from autoverse.frontends.repl.session_rail import SessionRail, SessionRow  # noqa: E402
from autoverse.infra.persist.jsonl_store import JSONLStore  # noqa: E402
from autoverse.ports import SessionMeta  # noqa: E402
from autoverse.types.messages import (  # noqa: E402
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

_WIDE = (140, 40)


def _log_text(app: object) -> str:
    from textual.widgets import Collapsible, Markdown, Static

    from autoverse.frontends.repl.conversation_log import ConversationLog

    log = app.query_one("#log", ConversationLog)  # type: ignore[attr-defined]
    parts: list[str] = []
    for node in log.query(Static):
        v = getattr(node, "visual", None)
        plain = getattr(v, "plain", None)
        if isinstance(plain, str):
            parts.append(plain)
    for node in log.query(Markdown):
        src = getattr(node, "source", None)
        if isinstance(src, str):
            parts.append(src)
    for node in log.query(Collapsible):
        if isinstance(node.title, str):
            parts.append(node.title)
    return "\n".join(parts)


async def _make_app(tmp_path: Path) -> tuple[AutoVerseApp, JSONLStore]:
    store = JSONLStore(tmp_path)
    # Seed two sessions; "other" carries a stored user message to replay.
    await store.create("aaaaaaaaaaaa", SessionMeta(session_id="aaaaaaaaaaaa",
                       model="echo", title="First", cwd=str(tmp_path)))
    await store.create("bbbbbbbbbbbb", SessionMeta(session_id="bbbbbbbbbbbb",
                       model="echo", title="Second", cwd=str(tmp_path)))
    await store.append(
        "bbbbbbbbbbbb",
        Message(role="user", content=[TextBlock("hello from second")]),
    )

    query = StoreSessionQuery(store, base_dir=tmp_path)
    registry = build_default_registry()
    holder: dict[str, AutoVerseApp] = {}

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo", cwd=tmp_path, session_id="aaaaaaaaaaaa",
            session_mode="interactive", event_sink=sink, store=store,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    async def _switch(session_id: str, sink: object) -> object:
        opts = EngineOptions(
            model="echo", cwd=tmp_path, session_id=session_id or None,
            session_mode="interactive", event_sink=sink, store=store,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(
        _factory, app_ctx=None, registry=registry,
        cwd=str(tmp_path), model="echo",
        session_query=query, switch_engine=_switch,
    )
    holder["app"] = app
    return app, store


async def _wait_ready(app: AutoVerseApp, pilot) -> None:
    for _ in range(60):
        if app._engine_ready:  # type: ignore[attr-defined]
            break
        await pilot.pause(0.05)
    assert app._engine_ready  # type: ignore[attr-defined]
    # The rail now starts collapsed by design. Most tests in this module exercise
    # the rail after the user has opened it, so establish that state explicitly
    # without moving focus away from the input.
    if app._session_query is not None:  # type: ignore[attr-defined]
        app._rail_mode = "shown"  # type: ignore[attr-defined]
        app._apply_rail_visibility()  # type: ignore[attr-defined]
        app._refresh_session_rail()  # type: ignore[attr-defined]


async def test_rail_shows_and_lists_sessions(tmp_path) -> None:
    app, _ = await _make_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        # Let the debounced rail refresh worker land.
        for _ in range(40):
            rail = app.query_one("#session-rail", SessionRail)
            rows = list(rail.query(SessionRow))
            if len(rows) >= 2:
                break
            await pilot.pause(0.05)
        rail = app.query_one("#session-rail", SessionRail)
        assert not rail.has_class("-hidden")
        ids = {r.session_id for r in rail.query(SessionRow)}
        assert ids == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


async def test_rail_hidden_without_query(tmp_path) -> None:
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=tmp_path, session_mode="interactive",
                             event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        rail = app.query_one("#session-rail", SessionRail)
        assert rail.has_class("-hidden")  # no listing port → never shown (§4.4)


async def test_switch_replays_target_history(tmp_path) -> None:
    app, _ = await _make_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        for _ in range(40):
            if len(list(app.query_one("#session-rail", SessionRail).query(SessionRow))) >= 2:
                break
            await pilot.pause(0.05)

        # Switch to the second session by posting the row-click message.
        app.post_message(SessionRail.SessionClicked("bbbbbbbbbbbb"))
        for _ in range(80):
            await pilot.pause(0.05)
            if app._session_id == "bbbbbbbbbbbb":  # type: ignore[attr-defined]
                break
        assert app._session_id == "bbbbbbbbbbbb"  # type: ignore[attr-defined]
        # The stored user message of the target session is replayed into #log.
        for _ in range(40):
            await pilot.pause(0.05)
            if "hello from second" in _log_text(app):
                break
        assert "hello from second" in _log_text(app)


async def test_switch_replays_agent_card_as_clickable(tmp_path) -> None:
    """A replayed persisted agent result must stay an AgentCard after switching.

    Regression: session replay rendered the stored `agent` tool result as a plain
    Static, so the card looked like a subagent card but did not post
    AgentCardClicked and could not open AgentView.
    """
    from autoverse.frontends.repl.agent_view import AgentView
    from autoverse.frontends.repl.conversation_log import AgentCard

    app, store = await _make_app(tmp_path)
    await store.append(
        "bbbbbbbbbbbb",
        Message(
            role="assistant",
            content=(
                ToolUseBlock(
                    id="toolu-agent-1",
                    name="agent",
                    input={
                        "subagent_type": "scout",
                        "description": "Find callers",
                        "task": "Find every caller of validate_session.",
                    },
                ),
            ),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_use_id="toolu-agent-1",
                    content="Found two callers in auth.py and session.py.",
                    metadata={
                        "subagent_type": "scout",
                        "description": "Find callers",
                        "tool_count": 3,
                    },
                ),
            ),
        ),
    )

    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        app.post_message(SessionRail.SessionClicked("bbbbbbbbbbbb"))
        for _ in range(80):
            await pilot.pause(0.05)
            if app._session_id == "bbbbbbbbbbbb":  # type: ignore[attr-defined]
                break
        assert app._session_id == "bbbbbbbbbbbb"  # type: ignore[attr-defined]

        for _ in range(40):
            await pilot.pause(0.05)
            cards = list(app.query(AgentCard))
            if cards:
                break
        card = app.query_one(AgentCard)
        cx = card.region.x + card.region.width // 2
        cy = card.region.y + card.region.height // 2
        await pilot.click(offset=(cx, cy))
        await pilot.pause(0.1)

        view = app.query_one("#agentview", AgentView)
        assert app._agent_view_active is True  # type: ignore[attr-defined]
        assert view.current is not None
        assert view.current.job_id == "history-agent:toolu-agent-1"
        assert "Found two callers" in "".join(
            getattr(ev.payload, "text", "") for ev in view.current.events
        )


async def test_switch_rehydrates_sidebar_state_from_history(tmp_path) -> None:
    """Replay must restore sink-derived sidebar state, not only visible log text."""
    from autoverse.frontends.repl.sidebar import Sidebar

    app, store = await _make_app(tmp_path)
    todos = [
        {"status": "completed", "content": "done"},
        {"status": "pending", "content": "next"},
    ]
    await store.append(
        "bbbbbbbbbbbb",
        Message(
            role="assistant",
            content=(
                ToolUseBlock("todo-1", "todo_write", {"todos": todos}),
                ToolUseBlock("edit-1", "edit_file", {"file_path": str(tmp_path / "a.py")}),
            ),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    "todo-1",
                    "Updated 2 todos.",
                    metadata={"todos": todos},
                ),
                ToolResultBlock(
                    "edit-1",
                    "Edited",
                    metadata={
                        "file_path": str(tmp_path / "a.py"),
                        "diff": "@@ -1 +1 @@\n-old\n+new\n",
                        "added": 1,
                        "removed": 1,
                    },
                ),
            ),
        ),
    )

    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        app.post_message(SessionRail.SessionClicked("bbbbbbbbbbbb"))
        for _ in range(80):
            await pilot.pause(0.05)
            sidebar = app.query_one("#sidebar", Sidebar)
            state = sidebar._state  # type: ignore[attr-defined]
            if state.todos and state.changed_files:
                break

        state = app.query_one("#sidebar", Sidebar)._state  # type: ignore[attr-defined]
        assert state.todos == (("completed", "done"), ("pending", "next"))
        assert any(
            path == "a.py" and status == "changed" and added == 1 and removed == 1
            for path, status, added, removed in state.changed_files
        )


async def test_switch_replays_exit_plan_as_markdown(tmp_path) -> None:
    """ExitPlanMode live path renders plan text as Markdown; replay should too."""
    from textual.widgets import Markdown

    app, store = await _make_app(tmp_path)
    plan = "## Plan\n\n1. Inspect\n2. Patch"
    await store.append(
        "bbbbbbbbbbbb",
        Message(
            role="assistant",
            content=(ToolUseBlock("plan-1", "ExitPlanMode", {"plan": plan}),),
        ),
        Message(
            role="user",
            content=(ToolResultBlock("plan-1", "Plan proposed", metadata={"plan": plan}),),
        ),
    )

    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        app.post_message(SessionRail.SessionClicked("bbbbbbbbbbbb"))
        for _ in range(80):
            await pilot.pause(0.05)
            if any("## Plan" in (getattr(m, "source", "") or "") for m in app.query(Markdown)):
                break

        sources = [getattr(m, "source", "") or "" for m in app.query(Markdown)]
        assert any("## Plan" in src and "1. Inspect" in src for src in sources)


async def test_ctrl_s_focuses_rail_and_hides_when_already_focused(tmp_path) -> None:
    app, _ = await _make_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        for _ in range(40):
            if len(list(app.query_one("#session-rail", SessionRail).query(SessionRow))) >= 2:
                break
            await pilot.pause(0.05)

        wrap = app.query_one("#input-wrap")
        assert not wrap.has_class("-dim")

        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert app._rail_focused()  # type: ignore[attr-defined]
        # Accent jumps to the rail → input bar dims (§6.1).
        assert wrap.has_class("-dim")

        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not app._rail_focused()  # type: ignore[attr-defined]
        assert not wrap.has_class("-dim")
        rail = app.query_one("#session-rail", SessionRail)
        assert not rail.has_class("-hidden")

        # Ctrl+S enters the rail again; pressing it while the rail already holds
        # focus returns to input AND hides the rail (Chinese input friendly: no
        # ASCII `h` required for the common hide path).
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert app._rail_focused()  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert not app._rail_focused()  # type: ignore[attr-defined]
        assert rail.has_class("-hidden")
        assert not wrap.has_class("-dim")


async def _make_two_group_app(tmp_path: Path) -> AutoVerseApp:
    """Current session in dir A; a second session lives in dir B (§6.3.4)."""
    dir_a, dir_b = tmp_path / "proj-a", tmp_path / "proj-b"
    dir_a.mkdir()
    dir_b.mkdir()
    store = JSONLStore(tmp_path)
    await store.create("aaaaaaaaaaaa", SessionMeta(session_id="aaaaaaaaaaaa",
                       model="echo", title="In A", cwd=str(dir_a)))
    await store.create("cccccccccccc", SessionMeta(session_id="cccccccccccc",
                       model="echo", title="In B", cwd=str(dir_b)))

    query = StoreSessionQuery(store, base_dir=tmp_path)
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=dir_a, session_id="aaaaaaaaaaaa",
                             session_mode="interactive", event_sink=sink, store=store)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    async def _switch(session_id: str, sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=dir_a, session_id=session_id or None,
                             session_mode="interactive", event_sink=sink, store=store)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    return AutoVerseApp(
        _factory, app_ctx=None, registry=registry,
        cwd=str(dir_a), model="echo",
        session_query=query, switch_engine=_switch,
    )


async def test_current_row_marked_while_input_focused(tmp_path) -> None:
    """The current session row carries ``-current`` (its gray bar) at startup,
    when focus is in the input area and the rail is unfocused (§6.1①)."""
    app, _ = await _make_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        for _ in range(40):
            rows = list(app.query_one("#session-rail", SessionRail).query(SessionRow))
            if len(rows) >= 2:
                break
            await pilot.pause(0.05)
        # Focus is in the input, not the rail (the persistent marker is the point).
        assert not app._rail_focused()  # type: ignore[attr-defined]
        current = [
            r for r in app.query_one("#session-rail", SessionRail).query(SessionRow)
            if r.session_id == app._session_id  # type: ignore[attr-defined]
        ]
        assert current and current[0].has_class("-current")


async def test_non_current_group_collapsed_by_default(tmp_path) -> None:
    app = await _make_two_group_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        # Wait for the refresh worker to apply the default-collapse pass.
        for _ in range(40):
            ids = {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}
            if ids == {"aaaaaaaaaaaa"}:
                break
            await pilot.pause(0.05)
        # Only the current session's group is expanded → only its row is mounted;
        # the other group is collapsed, so its row stays hidden (§6.3.4).
        ids = {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}
        assert ids == {"aaaaaaaaaaaa"}
        assert len(app._collapsed_groups) == 1  # type: ignore[attr-defined]


async def test_manual_expand_survives_refresh(tmp_path) -> None:
    """A user-expanded default-collapsed group must not snap shut on refresh."""
    app = await _make_two_group_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        for _ in range(40):
            if len(app._collapsed_groups) == 1:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        other = next(iter(app._collapsed_groups))  # type: ignore[attr-defined]

        # User expands the collapsed group.
        app.post_message(SessionRail.GroupToggled(other))
        for _ in range(40):
            ids = {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}
            if ids == {"aaaaaaaaaaaa", "cccccccccccc"}:
                break
            await pilot.pause(0.05)

        # A subsequent refresh must respect the manual expand (seen → no re-collapse).
        app._refresh_session_rail()  # type: ignore[attr-defined]
        for _ in range(20):
            await pilot.pause(0.05)
        ids = {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}
        assert ids == {"aaaaaaaaaaaa", "cccccccccccc"}
        assert other not in app._collapsed_groups  # type: ignore[attr-defined]


async def test_current_group_can_be_collapsed(tmp_path) -> None:
    """The current session's group is collapsible and stays shut across refresh."""
    app = await _make_two_group_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        # Wait for the default pass: current group expanded → its row is mounted.
        for _ in range(40):
            ids = {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}
            if ids == {"aaaaaaaaaaaa"}:
                break
            await pilot.pause(0.05)
        cur_group = app._last_cur_group  # type: ignore[attr-defined]
        assert cur_group

        # User collapses the current group → its session row disappears.
        app.post_message(SessionRail.GroupToggled(cur_group))
        for _ in range(40):
            ids = {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}
            if not ids:
                break
            await pilot.pause(0.05)
        assert not {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}

        # A refresh while staying in the same group must NOT reopen it.
        app._refresh_session_rail()  # type: ignore[attr-defined]
        for _ in range(20):
            await pilot.pause(0.05)
        assert cur_group in app._collapsed_groups  # type: ignore[attr-defined]
        assert not {r.session_id for r in app.query_one("#session-rail", SessionRail).query(SessionRow)}


async def test_h_hides_rail_and_ctrl_s_reopens(tmp_path) -> None:
    app, _ = await _make_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        for _ in range(40):
            if len(list(app.query_one("#session-rail", SessionRail).query(SessionRow))) >= 2:
                break
            await pilot.pause(0.05)

        rail = app.query_one("#session-rail", SessionRail)
        assert not rail.has_class("-hidden")

        # Focus the rail, then press `h` to collapse it (§4.1).
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert app._rail_focused()  # type: ignore[attr-defined]
        await pilot.press("h")
        await pilot.pause(0.1)
        assert rail.has_class("-hidden")
        assert not app._rail_focused()  # type: ignore[attr-defined]

        # ctrl+s brings it back and refocuses it.
        await pilot.press("ctrl+s")
        await pilot.pause(0.1)
        assert not rail.has_class("-hidden")
        assert app._rail_focused()  # type: ignore[attr-defined]


async def test_rail_spinner_animates_for_background_running_while_foreground_idle(
    tmp_path,
) -> None:
    """§5.6.3 regression: a detached background turn's rail row must keep
    spinning even though the foreground session is idle (no RUNNING band).

    Before the fix the spinner tick was gated on ``InputMode.RUNNING``, so the
    glyph froze the instant the current session went idle while another ran in
    the background."""
    app, _ = await _make_app(tmp_path)
    async with app.run_test(size=_WIDE) as pilot:
        await _wait_ready(app, pilot)
        for _ in range(40):
            if len(list(app.query_one("#session-rail", SessionRail).query(SessionRow))) >= 2:
                break
            await pilot.pause(0.05)

        # Foreground (aaaa…) is idle; mark the OTHER session as a live background
        # turn and let the rail refresh overlay its ``running`` status (§5.6.3).
        from autoverse.frontends.repl.input_queue import InputMode

        assert not app._mode_stack.has(InputMode.RUNNING)  # type: ignore[attr-defined]
        app._running_sessions.add("bbbbbbbbbbbb")  # type: ignore[attr-defined]
        app._refresh_session_rail()  # type: ignore[attr-defined]
        rail = app.query_one("#session-rail", SessionRail)
        for _ in range(40):
            if any(s.status == "running" for s in rail._state.sessions):  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert any(s.status == "running" for s in rail._state.sessions)  # type: ignore[attr-defined]

        # Two consecutive ticks must advance the rail's spinner frame even with
        # the foreground idle — proving the background branch drives it.
        app._tick()  # type: ignore[attr-defined]
        frame_a = rail._state.spinner_frame  # type: ignore[attr-defined]
        app._tick()  # type: ignore[attr-defined]
        frame_b = rail._state.spinner_frame  # type: ignore[attr-defined]
        assert frame_a != frame_b
