"""Unit: Textual TUI shell — TuiEventSink + AutoVerseApp pilot smoke test.

These tests are skipped when the optional ``textual`` dependency is not
installed (``pip install autoverse[tui]``). They verify:

* ``TuiEventSink`` renders events via the shared registry and writes them to a
  RichLog (reusing the framework, not re-implementing rendering).
* ``AutoVerseApp`` mounts, builds an Echo-backed engine, runs one turn driven by
  typed input, and writes the model output into the RichLog — all headless via
  Textual's ``run_test()`` pilot (no real TTY).
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from pathlib import Path  # noqa: E402

from autoverse.app.builder import EngineBuilder  # noqa: E402
from autoverse.app.options import EngineOptions  # noqa: E402
from autoverse.frontends.repl.app import AutoVerseApp  # noqa: E402
from autoverse.frontends.repl.builtin_cmds import build_default_registry  # noqa: E402
from autoverse.frontends.repl.renderer import RenderContext  # noqa: E402
from autoverse.frontends.repl.tui_sink import TuiEventSink  # noqa: E402
from autoverse.types import make_event  # noqa: E402
from autoverse.types.events import (  # noqa: E402
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    TurnStartPayload,
)


def test_tui_turn_elapsed_excludes_permission_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoverse.frontends.repl.app as repl_app

    now = [10.0]
    monkeypatch.setattr(repl_app.time, "monotonic", lambda: now[0])
    app = AutoVerseApp.__new__(AutoVerseApp)
    app._turn_started_at = 10.0
    app._turn_paused_elapsed = 0.0
    app._turn_pause_started_at = 0.0
    app._phase_paused_elapsed = 0.0
    app._phase_pause_started_at = 0.0

    now[0] = 12.0
    assert app._turn_elapsed_ms() == 2_000.0

    app._pause_turn_timer()
    now[0] = 17.0
    assert app._turn_elapsed_ms() == 2_000.0

    app._resume_turn_timer()
    assert app._turn_elapsed_ms() == 2_000.0

    now[0] = 19.0
    assert app._turn_elapsed_ms() == 4_000.0


class _FakeRichLog:
    """Minimal stand-in capturing what would be written to a RichLog."""

    def __init__(self) -> None:
        self.writes: list[object] = []

    def write(self, renderable: object) -> None:
        self.writes.append(renderable)


def _text_of(w: object) -> str:
    """Extract the source text of a write, whether str / Rich Text / Markdown."""
    markup = getattr(w, "markup", None)  # rich.markdown.Markdown carries .markup
    if isinstance(markup, str):
        return markup
    return str(w)


def _log_text(app: object) -> str:
    """Concatenate the visible text of every block in the ConversationLog.

    The TUI output region is a widget-per-block ``ConversationLog`` (not a flat
    ``RichLog``), so we walk its mounted ``Static``/``Markdown`` children and pull
    their plain text — the headless equivalent of "what the user would see".
    """
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


def _usage_event():
    from autoverse.types.events import UsagePayload
    from autoverse.types.messages import Usage

    return make_event(UsagePayload(usage=Usage(input_tokens=5, output_tokens=63), model="echo"))


async def test_sink_marks_single_line_tool_end_for_web_folding() -> None:
    """A one-line inline tool (web_search) must be written with the ``tool-line``
    class so the WebLog emits ``cls: inline tool`` — without it the web turn
    fold's tool-call count is 0 and the whole turn never collapses (regression:
    inline tools were invisible to the turn-history fold)."""
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.web.bridge import SessionBridge
    from autoverse.frontends.web.web_log import WebLog

    bridge = SessionBridge()
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), RenderContext(width=80))
    await sink.emit(make_event(ToolEndPayload(
        "t1", "web_search", "…",
        metadata={"query": "DeepSeek 更新", "count": 8},
    )))

    lines = [b for b in bridge.blocks if b["block"]["kind"] == "line"]
    assert lines, "the inline tool must render as a line block"
    assert lines[0]["block"]["cls"] == "inline tool"


async def test_sink_renders_events_to_log() -> None:
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    # Streaming text is buffered; it flushes at the next boundary event.
    await sink.emit(make_event(TextDeltaPayload(text="hello")))
    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN")))
    assert log.writes, "sink should write at least one renderable"
    rendered = " ".join(_text_of(w) for w in log.writes)
    assert "hello" in rendered          # buffered prose flushed


async def test_sink_usage_is_data_only() -> None:
    """USAGE is collected (tokens/cost) but NOT drawn as a bare line — the app
    composes a richer turn footer (model · duration · tokens · cost)."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(_usage_event())
    assert sink.last_input == 5 and sink.last_output == 63   # collected
    assert [w for w in log.writes if str(w).strip()] == []   # nothing rendered


async def test_sink_skips_trace_only_events() -> None:
    """REQUEST / GATE_DECISION / TURN_START / TURN_END are implementation
    detail and must never be committed to the log (the duplicate-marker /
    'gate completion' noise the user reported)."""
    from autoverse.types.events import GatePayload, RequestPayload

    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(TurnStartPayload(turn_index=0, iteration=0, model="echo")))
    await sink.emit(make_event(RequestPayload(
        turn_index=0, system="", system_sha256="", tool_names=(), tools_sha256="",
        model="echo", max_tokens=8192, temperature=0.0, thinking_budget=0)))
    await sink.emit(make_event(GatePayload(gate_name="completion", decision="stop")))
    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN")))
    assert log.writes == []  # none of these produce a visible line


async def test_sink_compacting_renders_divider_not_bare_line() -> None:
    """COMPACTING must render as a calm boundary divider (Rich Rule with a
    user-facing label), NOT the old bare 'compacting [overflow] N→M tok' log
    line. Internal kind + token counts are not surfaced (MiMo parity)."""
    from rich.rule import Rule

    from autoverse.types.events import CompactingPayload

    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(CompactingPayload(
        kind="overflow", tokens_before=7366, tokens_after=1347,
        messages_before=60, messages_after=5,
    )))
    rules = [w for w in log.writes if isinstance(w, Rule)]
    assert len(rules) == 1, "compaction should mount exactly one divider"
    title = str(getattr(rules[0], "title", ""))
    assert "Context summarised" in title          # human label for 'overflow'
    assert "overflow" not in title                 # internal kind not surfaced
    assert "7366" not in title and "→" not in title  # no token math
    # and the old bare line must NOT appear anywhere.
    assert all("compacting [" not in str(w) for w in log.writes)


async def test_sink_compacting_keeps_history_above() -> None:
    """The divider is additive — prior prose is flushed and stays; compaction
    does not clear earlier blocks (history is preserved, not wiped)."""
    from rich.rule import Rule

    from autoverse.types.events import CompactingPayload

    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(TextDeltaPayload(text="earlier answer")))
    await sink.emit(make_event(CompactingPayload(kind="micro")))
    rendered = " ".join(_text_of(w) for w in log.writes)
    assert "earlier answer" in rendered            # prior prose flushed + kept
    assert any(isinstance(w, Rule) for w in log.writes)  # divider added after


async def test_sink_buffers_text_until_boundary() -> None:
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    # Pure text deltas accumulate and are not written until a boundary event.
    await sink.emit(make_event(TextDeltaPayload(text="ab")))
    await sink.emit(make_event(TextDeltaPayload(text="cd")))
    assert log.writes == []  # still buffered
    # A trace-only TURN_END still flushes buffered prose (just no marker line).
    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")))
    rendered = " ".join(_text_of(w) for w in log.writes)
    assert "abcd" in rendered  # flushed as one paragraph
    assert "END_TURN" not in rendered  # no mechanical marker


class _FakeStreamingLog(_FakeRichLog):
    """A log that supports the streaming seam (like the real ConversationLog).

    Records every begin/update/end so a test can assert the answer grows in
    place (one widget, updated per delta) rather than buffered-then-flushed."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_calls: list[tuple[str, str]] = []  # (op, text-snapshot)
        self.open = False

    def begin_stream(self, text: str = "") -> None:
        self.open = True
        self.stream_calls.append(("begin", text))

    def update_stream(self, text: str) -> None:
        self.stream_calls.append(("update", text))

    def end_stream(self) -> None:
        self.open = False
        self.stream_calls.append(("end", ""))


async def test_sink_streams_answer_live_when_supported() -> None:
    """When the log exposes begin/update/end_stream, assistant TEXT_DELTAs grow
    an in-place widget token-by-token (MiMo parity) instead of being buffered
    until the boundary — and the boundary commits it exactly once."""
    log = _FakeStreamingLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(TextDeltaPayload(text="Hel")))
    await sink.emit(make_event(TextDeltaPayload(text="lo ")))
    await sink.emit(make_event(TextDeltaPayload(text="world")))
    # Streamed live: begin on the first delta, update on each subsequent one,
    # carrying the full accumulated snapshot — NOT buffered silently.
    ops = [op for op, _ in log.stream_calls]
    assert ops == ["begin", "update", "update"]
    assert log.stream_calls[0][1] == "Hel"
    assert log.stream_calls[-1][1] == "Hello world"
    assert log.open is True  # still in flight before the boundary
    # Boundary commits the live widget once (no second mount via write()).
    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN")))
    assert log.stream_calls[-1] == ("end", "")
    assert log.open is False
    # The answer was rendered into the streaming widget, not re-mounted as a
    # separate Markdown via the plain write() path.
    assert log.writes == []


async def test_sink_thinking_is_buffered_separately() -> None:
    """Regression: thinking deltas must also coalesce into one paragraph and
    flush as a separate block when the stream switches thinking→text (the
    one-word-per-line bug)."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(ThinkingDeltaPayload(text="I will ")))
    await sink.emit(make_event(ThinkingDeltaPayload(text="reason.")))
    assert log.writes == []  # thinking buffered, not per-chunk
    # Switching to text is a boundary → thinking flushes as ONE content write.
    await sink.emit(make_event(TextDeltaPayload(text="Answer ")))
    # USAGE is a boundary that flushes the buffered answer, but is data-only in
    # the sink (no usage line — the app composes the turn footer separately).
    await sink.emit(_usage_event())
    # Non-blank writes are the content lines (blank lines are BLOCK_GAP rhythm).
    content = [_text_of(w) for w in log.writes if _text_of(w).strip()]
    # thinking and answer are two distinct content blocks (not merged); USAGE
    # adds no third block (see test_sink_usage_is_data_only).
    assert len(content) == 2
    assert "I will reason." in content[0]
    assert "Answer" in content[1]
    # Vertical rhythm is now owned by the ConversationLog's per-block CSS
    # margins (one blank row between widgets) — the sink no longer writes bare
    # blank-line separators (that would double the gap), so we assert it does
    # NOT emit empty writes between blocks.
    assert all(_text_of(w).strip() != "" for w in log.writes)


async def test_conversation_log_coalesces_stream_updates() -> None:
    """Smoothness regression: a burst of ``update_stream`` deltas must NOT each
    trigger the expensive ``Markdown.update`` (full re-parse). They are
    coalesced via a timer to at most one render per flush interval, and
    ``end_stream`` always renders the final accumulated snapshot so the
    committed widget is complete."""
    from textual.app import App, ComposeResult

    from autoverse.frontends.repl.conversation_log import ConversationLog

    updates: list[str] = []

    class _CountingLog(ConversationLog):  # type: ignore[misc]
        # Speed the test: flush almost immediately so a single pause coalesces
        # the whole burst into one render.
        _STREAM_FLUSH_INTERVAL = 0.01

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield _CountingLog(id="log")

    app = _Harness()
    async with app.run_test() as pilot:
        log = app.query_one("#log", _CountingLog)
        # Spy on the actual expensive call without breaking rendering.
        orig_update = log._flush_stream

        def _spy() -> None:
            if log._stream_pending is not None:
                updates.append(log._stream_pending)
            orig_update()

        log._flush_stream = _spy  # type: ignore[method-assign]

        log.begin_stream("a")  # first paint is immediate (no timer)
        # Burst of deltas within one interval → only the last should render.
        log.update_stream("ab")
        log.update_stream("abc")
        log.update_stream("abcd")
        await pilot.pause(0.05)  # let the single coalesced timer fire
        # Exactly one coalesced flush carrying the latest snapshot — not 3.
        assert updates == ["abcd"], f"expected one coalesced flush, got {updates}"
        # A final delta after the flush + end_stream must still be committed.
        log.update_stream("abcde")
        log.end_stream()  # force-flushes the pending final snapshot
        assert log._stream_md is None
        assert log._stream_timer is None
        assert log._stream_pending is None


async def test_app_runs_one_echo_turn() -> None:
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo",
            cwd=Path.cwd(),
            session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        # Wait for the engine to finish building.
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready, "engine factory should complete"  # type: ignore[attr-defined]

        # Type a prompt and submit it.
        await pilot.click("#input")
        for ch in "ping":
            await pilot.press(ch)
        await pilot.press("enter")

        # Let the worker drain the queue and run the Echo turn.
        for _ in range(60):
            await pilot.pause(0.05)
            text = _log_text(app)
            if "ping" in text or "turn" in text:
                break
        text = _log_text(app)
        assert "turn" in text or "ping" in text


async def test_app_help_command() -> None:
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "/help":
            await pilot.press(ch)
        await pilot.press("enter")
        # /help opens a scrollable TextScreen overlay with the command list.
        from textual.widgets import Static

        from autoverse.frontends.repl.dialogs.text_screen import TextScreen

        text = ""
        for _ in range(40):
            await pilot.pause(0.05)
            screen = app.screen  # type: ignore[attr-defined]
            if isinstance(screen, TextScreen):
                body = screen.query_one("#text-body", Static)
                v = getattr(body, "visual", None)
                text = getattr(v, "plain", "") or ""
                if "help" in text.lower() or "model" in text.lower():
                    break
        assert "model" in text.lower() or "help" in text.lower()


async def test_app_echoes_user_input() -> None:
    """The user's message must be echoed to the log as a ``user-echo`` card
    (MiMo parity: a CSS left accent bar + grey block, not a ``▌`` glyph) so the
    conversation shows what was asked — the missing-echo bug."""
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=Path.cwd(), session_mode="interactive",
                             event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "marco":
            await pilot.press(ch)
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.05)
            text = _log_text(app)
            if "marco" in text:
                break
        text = _log_text(app)
        assert "marco" in text          # user input echoed
        # The accent gutter is now a CSS left bar on a ``user-echo`` block,
        # not a baked-in ▌ glyph — assert the styled card exists.
        from autoverse.frontends.repl.conversation_log import ConversationLog

        log = app.query_one("#log", ConversationLog)
        assert log.query(".user-echo"), "user echo must be a styled .user-echo card"
        assert "turn 0" not in text     # no mechanical turn marker
        assert "max_tokens" not in text  # no request/trace noise


async def test_input_frame_click_focuses_input() -> None:
    """Clicking the prompt frame around the text box should focus the input.

    The visible prompt is taller than the actual one-line ``CommandInput``: it
    includes padding and the meta footer. Those rows must be part of the hit
    target so mouse users do not have to click the exact text row.
    """
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=Path.cwd(), session_mode="interactive",
                             event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready  # type: ignore[attr-defined]

        from autoverse.frontends.repl.session_rail import SessionRail
        from autoverse.frontends.repl.widgets import CommandInput

        inp = app.query_one("#input", CommandInput)
        rail = app.query_one("#session-rail", SessionRail)
        rail.focus()
        await pilot.pause(0.05)
        assert app.focused is rail

        await pilot.click("#input-meta")
        await pilot.press("z")
        assert inp.value == "z"


async def test_edit_last_queued_pulls_into_input() -> None:
    """Ctrl+E pulls the most recently queued message back into the input box to
    edit. The gesture is gated so it only fires with a non-empty queue AND an
    empty input box, never clobbering a draft."""
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=Path.cwd(), session_mode="interactive",
                             event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        from autoverse.frontends.repl.widgets import CommandInput

        inp = app.query_one("#input", CommandInput)

        # Empty queue → gesture disabled (key falls through to the input box).
        assert app.check_action("edit_last_queued", ()) is False

        # Seed the queue directly — the gesture needs no engine turn.
        app._queue.enqueue("first task", priority="next")   # type: ignore[attr-defined]
        app._queue.enqueue("second task", priority="next")  # type: ignore[attr-defined]
        app._refresh_queue_panel()                           # type: ignore[attr-defined]

        # Non-empty queue + empty input → enabled.
        assert app.check_action("edit_last_queued", ()) is True
        # A half-typed draft disables it so it is never overwritten.
        inp.value = "draft"
        assert app.check_action("edit_last_queued", ()) is False
        inp.value = ""

        # Pull the most recent queued message back into the box for editing.
        app.action_edit_last_queued()                        # type: ignore[attr-defined]
        await pilot.pause(0.05)
        assert inp.value == "second task"
        assert inp.cursor_position == len("second task")
        # Only the pulled item leaves the queue; the rest is preserved in order.
        assert app._queue.items() == [("next", "first task")]  # type: ignore[attr-defined]


async def test_busy_command_shows_transient_status_spinner() -> None:
    """A long-running slash command surfaces an immediate status-bar spinner
    from its ``busy_label`` (the /compact "apparent freeze" fix) and clears it
    back to idle when the handler returns — acknowledging the keypress at once
    without writing to the scrollback."""
    import asyncio

    from autoverse.frontends.repl.commands import CommandResult, CommandSpec
    from autoverse.frontends.repl.widgets import StatusBar

    gate = asyncio.Event()

    async def _slow(ctx: object, args: str) -> CommandResult:
        await gate.wait()
        return CommandResult(display="")

    registry = build_default_registry()
    registry.register(
        CommandSpec(name="slowcmd", summary="test", handler=_slow,
                    busy_label="Working…")
    )

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=Path.cwd(),
                             session_mode="interactive",
                             event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        await pilot.click("#input")
        for ch in "/slowcmd":
            await pilot.press(ch)
        await pilot.press("enter")

        # While the handler is blocked the busy label is active and the status
        # bar shows it (not the idle "ready · …" hint).
        for _ in range(40):
            await pilot.pause(0.05)
            if app._cmd_busy_label == "Working…":  # type: ignore[attr-defined]
                break
        assert app._cmd_busy_label == "Working…"  # type: ignore[attr-defined]
        status = app.query_one("#status", StatusBar)
        assert status.phase == "Working…"

        # Releasing the handler clears the transient feedback back to idle.
        gate.set()
        for _ in range(40):
            await pilot.pause(0.05)
            if app._cmd_busy_label is None:  # type: ignore[attr-defined]
                break
        assert app._cmd_busy_label is None  # type: ignore[attr-defined]
        assert status.phase == "idle"


def test_compact_command_declares_busy_label() -> None:
    """The built-in /compact carries a busy_label so the TUI shows progress
    while it awaits the (potentially slow) summary LLM call."""
    registry = build_default_registry()
    spec = registry.get("compact")
    assert spec is not None
    assert spec.busy_label == "Compacting…"


def test_workflow_commands_share_lightweight_activity_headings() -> None:
    registry = build_default_registry()
    expected = {
        "commit": "Create commit",
        "review": "Review changes",
        "pr": "Open pull request",
        "init": "Initialize project",
        "coordinate": "Coordinate implementation",
    }
    for name, label in expected.items():
        spec = registry.get(name)
        assert spec is not None
        assert spec.activity_label == label
        assert spec.busy_label == f"Running /{name}…"


async def test_sink_renders_answer_as_markdown() -> None:
    """Assistant answer text is rendered as Markdown (headings/lists/code),
    while thinking stays plain muted prose."""
    from rich.markdown import Markdown

    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    md = "## Plan\n\n1. read\n2. edit\n\n```python\nx = 1\n```"
    await sink.emit(make_event(TextDeltaPayload(text=md)))
    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN")))
    mds = [w for w in log.writes if isinstance(w, Markdown)]
    assert len(mds) == 1                      # answer rendered via Markdown
    assert "## Plan" in mds[0].markup         # markdown source preserved
    assert "```python" in mds[0].markup


def test_unlabelled_markdown_fence_is_plain_text() -> None:
    """Bare fences must not guess a lexer and paint diagrams as syntax errors."""
    from autoverse.frontends.repl.markdown import AutoVerseMarkdownFence

    diagram = (
        "frontends/   →  CLI / REPL / Web / Gateway\n"
        "app/         →  DI 容器, EngineBuilder\n"
        "──────────────────────────────────────────\n"
        "ports/       →  协议接口 (LLMProvider, Store, ...)\n"
    )
    plain = AutoVerseMarkdownFence.highlight(diagram, "")
    assert [span.style for span in plain.spans] == ["$text"]
    assert "error-muted" not in repr(plain.spans)

    highlighted = AutoVerseMarkdownFence.highlight("x = 1\n", "python")
    assert len(highlighted.spans) > 1
    assert any(span.style == "$text-warning" for span in highlighted.spans)


async def test_sink_thinking_is_not_markdown() -> None:
    """Thinking is plain muted prose, never a Markdown object (reasoning is
    kept visually secondary, not parsed as structured markdown)."""
    from rich.markdown import Markdown

    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(ThinkingDeltaPayload(text="# not a heading, just reasoning")))
    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN")))
    assert not any(isinstance(w, Markdown) for w in log.writes)
    assert any("not a heading" in _text_of(w) for w in log.writes)


async def test_thinking_folds_into_collapsible() -> None:
    """Thinking renders as a collapsed 'Thought' Collapsible (MiMo parity)."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ThinkingDeltaPayload(text="some private reasoning here")))
        await s.emit(make_event(TextDeltaPayload(text="the answer")))  # boundary flushes thinking
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = app.query_one("#log", ConversationLog).query(Collapsible)
        assert len(list(cols)) >= 1
        thought = list(cols)[0]
        assert thought.collapsed is True             # folded by default
        assert "Thought" in str(thought.title)


async def test_long_bash_folds_expandable() -> None:
    """A long bash failure folds into a Collapsible whose body holds the FULL
    output (expand reveals everything)."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        long = "\n".join(f"FAIL test_{i}" for i in range(30))
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="bash", output=long, is_error=True,
            error_message="exit 1", duration_ms=2400.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True
        title = str(cols[0].title)
        assert "bash" in title
        assert "Error (" in title                       # red error cue in title
        # The body holds the full untruncated output (every FAIL line) plus the
        # terse failure reason, so expanding the card reveals everything.
        from textual.widgets import Static
        body = "\n".join(
            getattr(getattr(n, "visual", None), "plain", "") or ""
            for n in cols[0].query(Static)
        )
        assert "FAIL test_0" in body and "FAIL test_29" in body
        assert "exit 1" in body


async def test_short_tool_not_folded() -> None:
    """Short tool output (inline Read) is NOT a collapsible."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="Read", output="120 lines", duration_ms=8.0)))
        await pilot.pause(0.1)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 0


async def test_tool_start_enriches_activity_label() -> None:
    """TOOL_START sets the StatusBar activity to a target-aware label built from
    the tool INPUT (``Read src/foo.py``, ``Bash: npm test``), not the bare name."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]
    await sink.emit(make_event(ToolStartPayload(
        tool_use_id="t1", tool_name="Read",
        tool_input={"file_path": "src/foo.py"})))
    assert sink.activity == "Read src/foo.py"
    await sink.emit(make_event(ToolStartPayload(
        tool_use_id="t2", tool_name="bash",
        tool_input={"command": "npm test"})))
    assert sink.activity == "bash: npm test"
    await sink.emit(make_event(ToolStartPayload(
        tool_use_id="t3", tool_name="ExitWorktreeMode",
        tool_input={"action": "merge"})))
    assert sink.activity == "Exit worktree: merge"
    # A line-based log lacks the running-card seam → nothing is written on START.
    assert log.writes == []


async def test_exit_worktree_renderer_shows_action() -> None:
    """ExitWorktreeMode's final card must name the chosen behaviour."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]

    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="t", tool_name="ExitWorktreeMode",
        output="Exited worktree mode (merge): merged branch back to base.",
        metadata={"action": "merge"}, duration_ms=8.0)))

    rendered = " ".join(_text_of(w) for w in log.writes)
    assert "Exit worktree" in rendered
    assert "merge" in rendered
    assert "ExitWorktreeMode" not in rendered


async def test_enter_worktree_renderer_shows_worktree_path() -> None:
    """EnterWorktreeMode's final line should expose the actual isolated cwd."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]

    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="t", tool_name="EnterWorktreeMode",
        output="Entered worktree mode.",
        metadata={
            "branch": "autoverse/wt/13b6d996d5f8",
            "cwd": "/Users/alden/CCWorkSpace/worktree-demo/.autoverse/worktrees/13b6d996d5f8",
        },
        duration_ms=8.0,
    )))

    rendered = " ".join(_text_of(w) for w in log.writes)
    assert "Entered worktree mode" in rendered
    assert "worktree-demo/.autoverse/worktrees/13b6d996d5f8" in rendered
    assert "autoverse/wt/13b6d996d5f8" not in rendered


async def test_tool_activity_clears_on_end_and_reveals_concurrent_work() -> None:
    """The StatusBar activity is keyed by running call, so completion/failure
    removes only that call and falls back to any older still-running tool."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]

    await sink.emit(make_event(ToolStartPayload(
        tool_use_id="read", tool_name="Read",
        tool_input={"file_path": "src/foo.py"})))
    await sink.emit(make_event(ToolStartPayload(
        tool_use_id="bash", tool_name="bash",
        tool_input={"command": "npm test"})))
    assert sink.activity == "bash: npm test"

    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="bash", tool_name="bash", output="boom",
        is_error=True, error_message="failed", duration_ms=50.0)))
    assert sink.activity == "Read src/foo.py"

    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="read", tool_name="Read", output="ok", duration_ms=2.0)))
    assert sink.activity == ""


async def test_tool_activity_clears_when_end_id_is_missing() -> None:
    """A TOOL_END with a missing id falls back to clearing the latest same-name tool."""
    log = _FakeRichLog()
    sink = TuiEventSink(log, RenderContext())  # type: ignore[arg-type]

    await sink.emit(make_event(ToolStartPayload(
        tool_use_id="todo-1", tool_name="TodoWrite", tool_input={})))
    assert sink.activity == "TodoWrite"

    await sink.emit(make_event(ToolEndPayload(
        tool_use_id="", tool_name="TodoWrite", output="ok", duration_ms=2.0)))
    assert sink.activity == ""


async def test_status_bar_clears_stale_activity_while_turn_continues() -> None:
    """A TOOL_END can clear sink.activity while the turn remains RUNNING; the
    next tick must also clear the already-rendered StatusBar activity line."""
    import time

    from autoverse.frontends.repl.input_queue import InputMode
    from autoverse.frontends.repl.widgets import StatusBar

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)

        status = app.query_one("#status", StatusBar)
        status.activity = "↳ bash: npm test"
        app._sink.activity = ""  # type: ignore[union-attr]
        app._mode_stack.enter(InputMode.RUNNING)  # type: ignore[attr-defined]
        app._turn_started_at = time.monotonic()  # type: ignore[attr-defined]
        app._phase_started_at = app._turn_started_at  # type: ignore[attr-defined]

        app._tick()  # type: ignore[attr-defined]
        assert status.activity == ""


async def test_running_tool_card_appears_then_is_replaced() -> None:
    """A slow tool shows a transient ``tool-running`` card in the main log after
    the debounce, which is removed when its TOOL_END result card is written."""
    from textual.widgets import Static

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        from autoverse.frontends.repl.conversation_log import ConversationLog
        log = app.query_one("#log", ConversationLog)

        await s.emit(make_event(ToolStartPayload(
            tool_use_id="t", tool_name="bash",
            tool_input={"command": "sleep 1 && echo hi"})))
        # Wait past the debounce so the running card mounts.
        await pilot.pause(0.3)
        running = [n for n in log.query(Static) if "tool-running" in (n.classes or set())]
        assert len(running) == 1
        assert "bash: sleep 1" in str(getattr(running[0], "visual", ""))

        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="bash", output="hi", duration_ms=1000.0)))
        await pilot.pause(0.1)
        running = [n for n in log.query(Static) if "tool-running" in (n.classes or set())]
        assert len(running) == 0  # replaced by the result card


async def test_fast_tool_skips_running_card() -> None:
    """A tool that ends before the debounce never flickers a running card."""
    from textual.widgets import Static

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        from autoverse.frontends.repl.conversation_log import ConversationLog
        log = app.query_one("#log", ConversationLog)
        # START immediately followed by END (faster than the debounce).
        await s.emit(make_event(ToolStartPayload(
            tool_use_id="t", tool_name="Read",
            tool_input={"file_path": "a.py"})))
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="Read", output="ok", duration_ms=2.0)))
        await pilot.pause(0.3)  # let the (cancelled) debounce window elapse
        running = [n for n in log.query(Static) if "tool-running" in (n.classes or set())]
        assert len(running) == 0


async def test_curated_tool_hard_error_is_expandable() -> None:
    """A HARD error on a curated tool (Edit) — previously a dead-end one-line
    ``Error (N lines)`` whose text lived only in the trace — now folds into an
    expandable Collapsible the user can open in place (MiMo CollapsibleError)."""
    from textual.widgets import Collapsible, Static

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        detail = "OSError: read-only file system\nretry on a writable path"
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="edit_file", output="", is_error=True,
            error_kind="io_error", error_message=detail,
            metadata={"file_path": "/etc/hosts"}, duration_ms=5.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1                           # folded, not a dead line
        assert cols[0].collapsed is True
        # The error card is FLAT — no ``tool-block`` grey-wash / ``│`` left bar
        # (that frame is for block-tool output, not a one-line error).
        assert "tool-block" not in (cols[0].classes or set())
        title = str(cols[0].title)
        assert "Edit /etc/hosts" in title               # tool head preserved
        assert "Error (2 lines)" in title               # red error cue + count
        # The failed head is struck through (markup carries a [strike] span over
        # the tool head). Span offsets index the PLAIN text, not the raw markup.
        from textual.content import Content
        content = Content.from_markup(title)
        assert any(
            s.style == "strike" and "Edit /etc/hosts" in content.plain[s.start:s.end]
            for s in content.spans
        )
        body = "\n".join(
            getattr(getattr(n, "visual", None), "plain", "") or ""
            for n in cols[0].query(Static)
        )
        assert "read-only file system" in body and "retry on a writable path" in body


async def test_always_collapsed_tools_hard_error_uses_error_card() -> None:
    """Curated multi-line cards must not hide hard errors behind their normal
    success-style folding. Hard errors always use the flat Error card shape."""
    from textual.widgets import Collapsible, Static

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="todo", tool_name="todo_write",
            output="todo persistence failed", is_error=True,
            error_kind="io_error", error_message="disk unavailable",
            metadata={"todos": [{"status": "pending", "content": "retry"}]},
            duration_ms=5.0)))
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="jobs", tool_name="JobList",
            output="job backend failed", is_error=True,
            error_kind="server", error_message="queue offline",
            metadata={
                "total": 1,
                "jobs": [{"job_id": "j1", "subject": "check", "status": "failed"}],
            },
            duration_ms=5.0)))
        await pilot.pause(0.2)

        from autoverse.frontends.repl.conversation_log import ConversationLog

        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 2
        titles = [str(c.title) for c in cols]
        assert all("Error (" in title for title in titles)
        assert any("Todos" in title for title in titles)
        assert any("Jobs" in title for title in titles)
        assert all("tool-block" not in (c.classes or set()) for c in cols)
        body = "\n".join(
            getattr(getattr(n, "visual", None), "plain", "") or ""
            for c in cols
            for n in c.query(Static)
        )
        assert "todo persistence failed" in body and "disk unavailable" in body
        assert "job backend failed" in body and "queue offline" in body


async def test_benign_tool_error_folds_as_quiet_details() -> None:
    """A recoverable Edit no-match keeps its quiet struck-through treatment but
    folds so the user can inspect why the edit did not apply."""
    from textual.widgets import Collapsible
    from textual.widgets import Static

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="edit_file", output="", is_error=True,
            error_kind="no_match", error_message="Could not find the search string",
            metadata={"file_path": "a.py"}, duration_ms=3.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True
        assert "tool-block" not in (cols[0].classes or set())
        title = str(cols[0].title)
        assert "Edit a.py" in title
        assert "Details (1 line)" in title
        assert "Error (" not in title
        body = "\n".join(
            getattr(getattr(n, "visual", None), "plain", "") or ""
            for n in cols[0].query(Static)
        )
        assert "Could not find the search string" in body


async def test_short_multiline_bash_also_folds() -> None:
    """A SHORT multi-line bash block folds too (no line-count threshold): even a
    2-line output (``$ bash`` + the line) is a block and folds, collapsed by
    default. Single-line bash (success, no output) stays inline (other test)."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="bash", output="just two\nlines",
            duration_ms=12.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True


async def test_todo_write_folds_collapsed_by_default() -> None:
    """The main-panel todo card folds into a Collapsible, collapsed by default;
    the title shows progress (``Todos · 1/3``) and expanding reveals the coloured
    rows. Folding keeps long checklists from dominating the scrollback."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        todos = [
            {"status": "completed", "content": "alpha"},
            {"status": "in_progress", "content": "beta"},
            {"status": "pending", "content": "gamma"},
        ]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="todo_write", output="Updated 3 todos.",
            metadata={"todos": todos}, duration_ms=5.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True               # folded by default
        assert "Todos" in str(cols[0].title)
        assert "1/3" in str(cols[0].title)             # progress in the title


async def test_ask_user_question_folds_collapsed_by_default() -> None:
    """A successful AskUserQuestion result folds into a Collapsible card,
    collapsed (hidden) by default. The title summarises it; expanding reveals the
    question → user-answer rows. This keeps the prompt out of the main flow until
    the user clicks to review what was asked and answered."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="AskUserQuestion",
            output="User answered…",
            metadata={
                "questions": [
                    {"id": "q1", "prompt": "Pick a colour",
                     "labels": {"r": "Red", "b": "Blue"}},
                ],
                "answer": {"answers": [{"id": "q1", "selected": ["b"]}]},
            },
            duration_ms=5.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True               # hidden by default
        assert "Asked the user" in str(cols[0].title)  # summary in the title


async def test_ask_user_question_unavailable_is_struck_inline_not_card() -> None:
    """No interactive UI is a benign ask failure: struck inline, no error card.
    With no question metadata the renderer falls back to a terse inline line
    (folding one line is pointless). The label now says "Question unavailable"
    (not the old bare "Asked the user") and carries the error reason."""
    from textual.widgets import Collapsible, Static

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="AskUserQuestion",
            output="No interactive question UI is configured.",
            is_error=True, error_kind="ask_user_unavailable",
            error_message="No interactive question UI is configured.",
            duration_ms=1.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        # No Q&A card and no generic Error card: this is a recoverable
        # environment limitation, so it stays a struck-through inline tool line.
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert cols == []
        statics = list(app.query_one("#log", ConversationLog).query(Static))
        ask_contents = [
            getattr(n, "visual", None)
            for n in statics
            if "Question unavailable" in (getattr(getattr(n, "visual", None), "plain", "") or "")
        ]
        assert len(ask_contents) == 1
        content = ask_contents[0]
        assert any(
            "strike" in str(s.style)
            and "Question unavailable" in content.plain[s.start:s.end]
            for s in content.spans
        )


async def test_edit_diff_folds_collapsed_by_default() -> None:
    """A successful edit_file folds: title = ``← Edit <path>``, body = the diff.
    The main view stays one line until the user expands to read the change."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        diff = "@@ -1,2 +1,3 @@\n-old\n+new1\n+new2\n"
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="edit_file", output="Edited",
            metadata={"file_path": "/a.py", "diff": diff, "added": 2, "removed": 1},
            duration_ms=5.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True               # folded by default
        assert "Edit /a.py" in str(cols[0].title)      # path in the title


async def test_write_no_diff_stays_inline() -> None:
    """A Write with no diff body is a single line (``# Wrote <path>``) — folding
    one line is pointless, so it stays inline."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="write_file", output="Wrote",
            metadata={"file_path": "/n.py", "created": True},
            duration_ms=2.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 0                          # single line → not folded


async def test_job_output_folds_when_jobs_listed() -> None:
    """JobOutput with collected jobs folds into a Collapsible (title summary +
    per-job rows). An empty collection stays a single inline line."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        jobs = [
            {"job_id": "1", "subject": "build", "status": "completed"},
            {"job_id": "2", "subject": "test", "status": "completed"},
        ]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="JobOutput", output="…",
            metadata={"collected": 2, "jobs": jobs}, duration_ms=5.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 1
        assert cols[0].collapsed is True
        assert "Collected 2 jobs" in str(cols[0].title)


async def test_job_output_empty_stays_inline() -> None:
    """JobOutput that collected nothing is a one-line status — not a card."""
    from textual.widgets import Collapsible

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo")
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._sink is not None:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        s = app._sink  # type: ignore[attr-defined]
        await s.emit(make_event(ToolEndPayload(
            tool_use_id="t", tool_name="JobOutput", output="…",
            metadata={"collected": 0, "pending": True}, duration_ms=1.0)))
        await pilot.pause(0.2)
        from autoverse.frontends.repl.conversation_log import ConversationLog
        cols = list(app.query_one("#log", ConversationLog).query(Collapsible))
        assert len(cols) == 0


async def test_cancel_queued_removes_item_and_updates_panel() -> None:
    """Ctrl+X cancellation: the picked queued message is removed, the panel
    reactive updates, and the panel hides once the queue empties."""
    from autoverse.frontends.repl.widgets import QueuedPanel

    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(model="echo", cwd=Path.cwd(),
                             session_mode="interactive", event_sink=sink)  # type: ignore[arg-type]
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        q = app._queue  # type: ignore[attr-defined]
        q.enqueue("first", priority="next")
        q.enqueue("second", priority="next")
        app._refresh_queue_panel()  # type: ignore[attr-defined]
        await pilot.pause(0.05)

        panel = app.query_one("#queued-panel", QueuedPanel)
        assert not panel.has_class("-hidden")
        # panel.lines is (priority, text, mode); enqueued without a mode → default.
        assert [ln[1] for ln in panel.lines] == ["first", "second"]
        assert [ln[2] for ln in panel.lines] == ["normal", "normal"]

        # Cancel the first item via the synchronous core (what the picker calls).
        assert app._cancel_queued(0, "first") is True  # type: ignore[attr-defined]
        await pilot.pause(0.05)
        assert [t for _, t in q.items()] == ["second"]
        assert [ln[1] for ln in panel.lines] == ["second"]
        assert not panel.has_class("-hidden")

        # A stale/missing pick removes nothing.
        assert app._cancel_queued(5, "ghost") is False  # type: ignore[attr-defined]
        assert len(q) == 1

        # Cancelling the last item empties the queue and hides the panel.
        assert app._cancel_queued(0, "second") is True  # type: ignore[attr-defined]
        await pilot.pause(0.05)
        assert len(q) == 0
        assert panel.has_class("-hidden")


async def test_command_input_history_recall() -> None:
    """↑/↓ recall previously submitted commands (shell-style), and ↓ past the
    newest entry restores the in-progress draft."""
    from autoverse.frontends.repl.widgets import CommandInput

    ci = CommandInput(id="probe")
    # Simulate two submissions (records history the way ``_submit`` does).
    ci._record_history("first")
    ci._record_history("second")
    assert ci._history == ["first", "second"]

    # Start a fresh draft, then ↑ should recall newest → oldest.
    ci.value = "draft"
    ci._history_prev()
    assert ci.value == "second"
    ci._history_prev()
    assert ci.value == "first"
    ci._history_prev()  # already oldest — stays put
    assert ci.value == "first"

    # ↓ walks back toward newer, then restores the stashed draft.
    ci._history_next()
    assert ci.value == "second"
    ci._history_next()
    assert ci.value == "draft"
    assert ci._history_idx is None


async def test_command_input_history_dedups_consecutive() -> None:
    """Repeating the same command does not create duplicate history entries."""
    from autoverse.frontends.repl.widgets import CommandInput

    ci = CommandInput(id="probe2")
    ci._record_history("ls")
    ci._record_history("ls")
    ci._record_history("pwd")
    assert ci._history == ["ls", "pwd"]


async def test_action_cycle_mode_normal_to_plan() -> None:
    """Tab cycle (forward) moves normal → plan, writing app.extra["plan_mode"]."""
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo",
            cwd=Path.cwd(),
            session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready  # type: ignore[attr-defined]

        # Starts in normal mode.
        assert app._interaction_mode() == "normal"  # type: ignore[attr-defined]

        # Forward cycle: normal → plan.
        await app.action_cycle_mode("forward")  # type: ignore[attr-defined]
        assert app._interaction_mode() == "plan"  # type: ignore[attr-defined]
        assert app._app_ctx.extra["plan_mode"]["active"] is True  # type: ignore[attr-defined]

        # Backward cycle from plan returns to normal.
        await app.action_cycle_mode("backward")  # type: ignore[attr-defined]
        assert app._interaction_mode() == "normal"  # type: ignore[attr-defined]
        assert app._app_ctx.extra["plan_mode"]["active"] is False  # type: ignore[attr-defined]


async def test_tab_cycles_mode_only_when_input_empty() -> None:
    """check_action gates Tab: empty input cycles mode; non-empty falls through."""
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo",
            cwd=Path.cwd(),
            session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready  # type: ignore[attr-defined]

        # Empty input -> Tab cycles to the first non-normal mode (plan).
        assert app.check_action("cycle_mode", ()) is True
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert app._interaction_mode() == "plan"  # type: ignore[attr-defined]

        # Draft text present -> Tab must NOT cycle (falls through to indent).
        from autoverse.frontends.repl.widgets import CommandInput

        app.query_one("#input", CommandInput).value = "hello"
        assert app.check_action("cycle_mode", ()) is False


async def test_plan_approval_enqueues_execution_prompt() -> None:
    from types import SimpleNamespace

    app = AutoVerseApp.__new__(AutoVerseApp)
    queued: list[tuple[str, str]] = []
    logged: list[str] = []
    refreshed = {"queue": 0, "status": 0}

    async def _approve(**_kwargs: object) -> list[str]:
        return ["approve"]

    app._ask_one_question = _approve  # type: ignore[method-assign]
    app._log_line = logged.append  # type: ignore[method-assign]

    def _enqueue(text: str, *, priority: str = "next") -> None:
        queued.append((text, priority))

    app._enqueue = _enqueue  # type: ignore[method-assign]
    app._refresh_queue_panel = (  # type: ignore[method-assign]
        lambda: refreshed.__setitem__("queue", refreshed["queue"] + 1)
    )
    app._refresh_status_context = (  # type: ignore[method-assign]
        lambda: refreshed.__setitem__("status", refreshed["status"] + 1)
    )

    engine = SimpleNamespace(
        app=SimpleNamespace(
            extra={
                "plan_mode": {
                    "active": False,
                    "pending_approval": True,
                    "plan": "1. edit\n2. test",
                }
            }
        )
    )

    await AutoVerseApp._handle_plan_proposed(app, engine)  # type: ignore[arg-type]

    assert engine.app.extra["plan_mode"]["approved"] is True
    assert queued and queued[0][1] == "now"
    assert "Approved plan:" in queued[0][0]
    assert "1. edit" in queued[0][0]
    assert any("Executing now" in line for line in logged)
    assert refreshed == {"queue": 1, "status": 1}


async def _build_app_in_worktree(pilot_holder: dict[str, object]) -> object:
    """Helper: a running app driven into worktree mode over a dirty FakeIsolator."""
    from autoverse.core.loop import worktree_mode
    from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry

    from tests._doubles import FakeIsolator

    ctx = pilot_holder["ctx"]
    ctx.isolator = FakeIsolator(cwd=Path("/tmp/fake-wt"), dirty=True)  # type: ignore[attr-defined]
    ctx.worktrees = InMemoryWorktreeRegistry()  # type: ignore[attr-defined]
    ctx.extra["session_id"] = "s1"  # type: ignore[attr-defined]
    res = await worktree_mode.enter(ctx)  # type: ignore[arg-type]
    assert res.ok
    return ctx


async def test_cancel_finalize_dialog_aborts_switch_stays_in_worktree() -> None:
    """An accidental Tab out of worktree: cancelling (Esc) the keep/merge/discard
    dialog must abort the switch entirely — stay in the worktree, cwd untouched.
    """
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready  # type: ignore[attr-defined]

        ctx = await _build_app_in_worktree({"ctx": app._app_ctx})  # type: ignore[attr-defined]
        assert app._interaction_mode() == "worktree"  # type: ignore[attr-defined]
        cwd_in_box = ctx.cwd  # type: ignore[attr-defined]

        # Simulate Esc / cancel on the finalize dialog.
        async def _cancel(*_a: object, **_k: object) -> None:
            return None

        app.select = _cancel  # type: ignore[assignment,method-assign]
        await app._switch_mode("worktree", "normal")  # type: ignore[attr-defined]

        # Aborted: still in the worktree, cwd unchanged (no exit happened).
        assert app._interaction_mode() == "worktree"  # type: ignore[attr-defined]
        assert ctx.cwd == cwd_in_box  # type: ignore[attr-defined]


async def test_choosing_keep_with_dirty_worktree_stays_in_worktree() -> None:
    """A failed keep finalize leaves the user in worktree mode to commit/retry."""
    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready  # type: ignore[attr-defined]

        ctx = await _build_app_in_worktree({"ctx": app._app_ctx})  # type: ignore[attr-defined]
        assert app._interaction_mode() == "worktree"  # type: ignore[attr-defined]

        async def _keep(*_a: object, **_k: object) -> str:
            return "keep"

        app.select = _keep  # type: ignore[assignment,method-assign]
        await app._switch_mode("worktree", "normal")  # type: ignore[attr-defined]

        assert app._interaction_mode() == "worktree"  # type: ignore[attr-defined]
        assert app._app_ctx is ctx  # type: ignore[attr-defined]


async def test_empty_box_tab_out_discards_without_prompt() -> None:
    """A pristine box Tab'd out is silently discarded — no dialog, no leftover
    worktree, and no leftover registry entry.
    """
    from autoverse.core.loop import worktree_mode
    from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry

    from tests._doubles import FakeIsolator

    registry = build_default_registry()

    async def _factory(sink: object) -> object:
        opts = EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink,  # type: ignore[arg-type]
        )
        return await EngineBuilder(opts).build()

    app = AutoVerseApp(_factory, app_ctx=None, registry=registry)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app._engine_ready:  # type: ignore[attr-defined]
                break
            await pilot.pause(0.05)
        assert app._engine_ready  # type: ignore[attr-defined]

        ctx = app._app_ctx  # type: ignore[attr-defined]
        ctx.isolator = FakeIsolator(cwd=Path("/tmp/fake-empty-wt"), dirty=False)
        ctx.worktrees = InMemoryWorktreeRegistry()
        ctx.extra["session_id"] = "s1"
        assert (await worktree_mode.enter(ctx)).ok
        assert app._interaction_mode() == "worktree"  # type: ignore[attr-defined]

        prompted = {"shown": False}

        async def _must_not_prompt(*_a: object, **_k: object) -> str:
            prompted["shown"] = True
            return "discard"

        app.select = _must_not_prompt  # type: ignore[assignment,method-assign]
        await app._switch_mode("worktree", "normal")  # type: ignore[attr-defined]

        assert app._interaction_mode() == "normal"  # type: ignore[attr-defined]
        assert prompted["shown"] is False  # empty box: no keep/merge/discard dialog
        assert ctx.worktrees.active_manual("s1") is None


def test_status_bar_idle_hint_narrows_to_fit() -> None:
    """The idle status hint must never overflow a narrow bar — it picks the
    widest variant that fits ``size.width - 5`` (the CSS padding), degrading
    full → keys-only → /help, and only prefixes the idle context when it fits."""
    from unittest.mock import patch

    from textual.geometry import Size

    from autoverse.frontends.repl.widgets import StatusBar

    sb = StatusBar()
    sb.phase = "idle"

    def hint_at(width: int) -> str:
        with patch.object(type(sb), "size",
                          property(lambda self: Size(width, 1))):
            return sb._idle_hint()  # type: ignore[attr-defined]

    full = "ready · /help · /model · Ctrl+C interrupt · Ctrl+D quit"
    assert hint_at(120) == full
    # Every variant must fit the usable width (width - 5 padding) while the
    # ``/help`` floor (5 cols) still fits.
    for w in (60, 45, 38, 30, 20, 12):
        assert len(hint_at(w)) <= max(0, w - 5), f"overflow at width={w}"
    # Narrowest floor is always /help (irreducible — shown even when the bar is
    # too small to fit it, rather than collapsing to nothing).
    assert hint_at(8) == "/help"
    # The idle context prefix only appears when the whole line still fits.
    sb.context = "echo · ~/proj"
    assert hint_at(120).startswith("echo · ~/proj · ")
    assert not hint_at(60).startswith("echo")  # dropped to keep keys visible


def test_status_bar_idle_hint_assumes_room_before_layout() -> None:
    """Before the first layout ``size.width`` is 0; the hint must assume room
    (return the full text) rather than collapse to /help."""
    from unittest.mock import patch

    from textual.geometry import Size

    from autoverse.frontends.repl.widgets import StatusBar

    sb = StatusBar()
    sb.phase = "idle"
    with patch.object(type(sb), "size", property(lambda self: Size(0, 0))):
        assert "Ctrl+C interrupt" in sb._idle_hint()  # type: ignore[attr-defined]
