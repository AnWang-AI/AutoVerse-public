import asyncio
from dataclasses import dataclass
from pathlib import Path
import time

import pytest

from autoverse.core.loop import worktree_mode
from autoverse.frontends.web.session import SessionManager, WebSession
from autoverse.ports import SessionSummary
from autoverse.types.tools import ToolResult
from autoverse.types import ImageBlock


class _Query:
    def __init__(self, rows: list[SessionSummary]) -> None:
        self.rows = rows

    async def list(self) -> list[SessionSummary]:
        return self.rows


class _Store:
    async def load_meta(self, _sid: str):
        return type("Meta", (), {"extra": {}})()


class _BoomEngine:
    def run(self, *_args, **_kwargs):
        async def _gen():
            raise RuntimeError("web boom")
            yield None

        return _gen()


@dataclass
class _Sink:
    activity: str = ""


@dataclass
class _LiveSession:
    busy: bool
    activity: str = ""
    awaiting: bool = False

    def __post_init__(self) -> None:
        self.sink = _Sink(self.activity)
        self._ask_futures = {"q": object()} if self.awaiting else {}
        self.bridge = type("Bridge", (), {"blocks": []})()

    def is_busy(self) -> bool:
        return self.busy

    def _plan_state(self):
        return None


def _manager(summary: SessionSummary, live: _LiveSession) -> SessionManager:
    manager = object.__new__(SessionManager)
    manager.session_query = _Query([summary])
    manager.store = _Store()
    manager.sessions = {summary.session_id: live}
    return manager


def test_resolve_cwd_falls_back_to_default_for_missing_or_empty(tmp_path) -> None:
    """A new-session working directory is honoured only when it exists; an empty
    or non-existent path falls back to the manager default so the engine always
    builds against a real directory."""
    from pathlib import Path

    manager = object.__new__(SessionManager)
    manager.cwd = tmp_path

    # Empty / None → default
    assert manager.resolve_cwd(None) == tmp_path
    assert manager.resolve_cwd("") == tmp_path
    # Missing directory → default
    assert manager.resolve_cwd(str(tmp_path / "does-not-exist")) == tmp_path
    # A file (not a dir) → default
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert manager.resolve_cwd(str(f)) == tmp_path
    # An existing directory → that directory (resolved)
    sub = tmp_path / "project"
    sub.mkdir()
    assert manager.resolve_cwd(str(sub)) == Path(sub).resolve()


async def test_view_session_defers_engine_and_activation_reuses_loaded_messages(
    tmp_path: Path,
) -> None:
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.ports import SessionMeta
    from autoverse.types import Message, TextBlock
    from tests._doubles import MemStore

    class CountingStore(MemStore):
        def __init__(self) -> None:
            super().__init__()
            self.load_calls = 0

        async def load(self, session_id: str, *, since: int = 0) -> list[Message]:
            self.load_calls += 1
            return await super().load(session_id, since=since)

    store = CountingStore()
    sid = "lazy-view"
    await store.create(sid, SessionMeta(session_id=sid, cwd=str(tmp_path)))
    message = Message(role="user", content=(TextBlock("hello"),))
    await store.append(sid, message)
    manager = SessionManager(
        cwd=tmp_path,
        model="echo",
        theme=DEFAULT_THEME,
        version="test",
        context_window=0,
        registry=None,
        store=store,
        session_query=None,
        data_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    async def build_engine(
        _sink: object,
        _permissions: object,
        session_id: str,
        _secrets: object,
        cwd: Path,
        initial_messages: tuple[Message, ...] | None = None,
        initial_messages_complete: bool = True,
        model: str | None = None,
    ) -> object:
        captured["messages"] = initial_messages
        captured["messages_complete"] = initial_messages_complete
        captured["model"] = model
        app = type(
            "App",
            (),
            {"model": "echo", "cwd": cwd, "extra": {}, "config": None},
        )()
        return type("Engine", (), {"session_id": session_id, "app": app})()

    manager._build_engine = build_engine  # type: ignore[method-assign]

    view = await manager.get_or_create_view(sid)

    assert view.is_active is False
    assert store.load_calls == 1
    active = await manager.activate(sid)
    assert active is view
    assert active.is_active is True
    assert captured["messages"] == (message,)
    assert captured["messages_complete"] is True
    assert store.load_calls == 1


async def test_native_browser_hot_swaps_already_active_session(tmp_path: Path) -> None:
    manager = object.__new__(SessionManager)
    from autoverse.frontends.web.browser_host import NativeBrowserBroker

    old_browser = object()
    app = type("App", (), {"browser": old_browser, "extra": {}})()
    session = type(
        "Session",
        (),
        {"app_ctx": app, "session_id": "active-session"},
    )()
    manager.browser_host = NativeBrowserBroker()
    manager.sessions = {"active-session": session}
    manager._build_lock = asyncio.Lock()

    await manager.bind_native_browser_to_active_sessions()

    assert getattr(app.browser, "native_browser_session") is True
    assert app.extra["retired_browser_backends"] == [old_browser]


async def test_native_browser_hot_swap_waits_for_engine_build() -> None:
    manager = object.__new__(SessionManager)
    from autoverse.frontends.web.browser_host import NativeBrowserBroker

    old_browser = object()
    app = type("App", (), {"browser": old_browser, "extra": {}})()
    session = type(
        "Session",
        (),
        {"app_ctx": None, "session_id": "building-session"},
    )()
    manager.browser_host = NativeBrowserBroker()
    manager.sessions = {"building-session": session}
    manager._build_lock = asyncio.Lock()

    await manager._build_lock.acquire()
    bind_task = asyncio.create_task(manager.bind_native_browser_to_active_sessions())
    await asyncio.sleep(0)
    assert not bind_task.done()

    session.app_ctx = app
    manager._build_lock.release()
    await bind_task

    assert getattr(app.browser, "native_browser_session") is True
    assert app.extra["retired_browser_backends"] == [old_browser]


async def test_jsonl_view_reads_two_turn_tail_and_pages_older_turns(
    tmp_path: Path,
) -> None:
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.infra.persist.jsonl_store import JSONLStore
    from autoverse.ports import SessionMeta
    from autoverse.types import Message, TextBlock

    store = JSONLStore(tmp_path / "sessions")
    sid = "bounded-view"
    await store.create(sid, SessionMeta(session_id=sid, cwd=str(tmp_path)))
    for turn in range(4):
        await store.append(
            sid,
            Message(role="user", content=(TextBlock(f"prompt-{turn}"),)),
            Message(role="assistant", content=(TextBlock(f"answer-{turn}"),)),
        )
    manager = SessionManager(
        cwd=tmp_path,
        model="echo",
        theme=DEFAULT_THEME,
        version="test",
        context_window=0,
        registry=None,
        store=store,
        session_query=None,
        data_dir=tmp_path,
    )

    view = await manager.get_or_create_view(sid)
    snapshot = view.bridge.snapshot()
    assert view.build_state()["usage_available"] is False

    user_text = [
        item["block"]["text"]
        for item in snapshot["blocks"]
        if item["block"]["kind"] == "user"
    ]
    assert user_text == ["prompt-2", "prompt-3"]
    assert str(snapshot["before_cursor"]).startswith("jsonl:")

    older = await view.history_page(str(snapshot["before_cursor"]))
    assert [
        item["block"]["text"]
        for item in older["blocks"]
        if item["block"]["kind"] == "user"
    ] == ["prompt-1"]

    captured: dict[str, object] = {}

    async def build_engine(
        _sink: object,
        _permissions: object,
        session_id: str,
        _secrets: object,
        cwd: Path,
        initial_messages: tuple[Message, ...] | None = None,
        initial_messages_complete: bool = True,
        model: str | None = None,
    ) -> object:
        captured["complete"] = initial_messages_complete
        captured["model"] = model
        app = type(
            "App", (), {"model": "echo", "cwd": cwd, "extra": {}, "config": None}
        )()
        return type("Engine", (), {"session_id": session_id, "app": app})()

    manager._build_engine = build_engine  # type: ignore[method-assign]
    await manager.activate(sid)
    assert captured["complete"] is False


async def test_cold_view_restores_persisted_dashboard_and_session_model(
    tmp_path: Path,
) -> None:
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.infra.persist.jsonl_store import JSONLStore
    from autoverse.ports import SessionMeta
    from autoverse.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

    store = JSONLStore(tmp_path / "sessions")
    sid = "dashboard-restore"
    persisted_model = "deepseek/deepseek-v4-pro"
    await store.create(
        sid,
        SessionMeta(session_id=sid, model=persisted_model, cwd=str(tmp_path)),
    )
    await store.append(
        sid,
        Message(role="user", content=(TextBlock("hello"),)),
        Message(role="assistant", content=(
            ToolUseBlock("todo", "todo_write", {}),
            ToolUseBlock("write", "write_file", {}),
        )),
        Message(role="user", content=(
            ToolResultBlock("todo", "updated", metadata={"todos": [
                {"status": "in_progress", "content": "verify"},
            ]}),
            ToolResultBlock("write", "written", metadata={
                "file_path": str(tmp_path / "a.py"), "added": 2, "removed": 1,
            }),
        )),
        Message(
            role="assistant",
            content=(TextBlock("world"),),
            metadata={
                "usage_model": persisted_model,
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 34,
                    "cache_read_tokens": 1_000,
                    "cache_write_tokens": 0,
                },
            },
        ),
    )
    manager = SessionManager(
        cwd=tmp_path,
        model="echo",
        theme=DEFAULT_THEME,
        version="test",
        context_window=100_000,
        registry=None,
        store=store,
        session_query=None,
        data_dir=tmp_path,
    )

    view = await manager.get_or_create_view(sid)
    state = view.build_state()

    assert view.model == persisted_model
    assert state["usage_kind"] == "context"
    assert state["usage_available"] is True
    assert state["tokens"] == 1_234
    assert state["cost"] > 0
    assert state["cache_read"] == 1_000
    assert state["todos"] == [{"status": "in_progress", "content": "verify"}]
    assert state["changed"] == [{
        "path": "a.py", "status": "changed", "added": 2, "removed": 1,
    }]


async def test_legacy_cold_view_recovers_usage_and_all_tool_state_without_breaking_cursor(
    tmp_path: Path,
) -> None:
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.infra.observ.model import LLMCallRecord, TraceModel, TurnRecord
    from autoverse.infra.observ.store import TraceStore
    from autoverse.infra.persist.jsonl_store import JSONLStore
    from autoverse.ports import SessionMeta
    from autoverse.types import Message, TextBlock, ToolResultBlock, ToolUseBlock, Usage

    store = JSONLStore(tmp_path / "sessions")
    sid = "legacy-dashboard"
    model = "deepseek/deepseek-v4-pro"
    await store.create(sid, SessionMeta(session_id=sid, model=model, cwd=str(tmp_path)))
    await store.append(
        sid,
        Message(role="user", content=(TextBlock("first"),)),
        Message(role="assistant", content=(
            ToolUseBlock("todo-1", "todo_write", {}),
            ToolUseBlock("write-1", "write_file", {}),
        )),
        Message(role="user", content=(
            ToolResultBlock("todo-1", "updated", metadata={"todos": [
                {"status": "in_progress", "content": "verify legacy"},
            ]}),
            ToolResultBlock("write-1", "written", metadata={
                "file_path": str(tmp_path / "legacy.py"), "added": 4, "removed": 1,
            }),
        )),
        Message(role="assistant", content=(TextBlock("done"),)),
    )
    for turn in range(1, 4):
        await store.append(
            sid,
            Message(role="user", content=(TextBlock(f"prompt-{turn}"),)),
            Message(role="assistant", content=(TextBlock(f"answer-{turn}"),)),
        )

    trace_store = TraceStore(tmp_path / "traces")
    trace = TraceModel.new(sid, model, wall=1.0)
    record = TurnRecord(turn_index=3, model=model, start_mono=1.0, end_mono=2.0)
    record.llm = LLMCallRecord(
        model=model,
        start_mono=1.0,
        end_mono=2.0,
        usage=Usage(input_tokens=100, output_tokens=10, cache_read_tokens=900),
    )
    trace_store.append_turn(trace, record)

    manager = SessionManager(
        cwd=tmp_path,
        model="echo",
        theme=DEFAULT_THEME,
        version="test",
        context_window=100_000,
        registry=None,
        store=store,
        session_query=None,
        data_dir=tmp_path,
    )
    view = await manager.get_or_create_view(sid)
    snapshot = view.bridge.snapshot()
    state = view.build_state()

    assert state["usage_kind"] == "turn_total"
    assert state["tokens"] == 1_010
    assert state["cache_read"] == 900
    assert state["cost"] > 0
    assert state["todos"] == [{"status": "in_progress", "content": "verify legacy"}]
    assert state["changed"] == [{
        "path": "legacy.py", "status": "changed", "added": 4, "removed": 1,
    }]

    # Legacy migration must not rewrite the JSONL after load_tail, because an
    # os.replace would invalidate the inode-bound history cursor.
    older = await view.history_page(str(snapshot["before_cursor"]))
    assert any(item["block"]["kind"] == "user" for item in older["blocks"])


def test_changed_file_relative_path_stays_session_relative(tmp_path) -> None:
    from autoverse.frontends.web.session import _rel

    rel = "src/autoverse/frontends/web/session.py"
    abs_path = tmp_path / rel

    assert _rel(rel, str(tmp_path)) == rel
    assert _rel(str(abs_path), str(tmp_path)) == rel


def test_running_status_carries_a_stable_wall_clock_start_for_reconnect() -> None:
    session = object.__new__(WebSession)
    session._running = True
    session._turn_started_at_ms = time.time() * 1000.0 - 2500.0
    session._interaction = 3
    session._queue = []
    session.sink = _Sink("Bash: pytest")
    published = []
    session.bridge = type(
        "Bridge", (), {"publish_status": lambda _self, status: published.append(status)}
    )()

    session._publish_status()

    assert published[0]["running"] is True
    assert published[0]["started_at_ms"] == session._turn_started_at_ms
    assert 2000.0 <= time.time() * 1000.0 - published[0]["started_at_ms"] < 4000.0


async def test_web_turn_exception_renders_visible_error_block(tmp_path) -> None:
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.repl.tui_sink import TuiEventSink
    from autoverse.frontends.web.bridge import SessionBridge
    from autoverse.frontends.web.web_log import WebLog

    bridge = SessionBridge()
    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=80)
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), ctx)
    session = WebSession(
        "web-boom",
        _BoomEngine(),
        sink,
        bridge,
        permission=None,
        ctx=ctx,
        registry=None,
        theme=DEFAULT_THEME,
        model="echo",
        cwd=str(tmp_path),
        version="0",
        context_window=0,
        store=None,
    )
    session.app_ctx = type("Ctx", (), {"extra": {}})()
    session._publish_state = lambda: None  # type: ignore[method-assign]
    session._publish_status = lambda: None  # type: ignore[method-assign]

    await session._run_turn_locked("hello")

    rendered = str(bridge.blocks)
    assert "web boom" in rendered
    assert "error" in rendered
    assert bridge.status["running"] is False


async def test_web_turn_checkpoints_plan_state_in_rewind_metadata(tmp_path) -> None:
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.repl.tui_sink import TuiEventSink
    from autoverse.frontends.web.bridge import SessionBridge
    from autoverse.frontends.web.web_log import WebLog

    seen: dict[str, object] = {}
    plan_before = {"active": True, "pending_approval": False, "reason": "inspect"}

    class Engine:
        app = type(
            "App",
            (),
            {"extra": {"plan_mode": plan_before}, "cwd": tmp_path, "snapshot": None},
        )()

        async def capture_rewind_point(self) -> str:
            return "tree-before-turn"

        def run(self, *_args, **kwargs):
            seen.update(kwargs.get("message_metadata") or {})

            async def _gen():
                if False:
                    yield None

            return _gen()

    bridge = SessionBridge()
    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=80)
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), ctx)
    session = WebSession(
        "web-plan-checkpoint",
        Engine(),
        sink,
        bridge,
        permission=None,
        ctx=ctx,
        registry=None,
        theme=DEFAULT_THEME,
        model="echo",
        cwd=str(tmp_path),
        version="0",
        context_window=0,
        store=None,
    )

    await session._run_turn_locked("inspect the plan")

    assert seen["snapshot_hash"] == "tree-before-turn"
    assert seen["plan_mode_snapshot"] == plan_before
    assert seen["plan_mode_snapshot"] is not plan_before


async def test_rewind_waits_for_active_turn_to_stop_before_restoring() -> None:
    stopped = asyncio.Event()
    order: list[str] = []

    class Engine:
        def abort(self, _reason: str) -> None:
            order.append("abort")
            stopped.set()

        async def rewind_turn(self, _rewind_id: str):
            order.append("rewind")
            assert task.done()
            return "wrong", (), ("changed.py",)

    async def active_turn() -> None:
        await stopped.wait()
        order.append("turn-stopped")

    session = object.__new__(WebSession)
    task = asyncio.create_task(active_turn())
    session._cur_task = task
    session._queue = [{"id": "queued"}]
    session._lock = asyncio.Lock()
    session.session_id = "rewind-session"
    session.store = None
    session.engine = Engine()
    session.sink = type("Sink", (), {"reset_after_rewind": lambda _self: None})()
    session.bridge = type("Bridge", (), {"reset_blocks": lambda _self: None})()
    session.replay_history = lambda: _async_none()  # type: ignore[method-assign]
    session._publish_state = lambda: None  # type: ignore[method-assign]
    session._publish_status = lambda: None  # type: ignore[method-assign]

    out = await session.rewind_turn_for_edit("turn-id")

    assert order == ["abort", "turn-stopped", "rewind"]
    assert session._queue == []
    assert out["text"] == "wrong"
    assert out["changed"] == ["changed.py"]


async def test_abort_releases_pending_approvals_and_questions() -> None:
    calls: list[str] = []
    session = object.__new__(WebSession)
    session._queue = []
    session.engine = type(
        "Engine", (), {"abort": lambda _self, reason: calls.append(f"abort:{reason}")}
    )()
    session.permission = type(
        "Permission", (), {"cancel_all": lambda _self: calls.append("approvals")}
    )()
    future: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
    session._ask_futures = {"question-1": future}
    session.bridge = type(
        "Bridge",
        (),
        {"remove_block": lambda _self, block_id: calls.append(f"remove:{block_id}")},
    )()

    session.abort()

    assert calls == ["abort:cancel", "approvals", "remove:ask:question-1"]
    assert future.result() == []
    assert session._ask_futures == {}


def test_rewind_snapshot_retargets_active_worktree(tmp_path: Path) -> None:
    from autoverse.infra.snapshot import GitShadowSnapshot

    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    snapshot_root = tmp_path / "snapshots"
    def snapshot_for_cwd(raw: str) -> GitShadowSnapshot:
        target = Path(raw)
        return GitShadowSnapshot(target, snapshot_root / f"{target.name}.git")

    session = object.__new__(WebSession)
    session.cwd = str(source)
    session.app_ctx = type(
        "Ctx",
        (),
        {
            "cwd": worktree,
            "snapshot": GitShadowSnapshot(source, snapshot_root / "source.git"),
            "extra": {
                "web_snapshot_root": str(snapshot_root),
                "rewind_snapshot_resolver": snapshot_for_cwd,
            },
        },
    )()

    session._ensure_rewind_snapshot_for_current_cwd()

    assert session.app_ctx.snapshot.work_tree == worktree


async def _async_none() -> None:
    return None


@dataclass
class _StaleSink:
    """A sink whose ``activity`` is deliberately PINNED to a stale value.

    The status bar must derive its label from the events the consumer dequeues,
    never from the producer's live ``sink.activity`` (which races ahead of the
    streamed conversation). Pinning ``activity`` to "TodoWrite" here would make
    any code that peeks at it surface the wrong tool — so these tests fail loudly
    if the tracker ever regresses to reading it again. ``_tool_label`` mirrors
    the real sink's formatter for the label text."""

    activity: str = "TodoWrite"

    def _tool_label(self, name: str, tool_input: dict) -> str:
        target = ""
        for key in ("file_path", "path", "command", "pattern", "url", "query"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                target = val.strip()
                break
        if not target:
            return name or "tool"
        sep = ": " if name in ("bash", "Bash") else " "
        return f"{name}{sep}{target}"


def _track_session() -> tuple[WebSession, list[dict]]:
    session = object.__new__(WebSession)
    session._running = True
    session._turn_started_at_ms = time.time() * 1000.0
    session._interaction = 1
    session._queue = []
    session._activity_label = ""
    session._activity_started_at_ms = session._turn_started_at_ms
    session._active_ops = {}
    session._active_op_names = {}
    session._stream_phase = ""
    session.sink = _StaleSink()
    published: list[dict] = []
    session.bridge = type(
        "Bridge", (), {"publish_status": lambda _self, st: published.append(st)}
    )()
    return session, published


def test_finished_tool_label_does_not_linger_in_status_bar() -> None:
    """Regression: the bar showed/stuck on ``TodoWrite`` when it shouldn't.

    Drives think → TodoWrite start → TodoWrite end and asserts the published
    label drops the finished tool's name — even though the (stale) sink activity
    still says "TodoWrite", because the label is advanced by consumed events."""
    from autoverse.types.events import (
        ThinkingDeltaPayload,
        ToolEndPayload,
        ToolStartPayload,
        make_event,
    )

    session, published = _track_session()

    session._track_activity(make_event(ThinkingDeltaPayload(text="…")))
    assert published[-1]["label"] == "Thinking…"

    session._track_activity(
        make_event(
            ToolStartPayload(tool_use_id="t1", tool_name="TodoWrite", tool_input={})
        )
    )
    assert published[-1]["label"] == "TodoWrite"

    session._track_activity(
        make_event(ToolEndPayload(tool_use_id="t1", tool_name="TodoWrite", output="ok"))
    )
    # Must NOT linger on "TodoWrite" — even though sink.activity still names it.
    assert published[-1]["label"] == "Responding…"
    assert session._activity_label == "Responding…"

    session._track_activity(make_event(ThinkingDeltaPayload(text="more")))
    assert published[-1]["label"] == "Thinking…"


def test_parallel_tool_end_reveals_other_running_tool() -> None:
    """When one of two concurrent tools ends, the bar shows the one still running."""
    from autoverse.types.events import ToolEndPayload, ToolStartPayload, make_event

    session, published = _track_session()

    session._track_activity(
        make_event(ToolStartPayload(tool_use_id="a", tool_name="bash",
                                    tool_input={"command": "npm test"}))
    )
    session._track_activity(
        make_event(ToolStartPayload(tool_use_id="b", tool_name="TodoWrite",
                                    tool_input={}))
    )
    assert published[-1]["label"] == "TodoWrite"

    # TodoWrite finishes first; bash is still running → bar reveals bash.
    session._track_activity(
        make_event(ToolEndPayload(tool_use_id="b", tool_name="TodoWrite", output="ok"))
    )
    assert published[-1]["label"] == "bash: npm test"


def test_tool_end_without_matching_id_clears_same_named_tool() -> None:
    """A missing/rewritten TOOL_END id must not leave TodoWrite stuck as active."""
    from autoverse.types.events import ToolEndPayload, ToolStartPayload, make_event

    session, published = _track_session()

    session._track_activity(
        make_event(
            ToolStartPayload(
                tool_use_id="call_123", tool_name="TodoWrite", tool_input={}
            )
        )
    )
    assert published[-1]["label"] == "TodoWrite"

    session._track_activity(
        make_event(ToolEndPayload(tool_use_id="", tool_name="TodoWrite", output="ok"))
    )

    assert published[-1]["label"] == "Responding…"
    assert session._active_ops == {}


def test_publish_status_never_echoes_live_sink_activity() -> None:
    """Regression: ``_publish_status`` must not fall back to ``sink.activity``.

    Status frames pushed from non-event paths (turn start, footer refresh, queue
    edit) ran with an empty ``_activity_label`` while the producer's live
    ``sink.activity`` still named a finished/ahead tool — leaking "TodoWrite"
    onto the bar. The frame must reflect only the consumer-derived label."""
    session, published = _track_session()
    session._activity_label = ""  # boundary state: no consumed phase yet

    session._publish_status()

    assert session.sink.activity == "TodoWrite"  # live producer state is stale…
    assert published[-1]["label"] == "Responding…"  # …but the frame ignores it
    assert published[-1]["activity"] == ""


def test_idle_status_does_not_leak_previous_turn_start() -> None:
    session = object.__new__(WebSession)
    session._running = False
    session._turn_started_at_ms = time.time() * 1000.0 - 2500.0
    session._interaction = 3
    session._queue = []
    session.sink = _Sink()
    published = []
    session.bridge = type(
        "Bridge", (), {"publish_status": lambda _self, status: published.append(status)}
    )()

    session._publish_status()

    assert published[0]["running"] is False
    assert published[0]["started_at_ms"] is None


def test_queued_image_message_publishes_only_attachment_count() -> None:
    session = object.__new__(WebSession)
    session._running = True
    session._cur_task = None
    session._queue = []
    session._uid = 0
    session._turn_started_at_ms = 1000.0
    session._activity_label = ""
    session._activity_started_at_ms = None
    session._interaction = 1
    published: list[dict] = []
    session.bridge = type(
        "Bridge", (), {"publish_status": lambda _self, status: published.append(status)}
    )()

    img = ImageBlock(media_type="image/png", data="QUJD")
    out = session.submit("describe", (img,))

    assert out["queued"] is True
    assert out["item"] == {"id": "q1", "text": "describe", "attachment_count": 1}
    assert published[-1]["queued"] == [
        {"id": "q1", "text": "describe", "attachment_count": 1}
    ]


class _ExternallySwitchedBrowser:
    async def poll_page_state_change(self) -> dict[str, object]:
        return {
            "previous_active_page_id": "page-alpha",
            "active_page_id": "page-beta",
            "page_count": 3,
            "opened_page_ids": ["page-gamma"],
            "closed_page_ids": [],
            "updated_page_ids": ["page-beta"],
        }


class _UnavailableBrowserState:
    async def poll_page_state_change(self) -> dict[str, object]:
        raise RuntimeError("native host disconnected")


class _InitialBrowserState:
    async def poll_page_state_change(self) -> dict[str, object]:
        return {
            "initial_state": True,
            "previous_active_page_id": None,
            "active_page_id": "page-existing",
            "page_count": 1,
            "opened_page_ids": ["page-existing"],
            "closed_page_ids": [],
            "updated_page_ids": [],
        }


@pytest.mark.asyncio
async def test_web_turn_warns_model_when_user_switched_native_browser_page() -> None:
    session = object.__new__(WebSession)
    session.app_ctx = type("App", (), {"browser": _ExternallySwitchedBrowser()})()

    notice = await session._browser_state_notice()

    assert "native browser state changed" in notice
    assert "page-alpha" in notice
    assert "page-beta" in notice
    assert "Opened pages: page-gamma" in notice
    assert "changed revision, URL, title, or loading state: page-beta" in notice
    assert "3 open pages" in notice
    assert "browser_list_pages" in notice
    assert "explicit page_id" in notice


@pytest.mark.asyncio
async def test_web_turn_ignores_browser_state_reconciliation_failure() -> None:
    session = object.__new__(WebSession)
    session.app_ctx = type("App", (), {"browser": _UnavailableBrowserState()})()

    assert await session._browser_state_notice() == ""


@pytest.mark.asyncio
async def test_web_turn_exposes_initial_native_pages_to_the_model() -> None:
    session = object.__new__(WebSession)
    session.app_ctx = type("App", (), {"browser": _InitialBrowserState()})()

    notice = await session._browser_state_notice()

    assert "initial synchronized state" in notice
    assert "Opened pages: page-existing" in notice
    assert "browser_list_pages" in notice


@pytest.mark.asyncio
async def test_web_turn_passes_browser_change_as_hidden_llm_context(tmp_path) -> None:
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.repl.tui_sink import TuiEventSink
    from autoverse.frontends.web.bridge import SessionBridge
    from autoverse.frontends.web.web_log import WebLog

    seen: dict[str, object] = {}

    class Engine:
        app = type(
            "App",
            (),
            {
                "extra": {},
                "cwd": tmp_path,
                "snapshot": None,
                "browser": _ExternallySwitchedBrowser(),
            },
        )()

        def run(self, *_args, **kwargs):
            seen.update(kwargs.get("message_metadata") or {})

            async def _gen():
                if False:
                    yield None

            return _gen()

    bridge = SessionBridge()
    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=80)
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), ctx)
    session = WebSession(
        "web-browser-context",
        Engine(),
        sink,
        bridge,
        permission=None,
        ctx=ctx,
        registry=None,
        theme=DEFAULT_THEME,
        model="echo",
        cwd=str(tmp_path),
        version="0",
        context_window=0,
        store=None,
    )

    await session._run_turn_locked("continue")

    assert "native browser state changed" in str(
        seen["llm_context_prefix"]
    )


class _ReadImageArgs:
    def __init__(self, sources: list[str], question: str, model: str = "") -> None:
        self.sources = sources
        self.question = question
        self.model = model


class _ReadImageTool:
    Args = _ReadImageArgs

    def __init__(self) -> None:
        self.calls: list[_ReadImageArgs] = []

    async def run(self, args: _ReadImageArgs, _ctx) -> ToolResult:
        self.calls.append(args)
        return ToolResult.ok("visible text: BUILD FAILED")


class _Tools:
    def __init__(self, tool: _ReadImageTool | None = None) -> None:
        self.tool = tool

    def get(self, name: str):
        return self.tool if name == "read_image" else None


@pytest.mark.asyncio
async def test_non_vision_web_session_describes_images_before_engine() -> None:
    session = object.__new__(WebSession)
    session.model = "openrouter/deepseek-v4-flash"
    session.cwd = "/tmp"
    session.session_id = "s1"
    session.permission = object()
    tool = _ReadImageTool()
    session.app_ctx = type(
        "App",
        (),
        {
            "tools": _Tools(tool),
            "events": None,
            "secrets": object(),
        },
    )()
    session.sink = _Sink()
    session._uid = 0
    blocks: list[dict] = []
    session.bridge = type(
        "Bridge",
        (),
        {"append_block": lambda _self, block, _id: blocks.append(block)},
    )()

    prompt, attachments, card = await session._prepare_image_input(
        "what happened?",
        (ImageBlock(media_type="image/png", data="QUJD"),),
    )

    assert attachments == ()
    assert "Image compatibility note" in prompt
    assert "visible text: BUILD FAILED" in prompt
    assert tool.calls and tool.calls[0].sources
    assert blocks and blocks[-1]["kind"] == "collapsible"
    assert "Read 1 image" in blocks[-1]["title_html"]
    assert card == {
        "title": "Read 1 image",
        "body": "visible text: BUILD FAILED",
        "is_error": False,
    }


@pytest.mark.asyncio
async def test_vision_web_session_keeps_image_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object.__new__(WebSession)
    session.model = "any-model"
    monkeypatch.setattr(session, "_model_supports_vision", lambda: True)
    img = ImageBlock(media_type="image/png", data="QUJD")

    prompt, attachments, card = await session._prepare_image_input("look", (img,))

    assert prompt == "look"
    assert attachments == (img,)
    assert card is None


def test_replayed_user_image_is_restored_as_attachment() -> None:
    from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block
    from autoverse.types.messages import Message, TextBlock

    blocks = render_history_blocks(
        [
            Message(
                role="user",
                content=(
                    TextBlock("see this"),
                    ImageBlock(media_type="image/png", data="QUJD"),
                ),
            )
        ],
        RenderContext(width=90),
    )
    out = transcript_block_to_block(blocks[0], RenderContext(width=90), DEFAULT_THEME)

    assert out["kind"] == "user"
    assert out["text"] == "see this"
    assert out["attachments"] == [
        {"media_type": "image/png", "data": "QUJD", "source_type": "base64"}
    ]


def test_replayed_user_turn_keeps_rewind_action_anchor() -> None:
    from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block
    from autoverse.types.messages import Message, TextBlock

    blocks = render_history_blocks(
        [
            Message(
                role="user",
                content=(TextBlock("wrong instruction"),),
                metadata={"rewind_id": "turn-123", "snapshot_hash": "tree-456"},
            )
        ],
        RenderContext(width=90),
    )
    out = transcript_block_to_block(blocks[0], RenderContext(width=90), DEFAULT_THEME)

    assert out["rewind_id"] == "turn-123"


def test_replayed_inline_tool_line_carries_tool_class() -> None:
    """A replayed single-line tool card (``css_class="tool-line"``) maps to a
    ``line`` block with ``cls: inline tool`` so the frontend's turn fold can
    count inline tools (web_search / read / grep / …) as tool calls."""
    from autoverse.frontends.repl.renderer import RenderContext, TranscriptBlock
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block

    out = transcript_block_to_block(
        TranscriptBlock(
            "ansi", '◈ Web Search "DeepSeek 更新" (8 results)', css_class="tool-line"
        ),
        RenderContext(width=90),
        DEFAULT_THEME,
    )

    assert out["kind"] == "line"
    assert out["cls"] == "inline tool"


def test_replayed_plain_line_stays_inline_class() -> None:
    """Non-tool replay lines keep the existing ``_line_class`` mapping — the new
    ``tool-line`` marker must not leak into ordinary lines."""
    from autoverse.frontends.repl.renderer import RenderContext, TranscriptBlock
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block

    out = transcript_block_to_block(
        TranscriptBlock("ansi", "some ordinary line", css_class=""),
        RenderContext(width=90),
        DEFAULT_THEME,
    )

    assert out["kind"] == "line"
    assert out["cls"] == "inline"


def test_replayed_user_url_image_keeps_url_source() -> None:
    from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block
    from autoverse.types.messages import Message, TextBlock

    blocks = render_history_blocks(
        [
            Message(
                role="user",
                content=(
                    TextBlock("see this"),
                    ImageBlock(
                        media_type="image/png",
                        data="https://example.test/image.png",
                        source_type="url",
                    ),
                ),
            )
        ],
        RenderContext(width=90),
    )
    out = transcript_block_to_block(blocks[0], RenderContext(width=90), DEFAULT_THEME)

    assert out["attachments"] == [
        {
            "media_type": "image/png",
            "data": "https://example.test/image.png",
            "source_type": "url",
        }
    ]


def test_replayed_subagent_keeps_semantic_role_for_theme() -> None:
    from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block
    from autoverse.types.messages import Message, ToolResultBlock, ToolUseBlock

    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=90)
    blocks = render_history_blocks(
        [
            Message(
                role="assistant",
                content=(
                    ToolUseBlock(
                        "agent-1",
                        "agent",
                        {"task": "locate TurnLoop", "subagent_type": "scout"},
                    ),
                ),
            ),
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_use_id="agent-1",
                        content="Found TurnLoop.",
                        metadata={
                            "subagent_type": "scout",
                            "description": "locate TurnLoop",
                            "tool_count": 3,
                        },
                    ),
                ),
            ),
        ],
        ctx,
    )
    out = transcript_block_to_block(
        next(block for block in blocks if block.kind == "agent"), ctx, DEFAULT_THEME
    )

    assert out["role"] == "scout"
    assert out["head_text"].startswith("│ Scout Task")
    assert out["foot_text"] == "└ 3 toolcalls"
    assert "\x1b" not in out["head_text"]
    assert "\x1b" not in out["foot_text"]
    assert out["failed"] is False


def test_replayed_tool_image_is_restored_for_webui() -> None:
    from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block
    from autoverse.types.messages import Message, ToolResultBlock, ToolUseBlock

    blocks = render_history_blocks(
        [
            Message(
                role="assistant",
                content=(ToolUseBlock("t1", "browser_screenshot", {}),),
            ),
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_use_id="t1",
                        content="Captured screenshot.",
                        metadata={
                            "images": ["QUJD"],
                            "image_media_type": "image/jpeg",
                        },
                    ),
                ),
            ),
        ],
        RenderContext(width=90),
    )

    assert [b.kind for b in blocks][-1] == "tool_images"
    out = transcript_block_to_block(blocks[-1], RenderContext(width=90), DEFAULT_THEME)

    assert out == {
        "kind": "tool_images",
        "images": [
            {
                "media_type": "image/jpeg",
                "data": "QUJD",
                "label": "browser_screenshot screenshot",
            }
        ],
    }


def test_webui_static_assets_render_tool_images() -> None:
    root = Path(__file__).resolve().parents[3]
    app_js = (root / "src/autoverse/frontends/web/static/app.js").read_text()
    index_html = (root / "src/autoverse/frontends/web/static/index.html").read_text()

    assert 'k === "tool_images"' in app_js
    assert "openImageLightbox(src, label)" in app_js
    assert ".tool-images img" in index_html


def test_webui_subagent_text_has_client_ansi_guard() -> None:
    root = Path(__file__).resolve().parents[3]
    app_js = (root / "src/autoverse/frontends/web/static/app.js").read_text()

    assert "const stripAnsiText" in app_js
    assert "head.textContent = stripAnsiText(b.head_text)" in app_js
    assert "foot.textContent = stripAnsiText(b.foot_text)" in app_js


def test_webui_context_panel_never_presents_missing_history_as_zero() -> None:
    root = Path(__file__).resolve().parents[3]
    app_js = (root / "src/autoverse/frontends/web/static/app.js").read_text()

    assert 'const usageAvailable = s.usage_available !== false;' in app_js
    assert '" historical usage unavailable"' in app_js
    assert '" tokens in last turn"' in app_js
    assert 'usageKind !== "turn_total"' in app_js
    assert 'const costAvailable = usageAvailable && s.cost_available !== false;' in app_js


def test_replayed_read_image_card_is_restored_from_user_metadata() -> None:
    from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.session import transcript_block_to_block
    from autoverse.types.messages import Message, TextBlock

    blocks = render_history_blocks(
        [
            Message(
                role="user",
                content=(
                    TextBlock("what is in this image?"),
                    ImageBlock(media_type="image/png", data="QUJD"),
                ),
                metadata={
                    "image_read_card": {
                        "title": "Read 1 image",
                        "body": "visible text: BUILD FAILED",
                        "is_error": False,
                    }
                },
            )
        ],
        RenderContext(width=90),
    )
    out = transcript_block_to_block(blocks[1], RenderContext(width=90), DEFAULT_THEME)

    assert out["kind"] == "collapsible"
    assert out["title_html"] == "◉ Read 1 image"
    assert out["body_html"] == "visible text: BUILD FAILED"
    assert out["card_cls"] == "tool-block"
    assert out["collapsed"] is True


def test_live_turn_footer_carries_resume_timing_metadata() -> None:
    session = object.__new__(WebSession)
    session.model = "echo"
    session._turn_started_at_ms = 1_234_000.0
    session.sink = type(
        "Sink",
        (),
        {
            "usage_seq": 0,
            "last_input": 0,
            "last_output": 0,
            "cum_cache_read": 0,
            "cum_cache_write": 0,
            "cum_prompt": 0,
            "cost_usd": 0.0,
        },
    )()

    block = session._turn_footer_block(2_500.0, (0, 0.0, 0, 0, 0), live=True)

    assert block == {
        "kind": "turn_meta",
        "text": "echo · 2s",
        "live": True,
        "elapsed_ms": 2_500.0,
        "started_at_ms": 1_234_000.0,
        "timer_paused": False,
    }


def test_approval_wait_pauses_web_turn_timers(monkeypatch) -> None:
    from autoverse.frontends.web import session as web_session

    now = [10.0]
    monkeypatch.setattr(web_session.time, "monotonic", lambda: now[0])

    session = object.__new__(WebSession)
    session._running = True
    session._turn_started_at_ms = 1_000.0
    session._turn_started_monotonic = 10.0
    session._turn_paused_elapsed_ms = 0.0
    session._turn_pause_started_monotonic = None
    session._activity_label = "Bash: pytest"
    session._activity_started_at_ms = 1_000.0
    session._activity_started_monotonic = 10.0
    session._activity_paused_elapsed_ms = 0.0
    session._activity_pause_started_monotonic = None
    session._approval_waits = set()
    session._interaction = 1
    session._queue = []
    published: list[dict] = []
    session.bridge = type(
        "Bridge", (), {"publish_status": lambda _self, status: published.append(status)}
    )()

    now[0] = 12.0
    session._pause_timers_for_approval("c1")
    assert published[-1]["timer_paused"] is True
    assert published[-1]["elapsed_ms"] == 2_000.0
    assert published[-1]["activity_elapsed_ms"] == 2_000.0

    now[0] = 17.0
    session._publish_status()
    assert published[-1]["elapsed_ms"] == 2_000.0
    assert published[-1]["activity_elapsed_ms"] == 2_000.0

    session._resume_timers_for_approval("c1")
    assert published[-1]["timer_paused"] is False
    assert published[-1]["elapsed_ms"] == 2_000.0
    assert published[-1]["activity_elapsed_ms"] == 2_000.0

    now[0] = 19.0
    session._publish_status()
    assert published[-1]["elapsed_ms"] == 4_000.0
    assert published[-1]["activity_elapsed_ms"] == 4_000.0


@pytest.mark.asyncio
async def test_list_sessions_overlays_live_web_running_state() -> None:
    manager = _manager(
        SessionSummary(session_id="s1", title="Work", status="idle"),
        _LiveSession(True, "Bash: pytest"),
    )

    [row] = await manager.list_sessions()

    assert row["status"] == "running"
    assert row["glyph"] == "●"
    assert row["tag"] == "(running)"
    assert row["current_activity"] == "Bash: pytest"


@pytest.mark.asyncio
async def test_list_sessions_uses_query_pinned_projection() -> None:
    manager = _manager(
        SessionSummary(session_id="s1", status="idle", pinned=True),
        _LiveSession(False),
    )

    [row] = await manager.list_sessions()

    assert row["pinned"] is True


def test_web_tool_label_shows_worktree_exit_action() -> None:
    """Web status labels should expose ExitWorktreeMode's keep/merge/discard action."""
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.tui_sink import TuiEventSink

    class _Log:
        def write(self, _renderable: object) -> None:
            pass

    session = object.__new__(WebSession)
    session.sink = TuiEventSink(_Log(), RenderContext())  # type: ignore[arg-type]

    label = WebSession._tool_label(
        session, "ExitWorktreeMode", {"action": "discard"}
    )

    assert label == "Exit worktree: discard"


@pytest.mark.asyncio
async def test_live_running_does_not_override_higher_priority_input_state() -> None:
    manager = _manager(
        SessionSummary(session_id="s1", status="awaiting_input"),
        _LiveSession(True),
    )

    [row] = await manager.list_sessions()

    assert row["status"] == "awaiting_input"
    assert row["glyph"] == "◆"


@pytest.mark.asyncio
async def test_live_user_input_request_overrides_running_state() -> None:
    manager = _manager(
        SessionSummary(session_id="s1", status="idle"),
        _LiveSession(True, awaiting=True),
    )

    [row] = await manager.list_sessions()

    assert row["status"] == "awaiting_input"
    assert row["glyph"] == "◆"


@pytest.mark.asyncio
async def test_web_switch_mode_passes_worktree_exit_action(tmp_path) -> None:
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry
    from tests._doubles import FakeIsolator, MemStore

    iso = FakeIsolator(cwd=tmp_path / ".autoverse" / "worktrees" / "fake")
    app = AppContext(
        config=Config(),
        store=MemStore(),
        cwd=tmp_path,
        model="echo",
        events=type("Sink", (), {"emit": lambda *_args, **_kwargs: None})(),
    )
    app.extra["session_id"] = "web-wt"
    app.isolator = iso
    app.worktrees = InMemoryWorktreeRegistry()

    session = object.__new__(WebSession)
    session.app_ctx = app
    session.cwd = str(tmp_path)
    session._uid = 0
    blocks: list[dict] = []
    session.bridge = type(
        "Bridge",
        (),
        {"append_block": lambda _self, block, _id: blocks.append(block)},
    )()
    session._publish_state = lambda: None  # type: ignore[method-assign]

    entered = await session.switch_mode("worktree")
    assert entered["mode"] == "worktree"

    exited = await session.switch_mode("normal", exit_action="merge")

    assert exited["ok"] is True
    assert exited["mode"] == "normal"
    assert app.cwd == tmp_path
    assert iso.calls.count("finalize") == 1
    assert app.worktrees.active_manual("web-wt") is None
    assert exited["worktree_exit"]["action"] == "merge"
    assert "merged" in exited["worktree_exit"]["message"].lower()
    assert blocks and "Worktree merge" in blocks[-1]["html"]


@pytest.mark.asyncio
async def test_web_switch_mode_auto_exits_clean_worktree_without_prompt(tmp_path) -> None:
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry
    from tests._doubles import FakeIsolator, MemStore

    iso = FakeIsolator(cwd=tmp_path / ".autoverse" / "worktrees" / "clean", dirty=False)
    app = AppContext(
        config=Config(),
        store=MemStore(),
        cwd=tmp_path,
        model="echo",
        events=type("Sink", (), {"emit": lambda *_args, **_kwargs: None})(),
    )
    app.extra["session_id"] = "web-clean-wt"
    app.isolator = iso
    app.worktrees = InMemoryWorktreeRegistry()

    session = object.__new__(WebSession)
    session.app_ctx = app
    session.cwd = str(tmp_path)
    session._uid = 0
    blocks: list[dict] = []
    session.bridge = type(
        "Bridge",
        (),
        {"append_block": lambda _self, block, _id: blocks.append(block)},
    )()
    session._publish_state = lambda: None  # type: ignore[method-assign]

    entered = await session.switch_mode("worktree")
    assert entered["mode"] == "worktree"

    exited = await session.switch_mode("normal", exit_action="auto")

    assert exited["ok"] is True
    assert exited["mode"] == "normal"
    assert exited["worktree_exit"] is None
    assert "has_changes" in iso.calls
    assert "finalize" in iso.calls
    assert app.worktrees.active_manual("web-clean-wt") is None
    assert blocks == []


@pytest.mark.asyncio
async def test_web_switch_mode_auto_prompts_for_dirty_worktree(tmp_path) -> None:
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry
    from tests._doubles import FakeIsolator, MemStore

    iso = FakeIsolator(cwd=tmp_path / ".autoverse" / "worktrees" / "dirty", dirty=True)
    app = AppContext(
        config=Config(),
        store=MemStore(),
        cwd=tmp_path,
        model="echo",
        events=type("Sink", (), {"emit": lambda *_args, **_kwargs: None})(),
    )
    app.extra["session_id"] = "web-dirty-wt"
    app.isolator = iso
    app.worktrees = InMemoryWorktreeRegistry()

    session = object.__new__(WebSession)
    session.app_ctx = app
    session.cwd = str(tmp_path)
    session._uid = 0
    session.bridge = type(
        "Bridge",
        (),
        {"append_block": lambda *_args, **_kwargs: None},
    )()
    session._publish_state = lambda: None  # type: ignore[method-assign]

    entered = await session.switch_mode("worktree")
    assert entered["mode"] == "worktree"

    out = await session.switch_mode("normal", exit_action="auto")

    assert out["mode"] == "worktree"
    assert out["needs_worktree_exit_action"] is True
    assert out["has_changes"] is True
    assert "has_changes" in iso.calls
    assert "finalize" not in iso.calls


@pytest.mark.asyncio
async def test_web_switch_mode_rejects_invalid_worktree_exit_action(tmp_path) -> None:
    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry
    from tests._doubles import FakeIsolator, MemStore

    iso = FakeIsolator(cwd=tmp_path / ".autoverse" / "worktrees" / "invalid")
    app = AppContext(
        config=Config(),
        store=MemStore(),
        cwd=tmp_path,
        model="echo",
        events=type("Sink", (), {"emit": lambda *_args, **_kwargs: None})(),
    )
    app.extra["session_id"] = "web-invalid-wt"
    app.isolator = iso
    app.worktrees = InMemoryWorktreeRegistry()

    session = object.__new__(WebSession)
    session.app_ctx = app
    session.cwd = str(tmp_path)
    session._uid = 0
    session.bridge = type(
        "Bridge",
        (),
        {"append_block": lambda *_args, **_kwargs: None},
    )()
    session._publish_state = lambda: None  # type: ignore[method-assign]

    entered = await session.switch_mode("worktree")
    assert entered["mode"] == "worktree"
    iso.calls.clear()

    out = await session.switch_mode("normal", exit_action="wat")

    assert out["ok"] is False
    assert out["mode"] == "worktree"
    assert "action" in out["error"]
    assert worktree_mode.is_active(app) is True
    assert "finalize" not in iso.calls


@pytest.mark.asyncio
async def test_resume_cwd_uses_persisted_session_cwd(tmp_path) -> None:
    from autoverse.ports import SessionMeta

    project = tmp_path / "project"
    project.mkdir()

    class Store:
        async def load_meta(self, sid: str) -> SessionMeta:
            return SessionMeta(session_id=sid, cwd=str(project))

    manager = object.__new__(SessionManager)
    manager.cwd = tmp_path
    manager.store = Store()

    assert await manager._resume_cwd("s1") == project.resolve()


# ── mark_viewed uses max(existing, now) to avoid clobbering a later stamp ───
async def test_mark_viewed_uses_max_not_clobber(tmp_path) -> None:
    """``mark_viewed`` must not overwrite a *later* ``last_viewed_at`` from the
    other frontend with an *earlier* timestamp — it takes ``max(existing, now)``."""
    import time as _time

    from autoverse.frontends.web.bridge import SessionBridge
    from autoverse.frontends.web.session import WebSession
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.repl.tui_sink import TuiEventSink
    from autoverse.frontends.web.web_log import WebLog
    from autoverse.infra.persist.jsonl_store import JSONLStore
    from autoverse.ports import SessionMeta

    store = JSONLStore(tmp_path)
    sid = "test-viewed"
    await store.create(sid, SessionMeta(session_id=sid))

    # Simulate: the TUI frontend already stamped last_viewed_at = future_time
    future = _time.time() + 100
    await store.update_meta(sid, last_viewed_at=future)

    bridge = SessionBridge()
    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=80)
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), ctx)

    session = WebSession(
        sid, engine=None, sink=sink, bridge=bridge, permission=None, ctx=ctx,
        registry=None, theme=DEFAULT_THEME, model="echo", cwd=str(tmp_path),
        version="0", context_window=0, store=store,
    )

    await session.mark_viewed()

    meta = await store.load_meta(sid)
    viewed = float(meta.extra.get("last_viewed_at", 0.0))
    # The future stamp must survive — mark_viewed took max(existing, now)
    assert viewed >= future


async def test_mark_viewed_skips_meta_rewrite_when_no_activity_is_unseen() -> None:
    from autoverse.ports import SessionMeta

    class Store:
        def __init__(self) -> None:
            self.meta = SessionMeta(
                session_id="seen",
                extra={"last_activity_at": 100.0, "last_viewed_at": 120.0},
            )
            self.updates = 0

        async def load_meta(self, _sid: str) -> SessionMeta:
            return self.meta

        async def update_meta(self, _sid: str, **_fields: object) -> None:
            self.updates += 1

    store = Store()
    session = object.__new__(WebSession)
    session.store = store
    session.session_id = "seen"

    await session.mark_viewed()

    assert store.updates == 0


@pytest.mark.asyncio
async def test_resolve_plan_approve_echoes_short_label_not_full_plan(tmp_path) -> None:
    """After plan approval the chat already shows the plan card, so the user-echo
    for the continuation turn must NOT re-render the full plan body — otherwise the
    plan content appears twice in the WebUI."""
    from autoverse.frontends.web.bridge import SessionBridge
    from autoverse.frontends.web.session import WebSession
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.repl.tui_sink import TuiEventSink
    from autoverse.frontends.web.web_log import WebLog

    plan_body = "## Plan\n\n1. Do the first distinctive thing\n2. Do the second one"

    bridge = SessionBridge()
    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=80)
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), ctx)
    sid = "plan-echo"
    session = WebSession(
        sid, engine=None, sink=sink, bridge=bridge, permission=None, ctx=ctx,
        registry=None, theme=DEFAULT_THEME, model="echo", cwd=str(tmp_path),
        version="0", context_window=0, store=None,
    )
    session.app_ctx = type("Ctx", (), {})()
    session.app_ctx.extra = {
        "plan_mode": {
            "active": False, "pending_approval": True, "plan": plan_body,
        }
    }

    captured: dict[str, object] = {}

    async def _fake_run_turn(prompt, *, echo=None):
        captured["prompt"] = prompt
        captured["echo"] = echo

    session.run_turn = _fake_run_turn  # type: ignore[method-assign]

    await session.resolve_plan("approve")

    # The engine still receives the full approved-plan prompt …
    assert "first distinctive thing" in str(captured["prompt"])
    # … but the user-facing echo is a short label, not the plan body.
    assert captured["echo"] == "✓ Plan approved — executing"
    assert "first distinctive thing" not in str(captured["echo"])
