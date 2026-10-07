"""TuiEventSink — bridge ``StreamEvent`` → Textual ``RichLog``.

This sink implements :class:`~autoverse.ports.EventSink`. It does **not**
re-implement any rendering: it reuses the existing event/tool renderer registry
(``render_event`` in :mod:`autoverse.frontends.repl.renderer`) unchanged — the
framework layer is "一字不改" per the v3 design. Each rendered line is produced
with ANSI styling (``RenderContext.ansi=True``) and written to the ``RichLog``
widget as a ``rich.text.Text.from_ansi(...)`` renderable, so the stepped-scale
theme colours survive into the TUI.

Why reuse the string renderers instead of writing Rich renderables directly:
the v3 doc mandates the EventRenderer/ToolRenderer registry stay unchanged; the
TUI is purely an additive consumption-side adapter. Streaming ``TEXT_DELTA``
events are appended live; structured events (TURN_*, TOOL_*, USAGE, ERROR ...)
are written as they arrive. ``RichLog`` keeps full scrollback in memory.

The sink is only imported on the TUI path; ``textual``/``rich`` are optional
deps (``pip install autoverse[tui]``) and never imported by ``--print`` /
``--no-tui``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from autoverse.frontends.repl.renderer import RenderContext, render_event
from autoverse.infra.observ.meter import Meter

# Importing this module registers the built-in per-tool renderers (diff / todo /
# bash / agent) against the shared TOOL_RENDERERS registry — the framework's P8
# extension point. The TUI is the consumer that activates them.
from autoverse.frontends.repl import tool_renderers_builtin  # noqa: F401
from autoverse.types import EventType, StreamEvent
from autoverse.types.events import (
    SubagentActivityPayload,
    TextDeltaPayload,
    ToolEndPayload,
    make_event,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from textual.widgets import RichLog


# Events that are implementation detail, not user-facing — never committed to
# the scrollback (they still reach the trace sink). REQUEST is per-LLM-call
# transport detail; GATE_DECISION is the internal stop-judgment; TURN_START /
# TURN_END are mechanical markers (and double up across codec + turn-loop
# layers). The conversation reads as: user echo → assistant reply → tools →
# a single USAGE footer.
_TRACE_ONLY = frozenset(
    {
        EventType.REQUEST,
        EventType.GATE_DECISION,
        EventType.TURN_START,
        EventType.TURN_END,
    }
)


@dataclass
class SubAgentRecord:
    """A drill-down record for one dispatched sub-agent (TUI-side, P3 presentation).

    Populated purely from the event stream: ``SUBAGENT_ACTIVITY`` drives the
    metadata (status/turn/elapsed/description), ``SUBAGENT_CHILD`` appends the
    child's raw events to ``events``. Retained for the whole session — including
    after the agent finishes — so the user can open a completed agent's context.
    """

    job_id: str
    subagent_type: str
    description: str = ""
    # Full dispatch task — the sub-agent's "user prompt". Echoed at the top of the
    # drill-down transcript (AgentView) as a user-echo card, mirroring how the main
    # conversation echoes the user message. Set once from the initial "running"
    # SUBAGENT_ACTIVITY (``payload.task``); "" when unknown (legacy emits).
    task: str = ""
    status: str = "running"
    turn: int = 0
    elapsed_ms: float = 0.0
    tool_count: int = 0
    model: str = ""
    events: list[StreamEvent] = field(default_factory=list)


@dataclass(frozen=True)
class SubAgentMeta:
    """Aggregated usage meta for one sub-agent (mirrors the main turn footer).

    Computed purely from the sub-agent's retained ``SUBAGENT_CHILD`` transcript
    (its USAGE events), so the drill-down view can show the same model · time ·
    ↑↓tokens · cache% · $cost line the main conversation closes each turn with.
    """

    model: str = ""
    input_tokens: int = 0          # last USAGE in (conversation size), for ↑
    output_tokens: int = 0         # last USAGE out, for ↓
    cum_cache_read: int = 0
    cum_cache_write: int = 0
    cum_prompt: int = 0            # fresh input + cache reads (cache-rate base)
    cost_usd: float = 0.0
    elapsed_ms: float = 0.0
    has_usage: bool = False        # False → no USAGE seen → footer hidden

    @property
    def cache_hit_rate(self) -> float:
        if self.cum_prompt <= 0:
            return 0.0
        return self.cum_cache_read / self.cum_prompt


# A single shared pricing table for sub-agent cost (same source the main sink
# uses — registry-seeded; unpriced models contribute $0, never fabricated).
_SUBAGENT_METER = Meter()


def subagent_meta(rec: SubAgentRecord) -> SubAgentMeta:
    """Aggregate a sub-agent's USAGE events into a :class:`SubAgentMeta`.

    Tokens: ``input``/``output`` track the LAST USAGE (the conversation's current
    size, matching the main footer's ↑↓ arrows); cache + cost are CUMULATIVE
    across the sub-agent's turns. Returns ``has_usage=False`` when the transcript
    carries no USAGE event yet (footer is then suppressed)."""
    model = ""
    last_in = last_out = 0
    cum_cache_read = cum_cache_write = cum_prompt = 0
    cost = 0.0
    seen = False
    for ev in rec.events:
        if ev.type is not EventType.USAGE:
            continue
        payload = ev.payload
        usage = getattr(payload, "usage", None)
        if usage is None:
            continue
        seen = True
        model = getattr(payload, "model", "") or model
        # Keep the last non-zero input: a trailing ``message_delta`` USAGE
        # reports only output_tokens (input=0) and must not zero ↑ (mirrors the
        # main sink's _collect guard).
        if usage.input_tokens:
            last_in = usage.input_tokens
        last_out = usage.output_tokens
        cum_cache_read += usage.cache_read_tokens
        cum_cache_write += usage.cache_write_tokens
        cum_prompt += usage.input_tokens + usage.cache_read_tokens
        if model:
            cost += _SUBAGENT_METER.cost_for(model, usage).total_usd
    return SubAgentMeta(
        model=model,
        input_tokens=last_in,
        output_tokens=last_out,
        cum_cache_read=cum_cache_read,
        cum_cache_write=cum_cache_write,
        cum_prompt=cum_prompt,
        cost_usd=cost,
        elapsed_ms=rec.elapsed_ms,
        has_usage=seen,
    )


def _parse_todo_text(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        raw = line.strip()
        marker = ""
        content = raw
        for prefix in ("[x]", "[X]", "[~]", "[ ]"):
            idx = raw.find(prefix)
            if idx >= 0:
                marker = prefix
                content = raw[idx + len(prefix):].strip()
                break
        if not marker or not content:
            continue
        status = {
            "[x]": "completed",
            "[X]": "completed",
            "[~]": "in_progress",
            "[ ]": "pending",
        }[marker]
        out.append((status, content))
    return out


class TuiEventSink:
    """An :class:`~autoverse.ports.EventSink` that writes to a Textual ``RichLog``.

    Construct with the target ``RichLog`` and a :class:`RenderContext`. The
    context's ``ansi`` flag is forced on so theme colours are emitted; the
    rendered ANSI string is converted to a Rich ``Text`` before writing.
    """

    def __init__(self, log: Any, ctx: RenderContext, *, render_plan_md: bool = True) -> None:
        self._log = log
        self._render_plan_md = render_plan_md
        # Pure pricing table (seeded from the model registry) so the sink can
        # cost each turn itself — UsagePayload.cost_usd is never populated by the
        # codecs, so reading it would always yield $0.
        self._meter = Meter()
        # Force ANSI so the existing renderers emit colour; RichLog renders it
        # via Text.from_ansi (RichLog.markup parses console markup, not raw ANSI).
        ctx.ansi = True
        self._ctx = ctx
        # Accumulate streaming text/thinking so each lands as one wrapped
        # paragraph, not one RichLog line per delta chunk (docs/guides/using-cli-and-tui.md §3.8:
        # delta live + boundary finalize). RichLog cannot append to a partial
        # line, so we buffer deltas per kind and flush the whole block when a
        # boundary event arrives or the stream switches text↔thinking.
        self._buf: list[str] = []
        self._buf_kind: str | None = None  # "text" | "thinking" | None
        # Live streaming: when the log surface supports the streaming seam
        # (``begin_stream``/``update_stream``/``end_stream``, i.e. the real
        # ConversationLog), assistant TEXT_DELTAs grow an in-place Markdown
        # widget token-by-token (MiMo/opencode parity). Line-based test logs
        # (``_FakeRichLog``) lack the seam → we fall back to the original
        # buffer-and-flush-as-one-paragraph behaviour, so their assertions
        # ("nothing written until the boundary") still hold. Thinking is never
        # streamed live — it folds into a "Thought · Ns" card finalized once at
        # the boundary (the duration is only known then).
        self._can_stream: bool = callable(getattr(log, "begin_stream", None))
        self._stream_open: bool = False  # a live text widget is mounted
        # Live "running" tool cards: when the log surface supports the seam
        # (``begin_tool``/``end_tool``/``tick_tool_spinner`` — the real
        # ConversationLog), a tool that is still executing after a short debounce
        # mounts a transient "running" card in the main log (mirroring the
        # sub-agent live card), removed when its TOOL_END card is written. Plain
        # CLI / line-based test logs lack the seam → TOOL_START stays a pure
        # StatusBar activity update (legacy behaviour, tests unchanged).
        self._can_tool_card: bool = callable(getattr(log, "begin_tool", None))
        # Latest transient activity (running tool / subagent) for the StatusBar
        # to show; not committed to the log. Backed by an ordered active map so
        # concurrent tools/sub-agents remove only their own row on completion and
        # reveal the next still-running activity instead of leaving stale text.
        self.activity: str = ""
        self._active_activities: dict[str, str] = {}
        self._active_activity_names: dict[str, str] = {}
        # Fold/expand state: thinking-block start time (for "Thought · Ns")
        # and the show-details toggle (when on, foldables default to expanded).
        self._thinking_started: float = 0.0
        self.show_details: bool = False
        # Sidebar dashboard state collected from the event stream (read by the
        # app each tick; pure data, no rendering here — P2). tokens reflects the
        # most-recent USAGE (conversation size); cost is cumulative.
        self.tokens_used: int = 0
        self.last_input: int = 0   # last USAGE in/out — for the turn footer arrows
        self.last_output: int = 0
        self.last_cache_read: int = 0
        self.last_cache_write: int = 0
        self.cost_usd: float = 0.0
        self.cost_known: bool = True
        self.tps: float = 0.0
        # ``context`` means tokens_used is the latest model request's exact
        # context usage. ``turn_total`` is the honest legacy fallback reconstructed
        # from an older trace that only retained a whole-turn aggregate.
        self.usage_kind: str = "empty"
        # Monotonic count of USAGE events seen this session. The app snapshots it
        # at turn start so the per-turn footer can tell whether THIS turn has
        # produced any usage yet — until it has, the footer shows just the model
        # instead of stale ↑↓/cost carried over from the previous turn.
        self.usage_seq: int = 0
        # Cumulative prompt-cache accounting across the session (for the
        # sidebar's hit-rate panel). ``cum_prompt`` is the full cacheable prompt
        # (fresh input + cache reads); the hit rate is the fraction served from
        # cache.
        self.cum_cache_read: int = 0
        self.cum_cache_write: int = 0
        self.cum_prompt: int = 0
        self.todos: tuple[tuple[str, str], ...] = ()
        # abs_path → (status, added, removed). Files this session's write/edit/
        # delete tools mutated (MiMo File.status parity, event-sourced). Insertion
        # order is preserved (dict) so the sidebar lists oldest-changed first.
        self._changed_files: dict[str, tuple[str, int, int]] = {}
        # job_id → (type, activity, status, turn, elapsed_ms). Keyed on the
        # runner's stable dispatch/job id so concurrent same-type agents (e.g.
        # two scouts) occupy distinct rows and heartbeats update them in place.
        self._agents: dict[str, tuple[str, str, str, int, float]] = {}
        # job_id → monotonic start (set on the first "running" emit). Lets the
        # ``agents`` property recompute a LIVE elapsed every read (the 10 Hz tick)
        # instead of surfacing the per-turn snapshot in the event payload — the
        # latter only advances at child turn boundaries, so the timer "jumps" in
        # multi-second steps. Cleared when the agent reaches a terminal state.
        self._agent_started: dict[str, float] = {}
        # job_id → last rendered card line. Lets the 10 Hz live-elapsed refresh
        # skip the (ANSI-building) ``render_event`` write when the card text is
        # unchanged — e.g. once the elapsed crosses into whole-minute formatting
        # it renders the same label for many consecutive ticks.
        self._agent_card_line: dict[str, str] = {}
        # job_id → SubAgentRecord. Superset of ``_agents``: this keeps FINISHED
        # agents too (with their captured transcript) so the user can drill into
        # a completed sub-agent's context (docs/guides/using-cli-and-tui.md §3.9.3.1).
        self._subagents: dict[str, SubAgentRecord] = {}
        # Vertical rhythm: remember the kind of the last committed write so we
        # can insert ONE blank line (BLOCK_GAP) between dissimilar groups
        # (prose ↔ tool block ↔ usage) without doubling blanks.
        self._last_kind: str = "none"

    @property
    def agents(self) -> tuple[tuple[str, str, str, str, int, float], ...]:
        """Active sub-agent rows for the sidebar, oldest first.

        Each row is ``(job_id, type, activity, status, turn, elapsed_ms)``. The
        ``job_id`` lets the sidebar map a clicked row back to its
        :class:`SubAgentRecord` (drill-down).

        ``elapsed_ms`` is recomputed LIVE from the agent's monotonic dispatch
        start, so the timer advances smoothly every read (the 10 Hz tick) rather
        than only at child turn boundaries (the cause of the "jumping" timer)."""
        now = time.monotonic()
        out: list[tuple[str, str, str, str, int, float]] = []
        for job, (sub, activity, status, turn, elapsed) in self._agents.items():
            started = self._agent_started.get(job)
            live = (now - started) * 1000.0 if started is not None else elapsed
            out.append((job, sub, activity, status, turn, live))
        return tuple(out)

    @property
    def changed_files(self) -> tuple[tuple[str, str, int, int], ...]:
        """Files mutated this session, oldest-changed first (sidebar Changed panel).

        Each row is ``(abs_path, status, added, removed)`` with status ∈
        ``changed|deleted``. Paths are ABSOLUTE — the app relativises them to the
        cwd before handing them to the sidebar (it owns the cwd fact)."""
        return tuple(
            (path, status, added, removed)
            for path, (status, added, removed) in self._changed_files.items()
        )

    def refresh_live_agents(self) -> None:
        """Re-render each RUNNING sub-agent's scrollback card with a live elapsed.

        The card is normally redrawn only when a ``SUBAGENT_ACTIVITY`` event
        arrives (child turn boundaries), which makes its timer jump in multi-
        second steps. Called by the app's 10 Hz tick, this updates the SAME card
        in place (``done=False``) with a freshly-computed elapsed so the timer
        advances smoothly — exactly like the sidebar row. No-op when the log
        surface lacks ``write_or_update_agent`` (plain CLI / tests)."""
        woa = getattr(self._log, "write_or_update_agent", None)
        if not callable(woa) or not self._agents:
            return
        from rich.text import Text

        now = time.monotonic()
        for job, (sub, activity, status, turn, _snapshot) in self._agents.items():
            started = self._agent_started.get(job)
            if started is None:
                continue  # no known start → leave the last rendered value
            elapsed_ms = (now - started) * 1000.0
            rec = self._subagents.get(job)
            payload = SubagentActivityPayload(
                job_id=job,
                subagent_type=sub,
                activity=activity,
                status=status or "running",
                turn=turn,
                elapsed_ms=elapsed_ms,
                description=rec.description if rec else "",
            )
            try:
                line = render_event(make_event(payload), self._ctx)
            except Exception:  # pragma: no cover - defensive (§5)
                continue
            # Skip the widget update when the rendered card is byte-identical to
            # what's already shown (the elapsed label hasn't changed this tick).
            if self._agent_card_line.get(job) == line:
                continue
            self._agent_card_line[job] = line
            woa(job, Text.from_ansi(line), done=False)

    def refresh_live_tools(self, frame: str) -> None:
        """Advance the spinner glyph on each mounted running tool card.

        Called by the app's 10 Hz tick with the current spinner frame so a
        running tool's card pulses in step with the StatusBar. No-op when the
        log surface lacks ``tick_tool_spinner`` (plain CLI / tests)."""
        tick = getattr(self._log, "tick_tool_spinner", None)
        if callable(tick):
            try:
                tick(frame)
            except Exception:  # pragma: no cover - defensive (§5)
                pass

    # Tool input keys that name a call's primary target, tried in order. Used
    # only for the display label (running card + StatusBar) so the user sees
    # WHAT the tool is doing — never to reflect engine internals (P2). Covers
    # the builtin file tools (file_path/path/pattern) and the common
    # bash/web/search shapes; an unknown tool just shows its name.
    _TOOL_TARGET_KEYS: tuple[str, ...] = (
        "file_path", "path", "command", "pattern", "glob_pattern",
        "url", "query", "search_term", "action",
    )

    def _tool_label(self, name: str, tool_input: dict[str, object]) -> str:
        """A short human summary of a tool call from its INPUT (start-time).

        E.g. ``Read src/foo.py``, ``Bash: npm test``, ``Grep pattern``. Falls
        back to the bare tool name when no recognised target field is present."""
        if name == "ExitWorktreeMode":
            action = str(tool_input.get("action") or "").strip()
            return f"Exit worktree: {action}" if action else "Exit worktree"
        target = ""
        for key in self._TOOL_TARGET_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                target = val.strip()
                break
        if not target:
            return name or "tool"
        target = target.splitlines()[0]  # commands/patterns can be multi-line
        if len(target) > 60:
            target = target[:57] + "…"
        # ``bash`` reads better as "Bash: cmd"; path/pattern tools as "Read x".
        sep = ": " if name in ("bash", "Bash") else " "
        return f"{name}{sep}{target}"

    def _tool_running_body(self, label: str) -> Any:
        """The running card's styled label (amber ``tool_running``) as a Rich
        Text; ConversationLog prepends the animated spinner glyph each tick."""
        from rich.text import Text

        styled = self._ctx.theme.style(label, "tool_running", ansi=self._ctx.ansi)
        return Text.from_ansi(styled)

    def _set_activity(self, key: str, label: str, *, name: str = "") -> None:
        """Mark one transient activity as running and surface the newest one."""
        if not key or not label:
            return
        # Move updates to the end so the status bar follows the most recent
        # active signal while preserving older running work as a fallback.
        self._active_activities.pop(key, None)
        self._active_activity_names.pop(key, None)
        self._active_activities[key] = label
        if name:
            self._active_activity_names[key] = name
        self._refresh_activity()

    def _clear_activity(self, key: str, *, name: str = "") -> None:
        """Remove one finished activity and expose the next active fallback."""
        if not key:
            return
        if self._active_activities.pop(key, None) is not None:
            self._active_activity_names.pop(key, None)
        elif name:
            for active_key in reversed(list(self._active_activities)):
                if self._active_activity_names.get(active_key) == name:
                    self._active_activities.pop(active_key, None)
                    self._active_activity_names.pop(active_key, None)
                    break
        self._refresh_activity()

    def _clear_activities(self) -> None:
        """Clear every transient activity at a turn boundary / interrupt."""
        self._active_activities.clear()
        self._active_activity_names.clear()
        self.activity = ""

    def _refresh_activity(self) -> None:
        self.activity = next(reversed(self._active_activities.values()), "")

    def subagents(self) -> tuple[SubAgentRecord, ...]:
        """All dispatched sub-agents (incl. finished), oldest first — for drill-down."""
        return tuple(self._subagents.values())

    def add_replayed_subagent(
        self,
        *,
        job_id: str,
        subagent_type: str,
        description: str = "",
        task: str = "",
        status: str = "done",
        tool_count: int = 0,
        summary: str = "",
    ) -> None:
        """Seed a persisted sub-agent result during session-history replay.

        Live runs retain the full child event stream via ``SUBAGENT_CHILD``. Older
        persisted histories only have the final ``agent`` tool result, so replay
        registers a degraded drill-down record backed by that summary. The main
        card remains clickable after switching sessions instead of becoming a
        plain Static block.
        """
        if not job_id:
            return
        rec = SubAgentRecord(
            job_id=job_id,
            subagent_type=subagent_type or "general",
            description=description,
            task=task,
            status=status or "done",
            tool_count=tool_count,
        )
        if summary.strip():
            rec.events.append(make_event(TextDeltaPayload(text=summary)))
        self._subagents[job_id] = rec

    def collect_replayed_event(self, event: StreamEvent) -> None:
        """Collect sidebar/session state from a persisted event without rendering.

        Session switching replays history directly into ``ConversationLog``. This
        keeps rendering deterministic, but the freshly-created sink would otherwise
        miss structured state that live events collected (TodoWrite, Changed files,
        usage if persisted later). Keep this as data-only: no log writes.
        """
        self._collect(event)

    def collect_replayed_messages(self, messages: Sequence[object]) -> None:
        """Rebuild persisted Todo/Changed state without rendering history.

        Both Textual and Web paint stored messages directly, so neither path
        naturally sends the persisted tool results back through ``emit``.  Keep
        this reconstruction beside the live collector so every frontend applies
        exactly the same tool-name pairing and metadata rules.
        """
        from autoverse.types.events import UsagePayload
        from autoverse.types.messages import ToolResultBlock, ToolUseBlock, Usage

        tool_names: dict[str, str] = {}
        for message in messages:
            role = getattr(message, "role", "")
            content = getattr(message, "content", ())
            if role in ("assistant", "system"):
                metadata = getattr(message, "metadata", {})
                raw_usage = metadata.get("usage") if isinstance(metadata, Mapping) else None
                if isinstance(raw_usage, Mapping):
                    def token(name: str) -> int:
                        raw = raw_usage.get(name, 0)
                        return max(0, int(raw)) if isinstance(raw, (int, float)) else 0

                    self.collect_replayed_event(
                        make_event(
                            UsagePayload(
                                usage=Usage(
                                    input_tokens=token("input_tokens"),
                                    output_tokens=token("output_tokens"),
                                    cache_read_tokens=token("cache_read_tokens"),
                                    cache_write_tokens=token("cache_write_tokens"),
                                ),
                                model=str(metadata.get("usage_model", "")),
                            )
                        )
                    )
                for block in content:
                    if isinstance(block, ToolUseBlock):
                        tool_names[block.id] = block.name
                continue
            if role != "user":
                continue
            for block in content:
                if not isinstance(block, ToolResultBlock):
                    continue
                metadata = dict(block.metadata) if block.metadata else {}
                self.collect_replayed_event(
                    make_event(
                        ToolEndPayload(
                            tool_use_id=block.tool_use_id,
                            tool_name=tool_names.get(block.tool_use_id, "tool"),
                            output=str(block.content),
                            is_error=bool(block.is_error),
                            error_kind=str(metadata.get("error_kind", "")),
                            error_message=str(block.content) if block.is_error else "",
                            metadata=metadata,
                        )
                    )
                )

    def dashboard_snapshot(self) -> dict[str, object]:
        """Return a versioned, rebuildable projection for cold-session display."""
        return {
            "version": 1,
            "tokens_used": self.tokens_used,
            "last_input": self.last_input,
            "last_output": self.last_output,
            "last_cache_read": self.last_cache_read,
            "last_cache_write": self.last_cache_write,
            "cost_usd": self.cost_usd,
            "cost_known": self.cost_known,
            "usage_seq": self.usage_seq,
            "usage_kind": self.usage_kind,
            "cum_cache_read": self.cum_cache_read,
            "cum_cache_write": self.cum_cache_write,
            "cum_prompt": self.cum_prompt,
            "todos": [
                {"status": status, "content": content}
                for status, content in self.todos
            ],
            "changed_files": [
                {
                    "path": path,
                    "status": status,
                    "added": added,
                    "removed": removed,
                }
                for path, status, added, removed in self.changed_files
            ],
        }

    def restore_dashboard_snapshot(self, value: object) -> bool:
        """Restore a persisted dashboard projection, rejecting malformed data."""
        if not isinstance(value, dict) or value.get("version") != 1:
            return False

        def integer(name: str) -> int:
            raw = value.get(name, 0)
            if isinstance(raw, (int, float)):
                return max(0, int(raw))
            return 0

        raw_cost = value.get("cost_usd", 0.0)
        self.tokens_used = integer("tokens_used")
        self.last_input = integer("last_input")
        self.last_output = integer("last_output")
        self.last_cache_read = integer("last_cache_read")
        self.last_cache_write = integer("last_cache_write")
        self.cost_usd = max(0.0, float(raw_cost)) if isinstance(raw_cost, (int, float)) else 0.0
        self.cost_known = value.get("cost_known") is not False
        self.usage_seq = integer("usage_seq")
        kind = str(value.get("usage_kind", "empty"))
        self.usage_kind = kind if kind in {"empty", "context", "turn_total"} else "empty"
        self.cum_cache_read = integer("cum_cache_read")
        self.cum_cache_write = integer("cum_cache_write")
        self.cum_prompt = integer("cum_prompt")

        todos: list[tuple[str, str]] = []
        raw_todos = value.get("todos", [])
        if isinstance(raw_todos, list):
            for item in raw_todos:
                if isinstance(item, dict):
                    content = str(item.get("content", "")).strip()
                    if content:
                        todos.append((str(item.get("status", "pending")), content))
        self.todos = tuple(todos)

        self._changed_files.clear()
        raw_changed = value.get("changed_files", [])
        if isinstance(raw_changed, list):
            def count(item: dict[str, object], name: str) -> int:
                raw = item.get(name, 0)
                if isinstance(raw, (int, float)):
                    return max(0, int(raw))
                return 0

            for item in raw_changed:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).strip()
                status = str(item.get("status", "changed"))
                if path and status in {"changed", "deleted"}:
                    self._changed_files[path] = (
                        status,
                        count(item, "added"),
                        count(item, "removed"),
                    )
        return True

    def agent_live_state(
        self, job_id: str
    ) -> tuple[str, str, int, float] | None:
        """Live ``(activity, status, turn, elapsed_ms)`` for one sub-agent.

        Returns the snapshot from the active ``_agents`` map (with a LIVE elapsed
        recomputed from the monotonic dispatch start) so the in-place AgentView
        footer can show the open agent's status exactly like the sidebar row —
        ``None`` when *job_id* is not (or no longer) running."""
        row = self._agents.get(job_id)
        if row is None:
            return None
        sub, activity, status, turn, elapsed = row
        started = self._agent_started.get(job_id)
        if started is not None:
            elapsed = (time.monotonic() - started) * 1000.0
        return activity, status or "running", turn, elapsed

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of the cumulative prompt served from the prompt cache.

        ``cache_read / (fresh_input + cache_read)``. Returns 0.0 before any
        prompt tokens have been seen.
        """
        if self.cum_prompt <= 0:
            return 0.0
        return self.cum_cache_read / self.cum_prompt

    def mark_user_turn(self) -> None:
        """Called by the app after it echoes a user message. The echo already
        provides a separating blank line, so the first assistant block should
        not add another — reset the rhythm tracker to a neutral state."""
        self._last_kind = "user"

    async def emit(self, event: StreamEvent) -> None:
        """Render *event* via the shared registry and write it to the log.

        A renderer raising is contained (turned into a structured fallback
        line) so a single bad tool card never kills the event stream
        (docs/guides/using-cli-and-tui.md §5: renderer exceptions do not break the session).
        """
        # Streaming deltas: buffer the raw text by kind and finalize at a
        # boundary so the paragraph is written once (cleaner than per-chunk).
        if event.type is EventType.TEXT_DELTA:
            self._accumulate("text", getattr(event.payload, "text", ""))
            return
        if event.type is EventType.THINKING_DELTA:
            self._accumulate("thinking", getattr(event.payload, "text", ""))
            return
        # TOOL_START is a transient "running" indicator (§3.9.3 region ②). It
        # drives the StatusBar activity line — enriched from the tool INPUT so it
        # names the target ("Read src/foo.py", "Bash: npm test") rather than the
        # bare tool name — and, when the log surface supports it, a debounced
        # live "running" card in the main log so a slow tool is visible in the
        # right chronological place instead of the view sitting blank until the
        # result lands. The final TOOL_END card replaces the running card, so
        # each tool still appears once on completion.
        if event.type is EventType.TOOL_START:
            name = getattr(event.payload, "tool_name", "")
            tool_input = getattr(event.payload, "tool_input", {}) or {}
            label = self._tool_label(name, tool_input)
            tid = getattr(event.payload, "tool_use_id", "")
            self._set_activity(f"tool:{tid or name}", label, name=name)
            # The ``agent`` tool is skipped — its SUBAGENT_ACTIVITY already owns a
            # live card; a second running card would duplicate it.
            if self._can_tool_card and name not in ("agent", "Agent"):
                if tid:
                    # Commit any buffered/streaming prose first so the running
                    # card lands AFTER the answer text that requested it.
                    self._flush()
                    try:
                        self._log.begin_tool(tid, self._tool_running_body(label))
                    except Exception:  # pragma: no cover - defensive (§5)
                        pass
            return
        # SUBAGENT_CHILD: a sub-agent's raw card, wrapped by the progress sink so
        # it stays OUT of the main scrollback but is RETAINED per-job for the
        # drill-down view (§3.9.3.1). Append to the owning record's transcript;
        # never write to the log here.
        if event.type is EventType.SUBAGENT_CHILD:
            job = getattr(event.payload, "job_id", "")
            sub = getattr(event.payload, "subagent_type", "")
            inner = getattr(event.payload, "inner", None)
            rec = self._subagents.get(job)
            if rec is None:
                rec = SubAgentRecord(job_id=job, subagent_type=sub)
                self._subagents[job] = rec
            if inner is not None:
                rec.events.append(inner)
                model = getattr(getattr(inner, "payload", None), "model", "") or ""
                if model:
                    rec.model = model
            return
        # SUBAGENT_ACTIVITY drives the sub-agent's SINGLE live #log card: mounted
        # on the first (running) event so a dispatched sub-agent is visible in the
        # main conversation immediately, then updated IN PLACE (turn/elapsed →
        # completion summary) on every later event — never a new block. It also
        # feeds the sidebar row + the drill-down record. The ``agent`` tool's
        # TOOL_END is suppressed (below) so this is the one card per dispatch (it
        # used to be the canonical card, but it only fires at completion — no live
        # view). The lifecycle is driven by ``status`` (running/done/failed/
        # cancelled), falling back to the legacy activity-word check.
        if event.type is EventType.SUBAGENT_ACTIVITY:
            activity = getattr(event.payload, "activity", "")
            sub = getattr(event.payload, "subagent_type", "")
            status = getattr(event.payload, "status", "") or ""
            turn = int(getattr(event.payload, "turn", 0) or 0)
            elapsed = float(getattr(event.payload, "elapsed_ms", 0.0) or 0.0)
            tool_count = int(getattr(event.payload, "tool_count", 0) or 0)
            detail = getattr(event.payload, "detail", "") or ""
            description = getattr(event.payload, "description", "") or ""
            task = getattr(event.payload, "task", "") or ""
            job = getattr(event.payload, "job_id", "") or sub
            terminal = status in ("done", "failed", "cancelled") or (
                not status
                and activity.strip().lower() in ("done", "completed", "complete")
            )
            # The persistent task brief: the dedicated ``description`` field (set
            # on the initial running emit), falling back to the first emit's
            # ``detail`` for legacy producers that don't populate it.
            brief = description or detail
            # Maintain the drill-down record (superset of the sidebar active set);
            # the first event for a job carries its task brief.
            rec = self._subagents.get(job)
            if rec is None:
                rec = SubAgentRecord(
                    job_id=job, subagent_type=sub, description=brief, task=task
                )
                self._subagents[job] = rec
            rec.subagent_type = sub or rec.subagent_type
            rec.status = (status or ("done" if terminal else "running"))
            if turn:
                rec.turn = turn
            if elapsed:
                rec.elapsed_ms = elapsed
            if tool_count:
                rec.tool_count = tool_count
            if not rec.description and brief:
                rec.description = brief
            # Capture the full task once (the initial "running" emit carries it);
            # later heartbeats/terminal emits leave it "" so don't clobber it.
            if not rec.task and task:
                rec.task = task
            if not terminal:
                live_status = status or "running"
                self._set_activity(f"agent:{job}", f"[{sub}] {activity}")
                # sidebar: track/update the live row in place (keyed on job id)
                self._agents[job] = (sub, activity, live_status, turn, elapsed)
                # Record the monotonic dispatch start ONCE (first sighting) so the
                # ``agents`` property can recompute a live elapsed every tick.
                self._agent_started.setdefault(job, time.monotonic())
            else:
                self._clear_activity(f"agent:{job}")
                self._agents.pop(job, None)  # terminal → drop from sidebar active set
                self._agent_started.pop(job, None)  # stop the live timer
                self._agent_card_line.pop(job, None)  # drop the live-card cache
            # Live #log card: mount on the first event (so a dispatched sub-agent
            # is visible in the main conversation from the start) and update the
            # SAME card in place as it runs → completes. The agent tool's TOOL_END
            # is suppressed below so this is the single card per dispatch. We flush
            # buffered prose first so the card lands after any preceding answer.
            woa = getattr(self._log, "write_or_update_agent", None)
            if callable(woa):
                from dataclasses import replace

                from rich.text import Text

                self._flush()
                # Inject the per-job task brief so the head reads MiMo's
                # "{Type} Task — {description}" on EVERY emit — heartbeats and the
                # terminal event don't repeat it, but the record retains it.
                assert isinstance(event.payload, SubagentActivityPayload)
                render_payload = replace(event.payload, description=rec.description)
                try:
                    line = render_event(make_event(render_payload), self._ctx)
                except Exception as exc:  # pragma: no cover - defensive (§5)
                    line = f"[render error: subagent: {exc}]"
                self._gap_before("block")
                # Pass a Rich Text (ANSI parsed) — not the raw str — so the card
                # is styled, not littered with literal escape codes (mirrors
                # _write's Text.from_ansi handling).
                woa(job, Text.from_ansi(line), done=terminal)
                self._last_kind = "block"
            return  # SUBAGENT_ACTIVITY owns the card; never fall through to write
        # COMPACTING is a context boundary, not a transient log line: render it
        # as a calm full-width divider ("── Context summarised ──") that stays in
        # the scrollback (MiMo's persistent "Session compacted" part parity). The
        # pre-compaction messages above it are intentionally KEPT visible so the
        # screen never silently diverges from / loses the conversation history;
        # the divider just marks where the model's context was folded. Token
        # counts and the internal ``kind`` are not surfaced (the sidebar shows
        # live usage).
        if event.type is EventType.COMPACTING:
            self._flush()
            self._write_compaction_divider(event)
            self._last_kind = "block"
            return
        # Collect dashboard data from structured events first (pure data, P2) —
        # even for events we don't render (USAGE feeds the app-composed turn
        # footer + the sidebar, but is not drawn as a bare line here).
        self._collect(event)
        # Trace-only / data-only events are not committed to the scrollback: the
        # per-call REQUEST (transport/max_tokens), internal GATE_DECISION, the
        # mechanical TURN_START/TURN_END markers (which also double up across the
        # codec + turn-loop layers), and USAGE (the app draws a richer turn
        # footer with model + duration + tokens + cost). They still reach the
        # trace sink (P2). We flush buffered prose first so the final paragraph
        # lands before the turn closes.
        if event.type in _TRACE_ONLY or event.type is EventType.USAGE:
            self._flush()
            return
        # Any non-delta event is a boundary: flush buffered prose first so order
        # is preserved (text precedes the tool line that follows).
        self._flush()
        if event.type is EventType.TOOL_END:
            tid = getattr(event.payload, "tool_use_id", "")
            name = getattr(event.payload, "tool_name", "")
            self._clear_activity(f"tool:{tid or name}", name=name)
        # The ``agent`` tool's TOOL_END is NOT drawn here: the dispatched
        # sub-agent already owns a single live #log card driven by its
        # SUBAGENT_ACTIVITY events (mounted on dispatch, updated in place to the
        # completion summary). Every dispatch emits SUBAGENT_ACTIVITY(running)
        # first (runner._run_one), so the card always exists; rendering TOOL_END
        # too would duplicate it. The result is still recorded for the trace sink.
        if event.type is EventType.TOOL_END and \
                getattr(event.payload, "tool_name", "") in ("agent", "Agent"):
            return
        # Remove the live "running" card (if one was mounted) for this tool so
        # the final card below lands in its place. No-op for fast tools whose
        # debounce never fired, or when the log surface lacks the seam.
        if event.type is EventType.TOOL_END:
            tid = getattr(event.payload, "tool_use_id", "")
            if self._can_tool_card:
                end = getattr(self._log, "end_tool", None)
                if tid and callable(end):
                    try:
                        end(tid)
                    except Exception:  # pragma: no cover - defensive (§5)
                        pass
            if self._write_exit_plan(event):
                self._last_kind = "block"
                return
        try:
            line = render_event(event, self._ctx)
        except Exception as exc:  # pragma: no cover - defensive (§5)
            line = f"[render error: {event.type.value}: {exc}]"
        if line:
            # Long tool blocks (bash failure / generic / MCP) fold into a
            # collapsible card the user expands in place (MiMo parity); short
            # blocks and curated cards (diff/todo/agent/inline) render as-is.
            if event.type is EventType.TOOL_END and self._is_collapsible_tool(event, line):
                self._write_tool_collapsible(event, line)
                self._last_kind = "block"
                return
            kind = "block" if "\n" in line else "line"
            self._gap_before(kind)
            # Multi-line tool cards (diff / bash failure / curated blocks) get the
            # MiMo BlockTool look — a real left bar + grey background drawn by the
            # ``tool-block`` CSS class — instead of a baked-in ``┃`` glyph.
            # Single-line tool cards (web_search / read / grep / …) get the
            # ``tool-line`` marker so the WebLog emits ``cls: inline tool`` and
            # the web frontend's turn-history fold can count them as tool calls.
            if event.type is EventType.TOOL_END:
                self._write(
                    line,
                    classes="tool-block" if kind == "block" else "tool-line",
                )
            else:
                self._write(line)
            self._last_kind = kind

    # ── internals ────────────────────────────────────────────────────────────
    def _collect(self, event: StreamEvent) -> None:
        """Update sidebar state from structured events (no rendering, P2)."""
        if event.type is EventType.REQUEST:
            # A new provider request starts a new context measurement. Streaming
            # protocols may report prompt/cache at message_start and output at
            # message_delta, so the fields are reset here and then preserved
            # across every USAGE fragment belonging to this request.
            self.last_input = 0
            self.last_output = 0
            self.last_cache_read = 0
            self.last_cache_write = 0
        elif event.type is EventType.USAGE:
            usage = getattr(event.payload, "usage", None)
            if usage is not None:
                # A streaming turn reports usage in stages: the opening event
                # (Anthropic ``message_start``) carries the prompt size
                # (input_tokens) with a tiny output; the closing event
                # (``message_delta``) carries the final output_tokens but NO
                # input. Don't let that trailing delta clobber ↑ to 0 — keep the
                # last non-zero input (the current call's prompt size) so the
                # footer's ↑ stays correct as ↓ fills in live.
                if usage.input_tokens:
                    self.last_input = usage.input_tokens
                self.last_output = usage.output_tokens
                if usage.cache_read_tokens:
                    self.last_cache_read = usage.cache_read_tokens
                if usage.cache_write_tokens:
                    self.last_cache_write = usage.cache_write_tokens
                self.tokens_used = (
                    self.last_input + self.last_output
                    + self.last_cache_read + self.last_cache_write
                )
                self.usage_kind = "context"
                # Cumulative prompt-cache accounting (for the hit-rate panel).
                self.cum_cache_read += usage.cache_read_tokens
                self.cum_cache_write += usage.cache_write_tokens
                self.cum_prompt += usage.input_tokens + usage.cache_read_tokens
                # Cost: the codecs never populate UsagePayload.cost_usd, so price
                # the turn ourselves via the registry-seeded Meter (same source
                # of truth as the trace). The provider stamps the canonical model
                # id onto the USAGE event, so cost_for resolves correctly.
                model = getattr(event.payload, "model", "") or ""
                priced = self._meter.cost_for(model, usage)
                self.cost_usd += priced.total_usd
                self.cost_known = self.cost_known and priced.known
                self.usage_seq += 1
        elif event.type is EventType.TOOL_END:
            # Accept both the AutoVerse tool name and the legacy Claude-style
            # name because traces/tests may contain either spelling.
            if getattr(event.payload, "tool_name", "") in ("todo_write", "TodoWrite"):
                self.todos = self._parse_todos(event.payload)
            self._collect_changed_file(event.payload)

    def _write_exit_plan(self, event: StreamEvent) -> bool:
        """Render ExitPlanMode plans as Markdown instead of muted tool text."""
        payload = event.payload
        if not isinstance(payload, ToolEndPayload):
            return False
        if payload.tool_name != "ExitPlanMode" or payload.is_error:
            return False
        plan = str((payload.metadata or {}).get("plan") or "").strip()
        if not plan:
            return False
        # WebUI renders the plan as a dedicated approval card after the turn
        # ends (WebSession._emit_plan_card); suppress the markdown duplicate.
        if not self._render_plan_md:
            return True
        self._gap_before("block")
        head = self._ctx.theme.style(
            "◔ Plan proposed", "success", ansi=self._ctx.ansi
        )
        self._write(head)
        self._write_markdown(plan)
        return True

    def _collect_changed_file(self, payload: object) -> None:
        """Accumulate file mutations for the sidebar Changed panel.

        Reads the file tools' ``ToolEndPayload.metadata`` (``file_path`` /
        ``added`` / ``removed``). write/edit sum their +/- into a ``changed``
        entry; delete flips the entry to ``deleted`` (+/- zeroed). Failed
        mutations (``is_error``) carry no real change and are ignored."""
        name = getattr(payload, "tool_name", "")
        if getattr(payload, "is_error", False):
            return
        meta = getattr(payload, "metadata", {}) or {}
        path = meta.get("file_path")
        if not isinstance(path, str) or not path:
            return
        if name in ("delete_file", "FileDelete"):
            self._changed_files[path] = ("deleted", 0, 0)
            return
        if name in ("write_file", "WriteFile", "edit_file", "FileEdit", "Edit", "Write"):
            try:
                added = int(meta.get("added", 0) or 0)
                removed = int(meta.get("removed", 0) or 0)
            except (TypeError, ValueError):
                added = removed = 0
            prev = self._changed_files.get(path)
            if prev is not None and prev[0] == "changed":
                self._changed_files[path] = (
                    "changed", prev[1] + added, prev[2] + removed
                )
            else:
                self._changed_files[path] = ("changed", added, removed)

    @staticmethod
    def _parse_todos(payload: object) -> tuple[tuple[str, str], ...]:
        """Read the structured ``metadata['todos']`` (list of {status, content})
        attached by TodoWriteTool; tolerate a legacy text fallback."""
        meta = getattr(payload, "metadata", {}) or {}
        items = meta.get("todos")
        if isinstance(items, (list, tuple)):
            out: list[tuple[str, str]] = []
            for it in items:
                if isinstance(it, dict):
                    status = str(it.get("status", "pending"))
                    content = str(it.get("content", "")).strip()
                    if content:
                        out.append((status, content))
            return tuple(out)
        if isinstance(items, str):
            return tuple(_parse_todo_text(items))
        return ()

    def _accumulate(self, kind: str, text: str) -> None:
        if not text:
            return
        # Switching delta kind (thinking→text) is itself a boundary: flush.
        if self._buf_kind is not None and kind != self._buf_kind:
            self._flush()
        if kind == "thinking" and self._buf_kind != "thinking":
            import time
            self._thinking_started = time.monotonic()  # for "Thought · Ns"
        self._buf_kind = kind
        self._buf.append(text)
        # Live streaming (real ConversationLog only): grow the in-place answer
        # Markdown widget on each delta so the user watches it stream. Only the
        # assistant answer streams — thinking folds into a card finalized once
        # at the boundary (its "Thought · Ns" duration is unknown until then).
        if kind == "text" and self._can_stream:
            prose = "".join(self._buf)
            try:
                if not self._stream_open:
                    self._log.begin_stream(prose)
                    self._stream_open = True
                else:
                    self._log.update_stream(prose)
            except Exception:  # pragma: no cover - defensive: never break stream
                # Streaming surface misbehaved; fall back to buffer-and-flush by
                # disabling live mode for the rest of the session.
                self._can_stream = False
                self._stream_open = False

    def _flush(self) -> None:
        if not self._buf:
            # A live stream may be open with all its text already rendered (the
            # buffer was drained into the widget delta-by-delta). Commit it so
            # the next block starts fresh.
            if self._stream_open:
                self._commit_stream()
            return
        prose = "".join(self._buf)
        kind = self._buf_kind
        self._buf = []
        self._buf_kind = None
        if not prose.strip():
            # Whitespace-only buffer: still release any open live stream.
            if self._stream_open:
                self._commit_stream()
            return
        # thinking → muted plain prose (reasoning, kept visually secondary);
        # answer text → rendered as MARKDOWN (headings/lists/code/inline code/
        # bold) so the assistant's structured output reads properly. Each prose
        # block is its own rhythm group.
        group = "thinking" if kind == "thinking" else "text"
        self._gap_before(group)
        if group == "thinking":
            self._write_thinking(prose)
        elif self._stream_open:
            # The answer was already rendered live into the streaming widget;
            # just commit it (no second mount — that would duplicate the block).
            self._commit_stream()
        else:
            self._write_markdown(prose)
        self._last_kind = group

    def finalize_turn(self) -> None:
        """Flush any buffered/streaming prose at a turn boundary.

        The app calls this just before writing the per-turn footer so that an
        interrupted turn (which may not emit a trailing USAGE/TURN_END boundary
        event) still commits the in-flight answer instead of leaving a dangling
        live widget that the next turn would keep growing."""
        self._flush()
        # Drop any running tool card left dangling by an interrupted turn (whose
        # tools never emit TOOL_END) so a frozen spinner never lingers.
        clr = getattr(self._log, "clear_running_tools", None)
        if callable(clr):
            try:
                clr()
            except Exception:  # pragma: no cover - defensive (§5)
                pass
        self._clear_activities()

    def reset_after_rewind(self) -> None:
        """Drop derived state that may belong to history removed by a rewind.

        Usage and cost counters intentionally remain cumulative: rewinding cannot
        unspend a completed model request. Conversation-derived todos, changed
        files, agents, and partial rendering state must not survive the rewrite.
        """
        self._buf.clear()
        self._buf_kind = None
        self._stream_open = False
        self.todos = ()
        self._changed_files.clear()
        self._agents.clear()
        self._agent_started.clear()
        self._agent_card_line.clear()
        self._subagents.clear()
        self._last_kind = "none"
        self._clear_activities()

    def _commit_stream(self) -> None:
        """Finalize the in-flight live answer widget (release the reference)."""
        self._stream_open = False
        end = getattr(self._log, "end_stream", None)
        if callable(end):
            try:
                end()
            except Exception:  # pragma: no cover - defensive
                pass

    def _write_thinking(self, text: str) -> None:
        """Fold the reasoning into a collapsible ``Thought · Ns`` card (MiMo
        parity) — collapsed by default (the biggest source of verbosity), or
        expanded when ``show_details`` is on. Falls back to a muted line if the
        log surface has no collapsible support (line-based tests)."""
        import time

        from autoverse.frontends.repl.theme import DEFAULT_THEME  # role lookup

        secs = (time.monotonic() - self._thinking_started) if self._thinking_started else 0.0
        dur = f" · {secs:.1f}s" if secs >= 0.1 else ""
        title = f"[$av-text-muted]Thought{dur}[/$av-text-muted]"
        body = self._ctx.theme.style(text, "text_muted", ansi=self._ctx.ansi)
        wc = getattr(self._log, "write_collapsible", None)
        if callable(wc):
            from rich.text import Text
            wc(title, Text.from_ansi(body), collapsed=not self.show_details)
        else:  # pragma: no cover - line-based fallback
            self._write(body)

    # Tools that fold into a collapsible card. ``todo_write`` always folds
    # (collapsed by default — the title shows progress ``☰ Todos · 2/5`` and the
    # user expands in place to see the rows). ``bash`` and any UNREGISTERED
    # (generic / MCP) tool fold whenever their output is multi-line (a block),
    # collapsed by default; a single-line result (e.g. a successful bash with no
    # output) stays inline — folding one line is pointless. Curated cards (Edit
    # diff, Agent) and inline tools (Read/Grep) never fold.
    _COLLAPSE_TOOLS = frozenset({"bash", "Bash"})
    # Curated multi-line cards that always fold (collapsed by default): the head
    # line is the title and the rest is the expandable body, so the main view
    # stays one line per tool until the user clicks to expand. The ``"\n" in
    # rendered`` guard in ``_is_collapsible_tool`` keeps single-line renders
    # (a failed ask, a Write with no diff, an empty JobList) inline — folding one
    # line is pointless.
    #   * todo_write        → "☰ Todos · 2/5"        + checklist rows
    #   * AskUserQuestion   → "? Asked the user · N"  + Q→A rows
    #   * edit/write        → "← Edit <path>"         + coloured diff rows
    #   * JobOutput/JobList → "▸ Collected N jobs"    + per-job rows
    #   * read_image        → "◉ Read image <src>"    + question + answer rows
    _ALWAYS_COLLAPSE_TOOLS = frozenset(
        {
            "todo_write", "TodoWrite",
            "AskUserQuestion",
            "edit_file", "Edit", "write_file", "Write", "MultiEdit",
            "JobOutput", "JobList",
            "read_image",
        }
    )

    def _is_collapsible_tool(self, event: StreamEvent, rendered: str) -> bool:
        name = getattr(event.payload, "tool_name", "")
        from autoverse.frontends.repl.tool_renderer import TOOL_RENDERERS
        from autoverse.frontends.repl.tool_error import error_text, is_benign_error
        # Every failed tool with an explanation folds so the reason is available
        # in place. Hard errors use a red ``Error`` cue; recoverable/denied
        # failures keep their quieter struck-through treatment with ``Details``.
        if (
            isinstance(event.payload, ToolEndPayload)
            and event.payload.is_error
            and error_text(event.payload).strip()
        ):
            if (
                name == "AskUserQuestion"
                and is_benign_error(event.payload)
                and "\n" not in rendered
            ):
                return False
            return True
        if name in self._ALWAYS_COLLAPSE_TOOLS and "\n" in rendered:
            # Always fold (collapsed by default) — but only a real multi-line
            # card. A one-line render (e.g. a failed AskUserQuestion) stays inline
            # since folding a single line is pointless.
            return True
        # Curated (registered) tools render their own card and don't fold;
        # unregistered (generic/MCP) tools may. Read live (renderers register
        # at import of tool_renderers_builtin, after this module loads).
        known = name in TOOL_RENDERERS
        is_multiline = "\n" in rendered
        # bash blocks, or any UNREGISTERED (generic/MCP) tool — fold whenever the
        # rendered output spans more than one line (single-line stays inline).
        return is_multiline and (name in self._COLLAPSE_TOOLS or not known)

    def _write_tool_collapsible(self, event: StreamEvent, rendered: str) -> None:
        """Fold a tool block: the first rendered line becomes the title, the rest
        the (expandable) body. ``todo_write`` keeps its curated, coloured list
        rows as the body; other tools use the full untruncated output."""
        self._gap_before("block")
        from rich.text import Text

        from autoverse.frontends.repl.tool_error import (
            error_text,
            is_benign_error,
            is_hard_error,
        )

        name = getattr(event.payload, "tool_name", "")
        first, _, rest = rendered.partition("\n")
        head = Text.from_ansi(first).plain.replace("┃", "").strip()

        if isinstance(event.payload, ToolEndPayload) and is_hard_error(event.payload):
            # Expandable error card (MiMo ``CollapsibleError``). The failed tool
            # head is struck through (the call did not succeed) and a red
            # ``Error (N lines)`` cue follows. For curated multi-line tools
            # (AskUserQuestion) the renderer's body carries question context +
            # the error line, so use it as the collapsible body so the user sees
            # *what* was asked and *why* it failed. For bash / generic tools the
            # body is the full untruncated error text (every line of output).
            if rest.strip() and name == "AskUserQuestion":
                body = Text.from_ansi(rest)
                error_classes = "tool-block"
            else:
                full = error_text(event.payload)
                body = Text(full, style="default")
                # Flat — no ``tool-block`` grey-wash / ``│`` left bar (that frame
                # is for block-tool output, not a one-line error card).
                error_classes = ""
            n = body.plain.count("\n") + 1 if body.plain.strip() else 1
            label = f"Error ({n} {'line' if n == 1 else 'lines'})"
            title = (
                f"[$av-text-muted][strike]{head}[/strike][/$av-text-muted]"
                f"  [$av-tool-err]· {label}[/$av-tool-err]"
            )
            wc = getattr(self._log, "write_collapsible", None)
            if callable(wc):
                wc(title, body, collapsed=not self.show_details,
                   classes=error_classes)
            else:  # pragma: no cover - line-based fallback
                self._write(rendered)
            return

        if (
            isinstance(event.payload, ToolEndPayload)
            and is_benign_error(event.payload)
            and error_text(event.payload).strip()
        ):
            full = error_text(event.payload)
            body = Text(full, style="default")
            n = len(full.splitlines()) or 1
            label = f"Details ({n} {'line' if n == 1 else 'lines'})"
            title = (
                f"[$av-text-muted][strike]{head}[/strike][/$av-text-muted]"
                f"  [$av-text-dim]· {label}[/$av-text-dim]"
            )
            wc = getattr(self._log, "write_collapsible", None)
            if callable(wc):
                wc(title, body, collapsed=not self.show_details, classes="")
            else:  # pragma: no cover - line-based fallback
                self._write(rendered)
            return

        if name in self._ALWAYS_COLLAPSE_TOOLS:
            # Title = "☰ Todos · 2/5" (curated head); body = the coloured rows
            # below it (preserve icons/colours via ANSI), gutter bar stripped.
            body_lines = [
                ln.partition("┃")[2].lstrip() if "┃" in ln else ln
                for ln in rest.splitlines()
            ]
            title = f"[$av-text-muted]{head}[/$av-text-muted]"
            body = Text.from_ansi("\n".join(body_lines))
        else:
            full = str(getattr(event.payload, "output", "") or "")
            n = full.count("\n") + 1 if full else 0
            title = f"[$av-text-muted]{head}" + (f"  · {n} lines" if n else "") + "[/$av-text-muted]"
            body = Text(full, style="default")

        wc = getattr(self._log, "write_collapsible", None)
        if callable(wc):
            try:
                wc(title, body, collapsed=not self.show_details, classes="tool-block")
            except TypeError:  # pragma: no cover - older signature
                wc(title, body, collapsed=not self.show_details)
        else:  # pragma: no cover - line-based fallback
            self._write(rendered)

    def _write_markdown(self, text: str) -> None:
        """Render assistant prose as markdown (Rich), themed for the dark UI.

        ``RichLog`` renders a ``rich.markdown.Markdown`` natively and inherits
        the log's left-padding gutter. Falls back to plain themed text if Rich
        markdown is unavailable or errors (defensive — never break the stream)."""
        wm = getattr(self._log, "write_markdown", None)
        if callable(wm):
            try:
                wm(text)
                return
            except Exception:  # pragma: no cover - defensive
                pass
        # Fallback (line-based log / error): Rich Markdown or plain themed text.
        # ``ansi_dark`` is deliberately the SAME ANSI dark scheme the primary
        # Textual path uses for fenced code (``autoverse`` theme → ANSI dark
        # highlight), so both markdown paths share one code-highlight palette —
        # a fallback answer never re-tints code differently from a normal one.
        try:
            from rich.markdown import Markdown

            self._log.write(Markdown(text, code_theme="ansi_dark", hyperlinks=False))
        except Exception:  # pragma: no cover - defensive
            self._write(self._ctx.theme.style(text, "text", ansi=self._ctx.ansi))

    def _write_compaction_divider(self, event: StreamEvent) -> None:
        """Mount a calm, centred boundary line marking a compaction (MiMo's
        ``──── Session compacted ────`` parity). Rendered as a full-width Rich
        ``Rule`` with the muted label centred; falls back to the renderer's
        plain divider string when the log surface only supports plain writes."""
        from autoverse.frontends.repl.renderer import compaction_label

        kind = str(getattr(event.payload, "kind", "") or "")
        label = compaction_label(kind)
        self._gap_before("block")
        try:
            from rich.rule import Rule

            colour = self._ctx.theme.role("text_muted")
            rule = Rule(
                title=f"[{colour}]{label}[/{colour}]",
                characters="─",
                style=colour,
            )
            self._log.write(rule)
        except Exception:  # pragma: no cover - defensive / line-based fallback
            from autoverse.frontends.repl.renderer import render_event

            self._write(render_event(event, self._ctx))

    def _gap_before(self, kind: str) -> None:
        """Vertical rhythm is now owned by the ConversationLog's per-block CSS
        margins (one blank row between every mounted widget) — the widget tree
        can't append bare blank lines the way the old RichLog did, and doing so
        here would *double* the gap (and leave empty Static rows). Kept as a
        no-op so the call sites and ``_last_kind`` bookkeeping stay intact."""
        return

    def _write(self, line: str, *, classes: str = "") -> None:
        from rich.text import Text

        if classes:
            try:
                self._log.write(Text.from_ansi(line), classes=classes)
                return
            except TypeError:  # pragma: no cover - line-based log fallback
                pass
        self._log.write(Text.from_ansi(line))


__all__ = ["TuiEventSink", "SubAgentRecord", "SubAgentMeta", "subagent_meta"]
