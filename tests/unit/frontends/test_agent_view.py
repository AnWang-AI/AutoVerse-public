"""Sub-agent context drill-down: capture, retention, transcript, in-place view.

Covers the "enter a sub-agent's context" feature (MiMo route-driven parity):
SUBAGENT_CHILD events are captured per job (not written to the main log),
finished agents stay inspectable, the transcript renders, and the in-place
:class:`AgentView` cycles prev/next / returns to Main.
"""

from __future__ import annotations

from typing import Any

from autoverse.frontends.repl.agent_view import AgentView
from autoverse.frontends.repl.renderer import (
    RenderContext,
    render_transcript,
    render_transcript_blocks,
)
from autoverse.frontends.repl.tui_sink import SubAgentRecord, TuiEventSink
from autoverse.types import make_event
from autoverse.types.events import (
    GatePayload,
    RequestPayload,
    SubagentActivityPayload,
    SubagentChildPayload,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolEndPayload,
    TurnEndPayload,
    TurnStartPayload,
    UsagePayload,
)
from autoverse.types.messages import Usage


class _FakeLog:
    """A ConversationLog stand-in that records every write for assertions."""

    def __init__(self) -> None:
        self.writes: list[Any] = []

    def write(self, renderable: Any) -> None:
        self.writes.append(renderable)

    def write_markdown(self, text: str) -> None:
        self.writes.append(("md", text))

    def write_collapsible(self, title: str, body: Any, *, collapsed: bool = True) -> None:
        self.writes.append(("collapsible", title))


def _sink() -> tuple[TuiEventSink, _FakeLog]:
    log = _FakeLog()
    return TuiEventSink(log, RenderContext(width=80)), log


def _child(job: str, sub: str, inner: Any) -> Any:
    return make_event(SubagentChildPayload(job_id=job, subagent_type=sub, inner=inner))


# ── capture + retention ──────────────────────────────────────────────────────


async def test_subagent_child_captured_not_written_to_main_log() -> None:
    sink, log = _sink()
    before = len(log.writes)
    await sink.emit(_child("sa-1", "scout", make_event(TextDeltaPayload(text="hello"))))
    await sink.emit(_child("sa-1", "scout", make_event(ToolEndPayload("t", "grep", "a.py"))))

    # Nothing leaked into the main scrollback…
    assert len(log.writes) == before
    # …but both child events are retained under the owning job.
    recs = {r.job_id: r for r in sink.subagents()}
    assert "sa-1" in recs
    assert len(recs["sa-1"].events) == 2
    assert recs["sa-1"].subagent_type == "scout"


async def test_finished_subagent_stays_inspectable_but_leaves_sidebar() -> None:
    sink, _ = _sink()
    # running heartbeat → live + tracked
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="sa-2", subagent_type="builder", activity="running",
        status="running", turn=1, detail="implement X")))
    await sink.emit(_child("sa-2", "builder", make_event(TextDeltaPayload(text="working"))))
    # sidebar row tuple = (job_id, subagent_type, activity, status, turn, elapsed_ms)
    assert any(a[1] == "builder" for a in sink.agents)            # sidebar active
    assert any(a[0] == "sa-2" for a in sink.agents)               # keyed by job_id
    assert "sa-2" in {r.job_id for r in sink.subagents()}

    # terminal → drops from the sidebar active set …
    await sink.emit(make_event(SubagentActivityPayload(
        job_id="sa-2", subagent_type="builder", activity="completed",
        status="done", elapsed_ms=4200.0, detail="done")))
    assert all(a[1] != "builder" for a in sink.agents)
    # … but the drill-down record persists with its captured transcript + status.
    rec = {r.job_id: r for r in sink.subagents()}["sa-2"]
    assert rec.status == "done"
    assert rec.description == "implement X"          # first-seen task retained
    assert len(rec.events) == 1


async def test_concurrent_same_type_agents_keep_separate_records() -> None:
    sink, _ = _sink()
    await sink.emit(_child("sa-A", "scout", make_event(TextDeltaPayload(text="A"))))
    await sink.emit(_child("sa-B", "scout", make_event(TextDeltaPayload(text="B"))))
    recs = {r.job_id: r for r in sink.subagents()}
    assert set(recs) == {"sa-A", "sa-B"}
    assert recs["sa-A"].events[0].payload.text == "A"
    assert recs["sa-B"].events[0].payload.text == "B"


# ── transcript rendering ─────────────────────────────────────────────────────


def test_render_transcript_coalesces_prose_and_renders_tool() -> None:
    ctx = RenderContext(width=80)
    events = [
        make_event(ThinkingDeltaPayload(text="let me ")),
        make_event(ThinkingDeltaPayload(text="think")),
        make_event(TextDeltaPayload(text="Found ")),
        make_event(TextDeltaPayload(text="it.")),
        make_event(ToolEndPayload("t", "grep", "auth.py")),
    ]
    blocks = render_transcript(events, ctx)
    assert blocks  # non-empty
    assert any("let me think" in b for b in blocks)   # thinking coalesced
    assert any("Found it." in b for b in blocks)      # text coalesced
    assert any("Grep" in b for b in blocks)           # tool card rendered (MiMo label)


def test_render_transcript_empty_for_no_events() -> None:
    assert render_transcript([], RenderContext(width=80)) == []


# ── main-conversation parity: skip junk events, render prose as markdown ──────


def _full_turn_events() -> list[Any]:
    """A sub-agent's captured inner stream for one full turn — includes the
    mechanical/transport events (_SUPPRESSED in app/subagent) that the main
    conversation never displays."""
    return [
        make_event(TurnStartPayload(turn_index=1, iteration=0, model="m")),
        make_event(ThinkingDeltaPayload(text="reasoning")),
        make_event(TextDeltaPayload(text="## Heading\n- a\n- b")),
        make_event(ToolEndPayload("t", "grep", "auth.py")),
        make_event(GatePayload(gate_name="stop", decision="continue")),
        make_event(UsagePayload(usage=Usage(input_tokens=100, output_tokens=40), model="m")),
        make_event(TurnEndPayload(turn_index=1, iterations=3, stop_cause="end_turn")),
    ]


def test_transcript_skips_events_the_main_path_never_shows() -> None:
    """TURN_START/TURN_END/USAGE/GATE must NOT appear in the drill-down view —
    the main conversation filters them, so the sub-agent view must too."""
    blocks = render_transcript_blocks(_full_turn_events(), RenderContext(width=80))
    flat = "\n".join(b.text for b in blocks)
    # The substantive content is present…
    assert "reasoning" in flat
    assert "Heading" in flat
    assert "Grep" in flat  # MiMo display label (capitalized)
    # …but the mechanical/usage rows are gone (would be "▸ turn", "◂ turn",
    # "↑100 ↓40", "gate stop" if leaked).
    assert "turn 1" not in flat and "turn 1 done" not in flat
    assert "↑100" not in flat and "↓40" not in flat
    assert "gate stop" not in flat
    # No block is a turn/usage/gate card.
    assert all("turn" not in b.text and "gate" not in b.text for b in blocks if b.kind == "ansi")


def test_transcript_shows_compaction_divider_for_replay_parity() -> None:
    """COMPACTING is NOT skipped: it renders as a calm boundary divider in the
    transcript too, so live and replay/drill-down views stay consistent."""
    from autoverse.types.events import CompactingPayload

    events = [
        make_event(TextDeltaPayload(text="before compaction")),
        make_event(CompactingPayload(kind="overflow", tokens_before=900, tokens_after=80)),
        make_event(TextDeltaPayload(text="after compaction")),
    ]
    blocks = render_transcript_blocks(events, RenderContext(width=80))
    flat = "\n".join(b.text for b in blocks)
    assert "Context summarised" in flat   # human label present
    assert "overflow" not in flat          # internal kind not leaked
    assert "before compaction" in flat and "after compaction" in flat


def test_transcript_assistant_prose_is_a_markdown_block() -> None:
    """Assistant answer prose must be a ``markdown`` block (same widget the main
    conversation uses), thinking a foldable ``collapsible`` "Thought" card
    (parity with the main path's _write_thinking), tool a pre-styled ``ansi``."""
    blocks = render_transcript_blocks(_full_turn_events(), RenderContext(width=80))
    kinds = {b.kind for b in blocks}
    assert "markdown" in kinds          # the "## Heading" answer
    assert "ansi" in kinds              # the grep tool card
    md = next(b for b in blocks if b.kind == "markdown")
    assert "## Heading" in md.text      # raw markdown preserved (not pre-styled)
    # Reasoning folds into a collapsible "Thought" card with the muted body flag.
    thought = next(b for b in blocks
                   if b.kind == "collapsible" and b.thinking_body)
    assert thought.title == "Thought"
    assert "reasoning" in thought.text


def test_transcript_long_bash_folds_into_collapsible_tool_block() -> None:
    """A multi-line bash tool output folds into a ``collapsible`` block with the
    ``tool-block`` class and a "$ … · N lines" title — parity with the main
    path's _write_tool_collapsible. The full output is the expandable body."""
    out = "\n".join(f"line {i}" for i in range(20))  # multi-line → folds
    events = [make_event(ToolEndPayload("t1", "bash", out))]
    blocks = render_transcript_blocks(events, RenderContext(width=80))
    tool = next(b for b in blocks if b.kind == "collapsible" and not b.thinking_body)
    assert tool.css_class == "tool-block"
    assert "20 lines" in tool.title
    assert "line 19" in tool.text  # full untruncated output is the body


def test_transcript_short_multiline_bash_also_folds() -> None:
    """Even a SHORT multi-line bash output folds (no line-count threshold): a
    2-line output renders as ``$ bash`` + the line, i.e. a block, so it folds
    into a collapsible just like a long one."""
    events = [make_event(ToolEndPayload("t1", "bash", "just two\nlines"))]
    blocks = render_transcript_blocks(events, RenderContext(width=80))
    assert any(b.kind == "collapsible" and not b.thinking_body for b in blocks)


def test_transcript_edit_diff_folds_parity_with_live() -> None:
    """A successful edit_file folds in the transcript too (always-collapse parity
    with the live sink): a ``collapsible`` block titled ``← Edit <path>``."""
    diff = "@@ -1,2 +1,3 @@\n-old\n+new1\n+new2\n"
    events = [make_event(ToolEndPayload(
        "t1", "edit_file", "Edited",
        metadata={"file_path": "/a.py", "diff": diff, "added": 2, "removed": 1}))]
    blocks = render_transcript_blocks(events, RenderContext(width=90))
    tool = next(b for b in blocks if b.kind == "collapsible" and not b.thinking_body)
    assert "Edit /a.py" in tool.title


def test_transcript_job_output_folds_parity_with_live() -> None:
    """JobOutput with collected jobs folds in the transcript too."""
    jobs = [{"job_id": "1", "subject": "build", "status": "completed"}]
    events = [make_event(ToolEndPayload(
        "t1", "JobOutput", "…", metadata={"collected": 1, "jobs": jobs}))]
    blocks = render_transcript_blocks(events, RenderContext(width=90))
    assert any(b.kind == "collapsible" and not b.thinking_body for b in blocks)


def test_transcript_single_line_tool_stays_inline_ansi() -> None:
    """A single-line tool result must NOT fold — it stays a plain ``ansi`` card,
    like the main conversation (which folds multi-line bash / generic output but
    leaves a one-line result, e.g. a successful bash with no output, inline)."""
    # A successful bash with no output renders to one line (``$ bash``).
    events = [make_event(ToolEndPayload("t1", "bash", ""))]
    blocks = render_transcript_blocks(events, RenderContext(width=80))
    assert all(b.kind != "collapsible" for b in blocks)
    assert any(b.kind == "ansi" for b in blocks)


def test_transcript_inline_tool_blocks_carry_tool_line_marker() -> None:
    """A single-line inline tool (web_search / read / grep / …) stays an ``ansi``
    block but carries ``css_class="tool-line"`` so the web snapshot path can map
    it to ``cls: inline tool`` and the frontend's turn fold can count it."""
    events = [make_event(ToolEndPayload(
        "t1", "web_search", "…",
        metadata={"query": "DeepSeek 更新", "count": 8}))]
    blocks = render_transcript_blocks(events, RenderContext(width=80))
    ansi = [b for b in blocks if b.kind == "ansi"]
    assert ansi
    assert all(b.css_class == "tool-line" for b in ansi)
    assert all("Web Search" in b.text for b in ansi)


async def test_agent_view_mounts_collapsibles_for_thought_and_long_tool() -> None:
    """In the mounted view, a thinking block and a long bash block render as
    clickable Collapsibles (collapsed by default), so the user can expand them
    in place — the behaviour that was missing for sub-agents."""
    from textual.app import App, ComposeResult
    from textual.widgets import Collapsible

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(id="agentview")

    rec = _rec("sa-1", "scout", status="done")
    rec.task = ""  # isolate: no user-echo card, only the transcript blocks
    rec.events.extend([
        make_event(ThinkingDeltaPayload(text="weighing the options")),
        make_event(ToolEndPayload("t1", "bash", "\n".join(f"l{i}" for i in range(20)))),
        make_event(TextDeltaPayload(text="done.")),
    ])
    app = _Host()
    async with app.run_test() as pilot:
        view = app.query_one("#agentview", AgentView)
        view.set_records([rec], index=0)
        await pilot.pause(0.05)
        cols = list(view.query(Collapsible))
        assert len(cols) == 2  # Thought + long bash
        assert all(c.collapsed for c in cols)  # collapsed by default
        titles = [str(c.title) for c in cols]
        assert any("Thought" in t for t in titles)
        assert any("bash" in t and "lines" in t for t in titles)
        # The bash card carries the tool-block class for the grey-wash styling.
        assert any(c.has_class("tool-block") for c in cols)


# ── AgentView selection / prev-next navigation (unmounted index math) ─────────


def _rec(job: str, sub: str, status: str = "done") -> SubAgentRecord:
    return SubAgentRecord(job_id=job, subagent_type=sub, status=status,
                          description=f"task {job}")


def test_agent_view_index_cycles() -> None:
    records = [_rec("sa-1", "scout"), _rec("sa-2", "builder"), _rec("sa-3", "reviewer")]
    view = AgentView()
    # set_records / cycle guard widget queries → safe to call unmounted.
    view.set_records(records, index=0)
    assert view.current is not None and view.current.job_id == "sa-1"

    view.cycle(1)
    assert view.current.job_id == "sa-2"
    view.cycle(-1)
    assert view.current.job_id == "sa-1"
    view.cycle(-1)  # wrap backwards
    assert view.current.job_id == "sa-3"


def test_agent_view_clamps_out_of_range_index() -> None:
    view = AgentView()
    view.set_records([_rec("sa-1", "scout")], index=99)
    assert view._index == 0
    assert view.current is not None and view.current.job_id == "sa-1"


def test_agent_view_handles_empty_records() -> None:
    view = AgentView()
    view.set_records([], index=0)
    assert view.current is None


def test_agent_view_cycle_noop_with_single_record() -> None:
    view = AgentView()
    view.set_records([_rec("sa-1", "scout")], index=0)
    view.cycle(1)
    assert view.current is not None and view.current.job_id == "sa-1"


def _usage_child(model: str, usage: Usage) -> Any:
    """A SUBAGENT_CHILD-wrapped USAGE event (as the progress sink captures it)."""
    return make_event(SubagentChildPayload(
        job_id="sa-1", subagent_type="builder",
        inner=make_event(UsagePayload(usage=usage, model=model)),
    )).payload.inner


def test_subagent_meta_aggregates_usage() -> None:
    from autoverse.frontends.repl.tui_sink import subagent_meta

    rec = SubAgentRecord(job_id="sa-1", subagent_type="builder", elapsed_ms=12_300)
    # Two turns: tokens track the LAST usage; cache + cost are cumulative.
    rec.events.append(_usage_child(
        "echo", Usage(input_tokens=100, output_tokens=40, cache_read_tokens=10)))
    rec.events.append(_usage_child(
        "echo", Usage(input_tokens=200, output_tokens=60, cache_read_tokens=30)))
    meta = subagent_meta(rec)
    assert meta.has_usage is True
    assert meta.model == "echo"
    assert meta.input_tokens == 200 and meta.output_tokens == 60   # last usage
    assert meta.cum_cache_read == 40                               # cumulative
    assert meta.elapsed_ms == 12_300
    # cache rate = cache_read / (fresh_input + cache_read) over the cumulative base.
    assert 0.0 < meta.cache_hit_rate < 1.0


def test_subagent_meta_no_usage_is_empty() -> None:
    from autoverse.frontends.repl.tui_sink import subagent_meta

    rec = SubAgentRecord(job_id="sa-1", subagent_type="scout")
    rec.events.append(make_event(TextDeltaPayload(text="hi")))
    meta = subagent_meta(rec)
    assert meta.has_usage is False
    assert meta.input_tokens == 0 and meta.cost_usd == 0.0


def test_agent_view_meta_text_renders_usage_footer() -> None:
    rec = SubAgentRecord(job_id="sa-1", subagent_type="builder", elapsed_ms=12_300)
    rec.events.append(_usage_child(
        "echo", Usage(input_tokens=1200, output_tokens=340, cache_read_tokens=50)))
    view = AgentView()
    text = view._meta_text(rec).plain  # type: ignore[attr-defined]
    assert "▪" in text
    assert "echo" in text                 # model
    assert "12s" in text                  # elapsed
    assert "↑1.2k" in text and "↓340" in text  # token arrows
    assert "cache" in text                # cache rate present


def test_agent_view_meta_text_empty_without_usage() -> None:
    rec = SubAgentRecord(job_id="sa-1", subagent_type="scout", status="running")
    rec.events.append(make_event(TextDeltaPayload(text="working")))
    view = AgentView()
    assert view._meta_text(rec).plain == ""  # type: ignore[attr-defined]
    assert view._meta_text(None).plain == ""  # type: ignore[attr-defined]


async def test_agent_view_meta_widget_populated_when_mounted() -> None:
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(id="agentview")

    rec = SubAgentRecord(job_id="sa-1", subagent_type="builder", elapsed_ms=5_000)
    rec.events.append(_usage_child(
        "echo", Usage(input_tokens=500, output_tokens=120)))
    app = _Host()
    async with app.run_test() as pilot:
        view = app.query_one("#agentview", AgentView)
        view.set_records([rec], index=0)
        await pilot.pause(0.05)
        meta = app.query_one(".agentview-meta", Static)
        rendered = meta.render()
        text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
        assert "echo" in text and "↑500" in text


async def test_agent_view_echoes_task_as_user_card() -> None:
    """The dispatch task is echoed at the top of the transcript as a `.user-echo`
    card — parity with the main conversation's user message (AutoVerseApp
    ._echo_user). No task → no echo card."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(id="agentview")

    rec = _rec("sa-1", "scout", status="running")
    rec.task = "Find all callers of validate_session and report file:line."
    rec.events.append(make_event(TextDeltaPayload(text="searching…")))
    app = _Host()
    async with app.run_test() as pilot:
        view = app.query_one("#agentview", AgentView)
        view.set_records([rec], index=0)
        await pilot.pause(0.05)
        echoes = list(view.query(".user-echo"))
        assert len(echoes) == 1
        # The echo is the FIRST body child (top of the transcript) and carries
        # the full task text (rendered into the widget's content).
        body = view.query_one("#agentview-body")
        first = next(iter(body.query(Static)))
        assert first.has_class("user-echo")
        assert "validate_session" in str(echoes[0].content)  # type: ignore[attr-defined]

        # A record with no task echoes nothing.
        rec2 = _rec("sa-2", "builder", status="running")
        rec2.task = ""
        view.set_records([rec2], index=0)
        await pilot.pause(0.05)
        assert len(view.query(".user-echo")) == 0


def test_title_omits_description_when_it_is_a_task_prefix() -> None:
    """The title's dim sub-line must NOT repeat the prompt: at dispatch the
    description is seeded with ``req.task[:80]`` (a prefix of the full task), and
    the full task is already shown in the echo card below. Showing the prefix in
    the title too would duplicate the prompt at the top of the view."""
    view = AgentView()
    full = (
        "Find all callers of validate_session across the repo and report each "
        "occurrence as file:line with the surrounding function name for context."
    )
    rec = _rec("sa-1", "scout", status="running")
    rec.task = full
    rec.description = full[:80]  # the dispatch-seeded prefix
    title = str(view._title_text(rec))  # type: ignore[attr-defined]
    assert "[scout]" in title
    # The prompt prefix must not appear as a second line in the title.
    assert full[:80] not in title

    # A genuinely distinct description (not a task prefix) is still shown.
    rec.description = "explore auth module"
    title2 = str(view._title_text(rec))  # type: ignore[attr-defined]
    assert "explore auth module" in title2


def test_title_shows_model_and_tool_count_not_turn_count() -> None:
    view = AgentView()
    rec = _rec("sa-1", "builder", status="done")
    rec.model = "hunyuan-code"
    rec.turn = 7
    rec.tool_count = 3
    rec.elapsed_ms = 4200

    title = str(view._title_text(rec))  # type: ignore[attr-defined]

    assert "hunyuan-code" in title
    assert "3 toolcalls" in title
    assert "7t" not in title


def test_tui_sink_captures_full_task_from_running_event() -> None:
    """SUBAGENT_ACTIVITY(status=running) carries the full task → SubAgentRecord.task;
    later heartbeats (task="") must not clobber it."""
    import asyncio

    sink = TuiEventSink(_FakeLog(), RenderContext())
    full = "Implement the parser; handle nested arrays; add a failing test first."

    async def _run() -> None:
        await sink.emit(make_event(SubagentActivityPayload(
            job_id="sa-1", subagent_type="builder", activity="running",
            status="running", turn=1, detail=full[:80], task=full)))
        # Heartbeat with no task — must not overwrite the captured task.
        await sink.emit(make_event(SubagentActivityPayload(
            job_id="sa-1", subagent_type="builder", activity="running",
            status="running", turn=2, detail="turn 2", task="")))

    asyncio.run(_run())
    rec = next(r for r in sink.subagents() if r.job_id == "sa-1")
    assert rec.task == full


def test_tui_sink_captures_tool_count_and_model_for_agent_view_title() -> None:
    import asyncio

    sink = TuiEventSink(_FakeLog(), RenderContext())

    async def _run() -> None:
        await sink.emit(make_event(SubagentActivityPayload(
            job_id="sa-1", subagent_type="builder", activity="running",
            status="running", turn=2, detail="build")))
        await sink.emit(_child(
            "sa-1",
            "builder",
            make_event(UsagePayload(
                usage=Usage(input_tokens=100, output_tokens=20),
                model="echo",
            )),
        ))
        await sink.emit(make_event(SubagentActivityPayload(
            job_id="sa-1", subagent_type="builder", activity="completed",
            status="done", tool_count=5, elapsed_ms=3000.0)))

    asyncio.run(_run())
    rec = next(r for r in sink.subagents() if r.job_id == "sa-1")
    assert rec.model == "echo"
    assert rec.tool_count == 5


async def test_agent_view_refresh_body_tracks_event_count_when_mounted() -> None:
    """Mounted: the body renders and a growing transcript re-renders live."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(id="agentview")

    rec = _rec("sa-1", "scout", status="running")
    app = _Host()
    async with app.run_test() as pilot:
        view = app.query_one("#agentview", AgentView)
        view.set_records([rec], index=0)
        await pilot.pause(0.05)
        # First render recorded the (empty) event count.
        assert view._rendered_n == 0
        # A growing transcript bumps the tracked count on refresh.
        rec.events.append(make_event(TextDeltaPayload(text="hi")))
        view.refresh_body()
        await pilot.pause(0.05)
        assert view._rendered_n == 1


async def test_refresh_body_is_incremental_not_full_rebuild() -> None:
    """The live tick must NOT tear down + rebuild the whole transcript (the
    freeze): refresh_body keeps the existing block widgets and only mounts the
    new/changed tail. Streaming a new tool card after settled prose must leave
    the earlier widgets' identities untouched."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(id="agentview")

    rec = _rec("sa-1", "scout", status="running")
    rec.task = ""  # no echo card — isolate the transcript block region
    # Settled prose (its run ENDS at the tool boundary, so it won't keep mutating)
    # plus a tool card. Both become stable, mounted block widgets.
    rec.events.extend([
        make_event(TextDeltaPayload(text="first answer paragraph.")),
        make_event(ToolEndPayload("t1", "grep", "auth.py")),
    ])
    app = _Host()
    async with app.run_test() as pilot:
        view = app.query_one("#agentview", AgentView)
        view.set_records([rec], index=0)
        await pilot.pause(0.05)
        body = view.query_one("#agentview-body")
        # Transcript block widgets only (exclude the trailing inline meta footer).
        before = [w for w in body.children if not w.has_class("agentview-meta")]
        ids_before = [id(w) for w in before]
        assert len(before) == 2  # markdown + tool ansi

        # Append a NEW, separate tool card (a fresh trailing block).
        rec.events.append(make_event(ToolEndPayload("t2", "grep", "session.py")))
        view.refresh_body()
        await pilot.pause(0.05)
        after = [w for w in body.children if not w.has_class("agentview-meta")]
        # One widget appended; the earlier widgets are the SAME instances (not
        # rebuilt) — the whole point of the incremental path.
        assert len(after) == 3
        assert [id(w) for w in after[:2]] == ids_before


async def test_streaming_prose_updates_markdown_in_place_no_flicker() -> None:
    """The flicker/freeze fix: while a prose answer streams (the last block's
    text grows every tick), the Markdown widget must be UPDATED in place — NOT
    destroyed and re-mounted each tick. Identity of the growing widget stays
    stable across many refreshes."""
    from textual.app import App, ComposeResult
    from textual.widgets import Markdown

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(id="agentview")

    rec = _rec("sa-1", "scout", status="running")
    rec.task = ""
    rec.events.append(make_event(TextDeltaPayload(text="Answer: ")))
    app = _Host()
    async with app.run_test() as pilot:
        view = app.query_one("#agentview", AgentView)
        view.set_records([rec], index=0)
        await pilot.pause(0.05)
        body = view.query_one("#agentview-body")
        md0 = id(next(iter(body.query(Markdown))))

        seen = {md0}
        for i in range(10):  # simulate 10 streaming ticks
            rec.events.append(make_event(TextDeltaPayload(text=f"word{i} ")))
            view.refresh_body()
            await pilot.pause(0.01)
            seen.add(id(next(iter(body.query(Markdown)))))
        # The SAME Markdown widget served all 10 streaming updates (no re-mount).
        assert seen == {md0}, f"Markdown re-mounted during streaming: {len(seen)} instances"
        assert len(list(body.query(Markdown))) == 1


# ── end-to-end: enter via app, swap views, footer Main returns ────────────────


async def test_app_enter_agent_view_swaps_and_returns() -> None:
    """Clicking a sub-agent (here via the app entry point) hides #log and shows
    #agentview; returning to Main restores #log (scroll preserved)."""
    from pathlib import Path

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.frontends.repl.conversation_log import ConversationLog

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        # Seed one dispatched sub-agent into the sink so there's something to view.
        await app._sink.emit(  # type: ignore[union-attr]
            make_event(SubagentActivityPayload(
                job_id="sa-1", subagent_type="scout", activity="grep",
                status="running", turn=1, detail="find auth")))
        await app._sink.emit(  # type: ignore[union-attr]
            _child("sa-1", "scout", make_event(TextDeltaPayload(text="searching"))))

        log = app.query_one("#log", ConversationLog)
        view = app.query_one("#agentview", AgentView)
        assert view.has_class("-hidden") and not log.has_class("-hidden")

        # Enter the sub-agent's context (the path a sidebar click / ctrl+r drives).
        app._enter_agent_view(job_id="sa-1")  # type: ignore[attr-defined]
        await pilot.pause(0.05)
        assert app._agent_view_active is True  # type: ignore[attr-defined]
        assert log.has_class("-hidden") and not view.has_class("-hidden")
        assert view.current is not None and view.current.job_id == "sa-1"

        # The footer Main button posts AgentView.Main → returns to the conversation.
        app.post_message(AgentView.Main())
        await pilot.pause(0.05)
        assert app._agent_view_active is False  # type: ignore[attr-defined]
        assert not log.has_class("-hidden") and view.has_class("-hidden")


async def test_app_enter_agent_view_hides_sidebar_fullwidth() -> None:
    """MiMo parity: entering a sub-agent hides the sidebar so the transcript
    fills the full terminal width; returning to Main restores it (auto rule)."""
    from pathlib import Path

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.frontends.repl.conversation_log import ConversationLog
    from autoverse.frontends.repl.sidebar import Sidebar

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    # Width 130 > _WIDE_COLS (120) → sidebar auto-visible on Main.
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        await app._sink.emit(  # type: ignore[union-attr]
            make_event(SubagentActivityPayload(
                job_id="sa-1", subagent_type="scout", activity="grep",
                status="running", turn=1, detail="find auth")))

        sidebar = app.query_one("#sidebar", Sidebar)
        # On Main: sidebar visible, main column is terminal width minus 42.
        assert app._sidebar_visible() is True  # type: ignore[attr-defined]
        assert not sidebar.has_class("-hidden")
        assert app._main_width() == 130 - 42  # type: ignore[attr-defined]

        log = app.query_one("#log", ConversationLog)
        view = app.query_one("#agentview", AgentView)

        app._enter_agent_view(job_id="sa-1")  # type: ignore[attr-defined]
        await pilot.pause(0.05)
        # Inside sub-agent: sidebar force-hidden, transcript gets full width.
        assert app._sidebar_visible() is False  # type: ignore[attr-defined]
        assert sidebar.has_class("-hidden")
        assert app._main_width() == 130  # type: ignore[attr-defined]
        # Layout: #log must leave the layout entirely (display:none → region
        # collapses to height 0), so #agentview starts at the very top (y=0)
        # rather than being pushed below a reserved #log column. Regression
        # guard for the missing `#log.-hidden { display: none }` rule.
        assert log.region.height == 0
        assert view.region.y == 0 and view.region.height > 0

        app.post_message(AgentView.Main())
        await pilot.pause(0.05)
        # Back on Main: sidebar restored by the normal auto rule.
        assert app._sidebar_visible() is True  # type: ignore[attr-defined]
        assert not sidebar.has_class("-hidden")
        assert app._main_width() == 130 - 42  # type: ignore[attr-defined]


async def test_app_agent_nav_bindings_gated_by_view() -> None:
    """The escape/left/right nav bindings are enabled only while the view shows."""
    from pathlib import Path

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        # Not viewing → nav actions disabled (escape stays free for menus etc.).
        assert app.check_action("agent_prev", ()) is False
        assert app.check_action("agent_main", ()) is False
        # Always-on actions remain enabled.
        assert app.check_action("toggle_sidebar", ()) is True

        await app._sink.emit(  # type: ignore[union-attr]
            make_event(SubagentActivityPayload(
                job_id="sa-9", subagent_type="builder", activity="x",
                status="running", turn=1, detail="t")))
        app._enter_agent_view(job_id="sa-9")  # type: ignore[attr-defined]
        await pilot.pause(0.05)
        # Now viewing → nav actions enabled.
        assert app.check_action("agent_prev", ()) is True
        assert app.check_action("agent_main", ()) is True


async def test_agent_view_arrow_keys_reach_view_despite_input_focus() -> None:
    """In the sub-agent view the focus stays on the input box, so left/right must
    be ``priority`` bindings to reach the view (cycle prev/next) instead of being
    swallowed as cursor movement; Esc must return to Main. When NOT in the view,
    the same keys must fall through to normal text-cursor editing."""
    from pathlib import Path

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.agent_view import AgentView
    from autoverse.frontends.repl.builtin_cmds import build_default_registry

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        for j, t in (("sa-1", "scout"), ("sa-2", "builder")):
            await app._sink.emit(  # type: ignore[union-attr]
                make_event(SubagentActivityPayload(
                    job_id=j, subagent_type=t, activity="x",
                    status="running", turn=1, detail="d", task=f"task {j}")))
        await pilot.pause(0.1)

        # Before entering: Left edits the input cursor (falls through), not nav.
        inp = app.query_one("#input")
        inp.focus()
        await pilot.press("a", "b", "c")
        await pilot.pause(0.05)
        pos_before = inp.cursor_location  # type: ignore[attr-defined]
        await pilot.press("left")
        await pilot.pause(0.05)
        assert inp.cursor_location != pos_before  # type: ignore[attr-defined]  # cursor moved → key fell through
        assert app._agent_view_active is False  # type: ignore[attr-defined]

        # Enter the view; focus is still the input box.
        app._enter_agent_view(index=0)  # type: ignore[attr-defined]
        await pilot.pause(0.1)
        view = app.query_one("#agentview", AgentView)
        assert view.current is not None and view.current.job_id == "sa-1"
        # Right/Left now reach the view (priority bindings) and cycle it.
        await pilot.press("right")
        await pilot.pause(0.1)
        assert view.current.job_id == "sa-2"
        await pilot.press("left")
        await pilot.pause(0.1)
        assert view.current.job_id == "sa-1"
        # Esc returns to Main.
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app._agent_view_active is False  # type: ignore[attr-defined]


async def test_agent_view_collapsible_styling_matches_main_log() -> None:
    """A Thought card in the drill-down must render FLAT (no border) like the
    main conversation's #log > Collapsible — not Textual's default boxed card.
    Regression: the #agentview-body > Collapsible CSS rules were once dropped in
    a refactor, so a Thought rendered with a grey box in the sub-agent view."""
    from pathlib import Path

    from textual.widgets import Collapsible

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        await app._sink.emit(make_event(SubagentActivityPayload(  # type: ignore[union-attr]
            job_id="sa-1", subagent_type="scout", activity="running",
            status="running", turn=1, detail="d", task="inspect")))
        await app._sink.emit(  # type: ignore[union-attr]
            _child("sa-1", "scout", make_event(ThinkingDeltaPayload(text="reasoning here"))))
        await app._sink.emit(  # type: ignore[union-attr]
            _child("sa-1", "scout", make_event(TextDeltaPayload(text="done."))))
        await pilot.pause(0.1)
        app._enter_agent_view(job_id="sa-1")  # type: ignore[attr-defined]
        await pilot.pause(0.2)

        body = app.query_one("#agentview-body")
        thought = next(c for c in body.query(Collapsible) if "Thought" in str(c.title))
        # Flat card: no border edge has a visible style (Textual's default
        # Collapsible would draw a box). Regression guard for the missing
        # #agentview-body > Collapsible CSS that gave Thought a grey box.
        b = thought.styles.border
        assert all(edge[0] in ("", "none") for edge in (b.top, b.right, b.bottom, b.left)), str(b)
        # And a Thought must NOT carry the tool-block (grey-wash) class.
        assert not thought.has_class("tool-block")


async def test_conversation_log_agent_card_mounts_once_updates_in_place() -> None:
    """ConversationLog.write_or_update_agent mounts ONE card per job_id and
    updates it in place on later calls (the live sub-agent card in #log) — so a
    running sub-agent grows a single block, never a new one per tick. ``done``
    releases the key."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from autoverse.frontends.repl.conversation_log import ConversationLog

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield ConversationLog(id="log")

    app = _Host()
    async with app.run_test() as pilot:
        log = app.query_one("#log", ConversationLog)
        n0 = len(list(log.children))

        log.write_or_update_agent("sa-1", "running turn 1")
        await pilot.pause(0.02)
        assert len(list(log.children)) == n0 + 1  # one card mounted
        card = next(w for w in log.children if isinstance(w, Static))
        card_id = id(card)

        # Two more updates for the same job → SAME widget, no new blocks.
        log.write_or_update_agent("sa-1", "running turn 2")
        log.write_or_update_agent("sa-1", "done · 5s", done=True)
        await pilot.pause(0.02)
        assert len(list(log.children)) == n0 + 1
        assert id(next(w for w in log.children if isinstance(w, Static))) == card_id

        # After done the key is released; a new job mounts its own separate card.
        log.write_or_update_agent("sa-2", "running")
        await pilot.pause(0.02)
        assert len(list(log.children)) == n0 + 2


async def test_app_agent_view_hides_input_and_shows_agent_status() -> None:
    """Entering a RUNNING sub-agent collapses the input frame (nothing to type
    into) and repurposes the StatusBar footer to show the OPEN agent's LIVE
    signal as a SINGLE line — spinner + current activity + smooth elapsed.
    Type/status/turns are NOT repeated here (they live in the title). Returning
    to Main restores both."""
    from pathlib import Path

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.frontends.repl.widgets import StatusBar

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        await app._sink.emit(  # type: ignore[union-attr]
            make_event(SubagentActivityPayload(
                job_id="sa-1", subagent_type="scout", activity="grep",
                status="running", turn=3, detail="find auth")))

        wrap = app.query_one("#input-wrap")
        spacer = app.query_one("#input-spacer")
        status = app.query_one("#status", StatusBar)
        # On Main: input frame visible.
        assert not wrap.has_class("-hidden")

        app._enter_agent_view(job_id="sa-1")  # type: ignore[attr-defined]
        await pilot.pause(0.15)  # let a couple of 10 Hz ticks run
        # Input frame + spacer collapse.
        assert wrap.has_class("-hidden")
        assert spacer.has_class("-hidden")
        # StatusBar is driven by a single pre-formatted `agent_line`: spinner +
        # activity + elapsed, all on ONE line (no \n). Type/status/turns are NOT
        # repeated (they're in the title), and the activity sits inline.
        import re

        assert status.agent_line != ""
        assert "\n" not in status.agent_line  # exactly one line
        assert "grep" in status.agent_line
        assert re.search(r"\d+\.\d+s", status.agent_line)  # smooth elapsed present
        assert "[scout]" not in status.agent_line
        assert "running" not in status.agent_line

        app.post_message(AgentView.Main())
        await pilot.pause(0.05)
        # Back on Main: input frame restored, StatusBar released back to main.
        assert not wrap.has_class("-hidden")
        assert not spacer.has_class("-hidden")
        assert status.agent_line == ""


async def test_app_agent_view_finished_agent_status_no_spinner() -> None:
    """A finished sub-agent's StatusBar stays a SINGLE line showing only this
    subagent's terminal state (e.g. ``✓ done · 4s``) — no spinner, and crucially
    no main-agent ready/help/model hint (irrelevant inside a subagent; navigation
    already lives in the pinned AgentView footer)."""
    from pathlib import Path

    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.frontends.repl.widgets import StatusBar

    async def _factory(sink: object) -> object:
        return await EngineBuilder(EngineOptions(
            model="echo", cwd=Path.cwd(), session_mode="interactive",
            event_sink=sink)).build()  # type: ignore[arg-type]

    app = AutoVerseApp(_factory, app_ctx=None, registry=build_default_registry(),
                       cwd="/x", model="echo", context_window=8192)
    async with app.run_test(size=(130, 28)) as pilot:
        await pilot.pause(0.2)
        await app._sink.emit(  # type: ignore[union-attr]
            make_event(SubagentActivityPayload(
                job_id="sa-1", subagent_type="scout", activity="grep",
                status="running", turn=1, detail="x")))
        # Finish it → leaves the live _agents map, persists as a done record.
        await app._sink.emit(  # type: ignore[union-attr]
            make_event(SubagentActivityPayload(
                job_id="sa-1", subagent_type="scout", activity="",
                status="done", turn=2, elapsed_ms=4200.0, detail="x")))

        status = app.query_one("#status", StatusBar)
        app._enter_agent_view(job_id="sa-1")  # type: ignore[attr-defined]
        await pilot.pause(0.15)
        # Single-line terminal-state summary; no main-agent hint, no spinner.
        assert status.agent_line != ""
        assert "\n" not in status.agent_line       # exactly one line
        assert "Done" in status.agent_line         # Title-case (parity w/ main bar)
        assert "4s" in status.agent_line           # frozen final elapsed
        assert "ready" not in status.agent_line    # no main-agent hint
        assert "/help" not in status.agent_line
        assert "/model" not in status.agent_line
