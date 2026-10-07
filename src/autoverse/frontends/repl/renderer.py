"""Event renderers — EventType → renderer registry, no giant if-chain (R1, D3).

``render_event`` is a thin table lookup: ``RENDERERS.get(event.type)``. Each
EventType maps to exactly one ``EventRenderer`` (a callable returning a plain
string). TOOL_START / TOOL_END renderers merely hand off to the second axis
(``tool_renderer.get_tool_renderer``). Unregistered event types fall back to
:data:`DEFAULT_RENDERER` for forward compatibility (P8).

Renderers return plain strings; ANSI styling is opt-in via ``RenderContext.ansi``
so unit tests assert deterministic plain text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from rich.text import Text

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.frontends.repl.tool_renderer import get_tool_renderer
from autoverse.types import EventType, StreamEvent
from autoverse.types.events import (
    ApprovalPayload,
    CompactingPayload,
    ErrorPayload,
    GatePayload,
    Payload,
    RequestPayload,
    RetryPayload,
    SubagentActivityPayload,
    SubagentChildPayload,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    TurnStartPayload,
    UsagePayload,
)


@dataclass
class RenderContext:
    """Render-time shared state carried as instance fields, no module globals (P4)."""

    theme: Theme = field(default_factory=lambda: DEFAULT_THEME)
    ansi: bool = False
    text_buffer: list[str] = field(default_factory=list)
    streaming_text: bool = False
    width: int = 80  # terminal width for output folding (set by the TUI on mount)


# A renderer takes the typed payload and the context, returns a line/string.
EventRenderer = Callable[[Payload, RenderContext], str]

RENDERERS: dict[EventType, EventRenderer] = {}


def register_renderer(event_type: EventType) -> Callable[[EventRenderer], EventRenderer]:
    """Register a renderer for *event_type* (single-source dispatch table)."""

    def deco(fn: EventRenderer) -> EventRenderer:
        RENDERERS[event_type] = fn
        return fn

    return deco


# ── concrete renderers (one per EventType) ───────────────────────────────────

@register_renderer(EventType.TEXT_DELTA)
def _render_text_delta(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, TextDeltaPayload)
    ctx.text_buffer.append(payload.text)
    ctx.streaming_text = True
    return ctx.theme.style(payload.text, "text_muted", ansi=ctx.ansi)


@register_renderer(EventType.THINKING_DELTA)
def _render_thinking(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, ThinkingDeltaPayload)
    return ctx.theme.style(payload.text, "text_muted", ansi=ctx.ansi)


@register_renderer(EventType.TOOL_START)
def _render_tool_start(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, ToolStartPayload)
    return ctx.theme.style(f"… {payload.tool_name}", "tool_running", ansi=ctx.ansi)


@register_renderer(EventType.TOOL_END)
def _render_tool_end(payload: Payload, ctx: RenderContext) -> str:
    # Hand off to the second axis (per-tool renderer); no per-tool if-chain here.
    assert isinstance(payload, ToolEndPayload)
    return get_tool_renderer(payload.tool_name)(payload, ctx)


# Internal compaction ``kind`` → user-facing label (single source of truth, so
# neither the live sink nor the transcript leaks internal terms like
# ``overflow``). The cascade kinds are: ``micro`` (folded tool noise),
# ``overflow``/``meta`` (an LLM meta-summary replaced the head),
# ``truncate``/``overflow_truncate`` (hard truncation — summary unavailable),
# ``clear`` (history wiped). MiMo parity: a calm, plain phrase, no token math.
_COMPACTION_LABELS: dict[str, str] = {
    "micro": "Folded tool output",
    "meta": "Context summarised",
    "overflow": "Context summarised",
    "overflow_compact": "Context summarised",
    "truncate": "Context truncated",
    "overflow_truncate": "Context truncated",
    "clear": "History cleared",
}


def compaction_label(kind: str) -> str:
    """User-facing label for a compaction ``kind`` (defaults to a safe phrase)."""
    return _COMPACTION_LABELS.get(kind, "Context compacted")


@register_renderer(EventType.COMPACTING)
def _render_compacting(payload: Payload, ctx: RenderContext) -> str:
    """A calm, centred boundary line marking where the context was compacted
    (MiMo's ``──── Session compacted ────`` parity). The label is a plain phrase;
    token counts and the internal ``kind`` are deliberately not surfaced — the
    sidebar/footer already shows live token usage. The TUI sink mounts this as a
    full-width Rich ``Rule``; this plain-string form is the fallback used by the
    headless transcript / sub-agent views."""
    assert isinstance(payload, CompactingPayload)
    label = compaction_label(payload.kind)
    body = ctx.theme.style(f"── {label} ──", "text_muted", ansi=ctx.ansi)
    return body


@register_renderer(EventType.GATE_DECISION)
def _render_gate(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, GatePayload)
    suffix = f" — {payload.reason}" if payload.reason else ""
    return f"gate {payload.gate_name}: {payload.decision}{suffix}"


def _kfmt(n: int) -> str:
    """Compact token count: 1234 → 1.2k, 999 → 999."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_duration(ms: float) -> str:
    """Human duration: 123ms / 12s / 2m 3s / 1h 4m."""
    ms = max(0.0, ms)
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 60_000:
        return f"{int(ms / 1000)}s"
    if ms < 3_600_000:
        m, s = divmod(int(ms // 1000), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(ms // 1000), 3600)
    return f"{h}h {rem // 60}m"


@register_renderer(EventType.USAGE)
def _render_usage(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, UsagePayload)
    u = payload.usage
    up = ctx.theme.style(f"↑{_kfmt(u.input_tokens)}", "text_muted", ansi=ctx.ansi)
    down = ctx.theme.style(f"↓{_kfmt(u.output_tokens)}", "text_muted", ansi=ctx.ansi)
    model = ctx.theme.style(payload.model, "text_dim", ansi=ctx.ansi)
    sep = ctx.theme.style(" · ", "text_dim", ansi=ctx.ansi)
    line = f"{up} {down}{sep}{model}"  # gutter from RichLog left padding
    if payload.cost_usd:
        line += sep + ctx.theme.style(f"${payload.cost_usd:.4f}", "accent", ansi=ctx.ansi)
    return line


@register_renderer(EventType.TURN_START)
def _render_turn_start(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, TurnStartPayload)
    marker = ctx.theme.style("▸", "accent", ansi=ctx.ansi, bold=True)
    label = ctx.theme.style(f"turn {payload.turn_index}", "text", ansi=ctx.ansi, bold=True)
    model = ctx.theme.style(payload.model, "text_dim", ansi=ctx.ansi)
    return f"{marker} {label} {model}"


@register_renderer(EventType.TURN_END)
def _render_turn_end(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, TurnEndPayload)
    ctx.streaming_text = False
    ctx.text_buffer.clear()
    return ctx.theme.style(
        f"◂ turn {payload.turn_index} done · {payload.iterations} iter · {payload.stop_cause}",
        "text_dim", ansi=ctx.ansi,
    )


# Per-subagent-type colour role — the SINGLE SOURCE OF TRUTH. The sidebar
# imports this exact mapping (it previously kept a divergent copy where reviewer/
# researcher were grey `info` while here they were amber `accent` — the same
# agent showed two colours in two places). Roles only, never raw hex (P2).
#
# 5-family split (one hue per family, lightness distinguishes siblings):
#   exploration  → scout          (fog-blue)
#   plan+build   → architect/builder  (dark gold / bright gold)
#   verify       → validator/reviewer (dark sage / bright sage)
#   debug+study  → debugger/researcher (dark terracotta / bright terracotta)
#   fallback     → generalist     (neutral grey)
# This replaces the old 3-effective-hue mapping where scout/architect/reviewer/
# researcher all collapsed onto amber. Each type now maps to a dedicated
# agent_* role key (defined in theme.py), decoupled from the syntax/status
# roles so sub-agent colours can be tuned independently later.
SUBAGENT_ROLE: dict[str, str] = {
    "scout": "agent_scout", "architect": "agent_architect", "builder": "agent_builder",
    "validator": "agent_validator", "reviewer": "agent_reviewer", "debugger": "agent_debugger",
    "researcher": "agent_researcher", "generalist": "agent_generalist",
}
# Back-compat alias (the old private name).
_SUBAGENT_ROLE = SUBAGENT_ROLE


def _fmt_elapsed(ms: float) -> str:
    return format_duration(ms) if ms >= 1.0 else ""


def _titlecase(s: str) -> str:
    return s[:1].upper() + s[1:] if s else "General"


@register_renderer(EventType.SUBAGENT_ACTIVITY)
def _render_subagent(payload: Payload, ctx: RenderContext) -> str:
    """Subagent live/terminal line — MiMo ``Task`` parity (index.tsx).

    Form: ``│ {Titlecase} Task`` head; live activity ``↳ {activity} · …``;
    completion footer ``└ {detail} · {elapsed}``; failure renders the head in
    error colour with a ``└`` detail line."""
    assert isinstance(payload, SubagentActivityPayload)
    role = _SUBAGENT_ROLE.get(payload.subagent_type, "accent")
    title = _titlecase(payload.subagent_type)
    # MiMo head: "{Type} Task — {description}" (the short task brief, shown
    # persistently). "" → just "{Type} Task".
    desc = (payload.description or "").strip()
    task_label = f"{title} Task — {desc}" if desc else f"{title} Task"
    status = (payload.status or "").lower()
    # Terminal state keyed on ``status`` when present, else the legacy word.
    is_done = status == "done" or (
        not status and payload.activity.strip().lower() in ("done", "completed", "complete")
    )
    is_failed = status in ("failed", "cancelled")
    if is_failed:
        verb = "cancelled" if status == "cancelled" else "failed"
        head = ctx.theme.style(f"│ {task_label} {verb}", "error", ansi=ctx.ansi)
        tail = ctx.theme.style(f"└ {payload.detail}", "text_muted", ansi=ctx.ansi) if payload.detail else ""
        return f"{head}\n{tail}" if tail else head
    if is_done:
        # Completion footer: structured metrics only — `└ {N} toolcalls · {elapsed}`
        # (MiMo parity). The sub-agent's (truncated) summary is intentionally NOT
        # shown here; the full output lives in the drill-down transcript.
        head = ctx.theme.style(f"│ {task_label}", role, ansi=ctx.ansi)
        bits: list[str] = []
        if payload.tool_count:
            bits.append(f"{payload.tool_count} toolcalls")
        elapsed = _fmt_elapsed(payload.elapsed_ms)
        if elapsed:
            bits.append(elapsed)
        tail = ctx.theme.style(f"└ {' · '.join(bits)}", "text_muted", ansi=ctx.ansi) if bits else ""
        return f"{head}\n{tail}" if tail else head
    # Live activity line: │ {Titlecase} Task — {desc} / ↳ {activity} · turn N · elapsed.
    head = ctx.theme.style(f"│ {task_label}", role, ansi=ctx.ansi)
    bits = [payload.activity] if payload.activity else []
    if payload.turn:
        bits.append(f"turn {payload.turn}")
    elapsed = _fmt_elapsed(payload.elapsed_ms)
    if elapsed:
        bits.append(elapsed)
    if not bits and payload.detail:
        bits.append(payload.detail)
    body = " · ".join(bits)
    tail = ctx.theme.style(f"↳ {body}", "text_muted", ansi=ctx.ansi) if body else ""
    return f"{head}\n{tail}" if tail else head


@register_renderer(EventType.SUBAGENT_CHILD)
def _render_subagent_child(payload: Payload, ctx: RenderContext) -> str:
    # SUBAGENT_CHILD is normally consumed by the TUI sink into a per-job drill-down
    # transcript, NOT drawn into the main scrollback. This renderer exists so the
    # registry stays total (one renderer per EventType); if ever rendered directly
    # it shows the owning agent + the inner event's own card, dimmed.
    assert isinstance(payload, SubagentChildPayload)
    inner = render_event(payload.inner, ctx)
    return ctx.theme.style(f"  ⌍ [{payload.subagent_type}] {inner}", "text_dim", ansi=ctx.ansi)


@register_renderer(EventType.ERROR)
def _render_error(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, ErrorPayload)
    if payload.recoverable:
        tag = "recoverable"
    elif payload.error_class == "retryable":
        tag = "retry exhausted"
    else:
        tag = "terminal"
    role = "warning" if payload.recoverable else "error"
    code = f" {payload.platform_code}" if payload.platform_code is not None else ""
    return ctx.theme.style(
        f"✗ error [{payload.error_class}/{tag}{code}]: {payload.message}",
        role,
        ansi=ctx.ansi,
    )


@register_renderer(EventType.RETRY)
def _render_retry(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, RetryPayload)
    destination = payload.next_model or payload.next_transport
    nxt = f" → {destination}" if destination else ""
    wait = (
        f" in {payload.delay_seconds:.1f}s"
        if payload.delay_seconds is not None
        else ""
    )
    code = f" [{payload.platform_code}]" if payload.platform_code is not None else ""
    return ctx.theme.style(
        f"↻ retry #{payload.attempt}{code}{wait}: {payload.reason}{nxt}",
        "warning",
        ansi=ctx.ansi,
    )


@register_renderer(EventType.APPROVAL)
def _render_approval(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, ApprovalPayload)
    reason = f" ({payload.reason})" if payload.reason else ""
    return ctx.theme.style(
        f"⤷ approve {payload.tool_name}: {payload.summary}{reason}", "warning", ansi=ctx.ansi
    )


@register_renderer(EventType.REQUEST)
def _render_request(payload: Payload, ctx: RenderContext) -> str:
    assert isinstance(payload, RequestPayload)
    return ctx.theme.style(
        f"→ request turn {payload.turn_index} · {payload.model} · max_tokens={payload.max_tokens}",
        "text_dim", ansi=ctx.ansi,
    )


def _default_renderer(event: StreamEvent, ctx: RenderContext) -> str:
    """Structured fallback for an unregistered EventType (forward compat, P8)."""
    return f"[{event.type.value}] {event.payload!r}"


DEFAULT_RENDERER: Callable[[StreamEvent, RenderContext], str] = _default_renderer


def render_event(event: StreamEvent, ctx: RenderContext) -> str:
    """Render *event* by dict lookup on its EventType — no if-chain (R1/D3)."""
    renderer = RENDERERS.get(event.type)
    if renderer is None:
        return DEFAULT_RENDERER(event, ctx)
    return renderer(event.payload, ctx)


# Events that the LIVE main path (TuiEventSink) never commits to the scrollback,
# so the sub-agent transcript must skip them too — otherwise a drilled-into
# sub-agent would show turn markers / a bare USAGE line / gate-decision rows that
# the main conversation never displays (format parity, mirrors tui_sink). USAGE
# is folded into the app's turn footer (not shown per-event); TURN_*/REQUEST/
# GATE_DECISION are mechanical/transport detail. TOOL_START is the transient
# "running" indicator (also dropped live). COMPACTING is NOT skipped: it renders
# as a calm boundary divider in BOTH the live conversation and the transcript so
# the two stay consistent (MiMo's persistent "Session compacted" part parity).
_TRANSCRIPT_SKIP: frozenset[EventType] = frozenset(
    {
        EventType.TURN_START,
        EventType.TURN_END,
        EventType.USAGE,
        EventType.REQUEST,
        EventType.GATE_DECISION,
        EventType.TOOL_START,
    }
)


# Tool-collapse rule mirrored from TuiEventSink (the main path) so the sub-agent
# transcript folds the SAME tool cards the live conversation folds: ``bash`` (or
# any unregistered generic/MCP tool) whose output exceeds the line threshold,
# plus the curated always-collapse cards (todos / ask / edit-write diff / jobs)
# below. Inline tools (Read/Grep) and Agent never fold.
_COLLAPSE_TOOLS: frozenset[str] = frozenset({"bash", "Bash"})
# Mirror of ``TuiEventSink._ALWAYS_COLLAPSE_TOOLS`` so an exported / replayed
# transcript folds exactly the same curated cards the live view does.
_ALWAYS_COLLAPSE_TOOLS: frozenset[str] = frozenset(
    {
        "todo_write", "TodoWrite",
        "AskUserQuestion",
        "edit_file", "Edit", "write_file", "Write", "MultiEdit",
        "JobOutput", "JobList",
        "read_image",
    }
)


@dataclass(frozen=True)
class TranscriptBlock:
    """One renderable block of a sub-agent transcript.

    ``kind`` selects the mounting widget in :class:`AgentView`:
    ``"markdown"`` (assistant prose → native Markdown), ``"thinking"`` (muted
    reasoning), ``"ansi"`` (a pre-styled card line, e.g. a compact tool card),
    or ``"collapsible"`` (a foldable card — thinking or a long tool block —
    mounted as a Textual ``Collapsible`` so the user can click to expand/collapse
    in place, exactly like the main conversation).

    For ``"collapsible"`` blocks: ``title`` is the (console-markup) header shown
    when folded, ``text`` is the expandable body, and ``css_class`` is an optional
    CSS class (``tool-block`` for tool cards) applied to the ``Collapsible``.
    ``thinking_body`` marks whether the body is muted reasoning (vs ANSI tool
    output) so the view styles it correctly. ``metadata`` carries optional replay
    hints for special blocks such as persisted sub-agent cards.
    """

    kind: str
    text: str
    title: str = ""
    css_class: str = ""
    thinking_body: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


def render_transcript_blocks(
    events: list[StreamEvent], ctx: RenderContext
) -> list[TranscriptBlock]:
    """Render a captured sub-agent event stream into typed display blocks.

    Mirrors the live sink's accumulate/flush rhythm AND its event filtering so a
    sub-agent's context reads EXACTLY like the main conversation: consecutive
    ``TEXT_DELTA`` chunks coalesce into one ``markdown`` block, ``THINKING_DELTA``
    into a ``thinking`` block, ``TOOL_END`` and other cards into ``ansi`` blocks
    via the shared registry. Trace-only / usage / turn-marker events are skipped
    (see :data:`_TRANSCRIPT_SKIP`). Pure: no widget calls, no I/O (P3)."""
    blocks: list[TranscriptBlock] = []
    buf: list[str] = []
    buf_kind: str | None = None

    def flush() -> None:
        nonlocal buf, buf_kind
        if not buf:
            return
        text = "".join(buf).strip()
        kind = buf_kind
        buf = []
        buf_kind = None
        if not text:
            return
        if kind == "thinking":
            # Foldable "Thought" card (collapsed by default), mirroring the main
            # path's _write_thinking → write_collapsible. The transcript can't
            # recover the live elapsed time, so the title omits the "· Ns" suffix.
            blocks.append(
                TranscriptBlock(
                    "collapsible", text, title="Thought", thinking_body=True
                )
            )
        else:
            # Assistant answer prose → markdown (parity with the main path's
            # write_markdown), not pre-styled ANSI.
            blocks.append(TranscriptBlock("markdown", text))

    for event in events:
        if event.type is EventType.TEXT_DELTA:
            if buf_kind not in (None, "text"):
                flush()
            buf_kind = "text"
            buf.append(getattr(event.payload, "text", ""))
            continue
        if event.type is EventType.THINKING_DELTA:
            if buf_kind not in (None, "thinking"):
                flush()
            buf_kind = "thinking"
            buf.append(getattr(event.payload, "text", ""))
            continue
        # Any structured (non-delta) event is a boundary: flush prose first.
        flush()
        # Skip the events the main path never commits (parity — no turn markers,
        # bare USAGE line, gate rows, etc. leaking into the drill-down view).
        if event.type in _TRANSCRIPT_SKIP:
            continue
        try:
            line = render_event(event, ctx)
        except Exception:  # noqa: BLE001 — one bad card must not break the view
            line = f"[{event.type.value}]"
        if not line.strip():
            continue
        # Fold a long tool block into a collapsible card — same rule and shape as
        # the main path's _write_tool_collapsible (title = first line + "· N
        # lines", body = full untruncated output, tool-block styling).
        block = _maybe_tool_collapsible(event, line)
        if block is not None:
            blocks.append(block)
            continue
        # Single-line tool results stay inline ansi cards (parity with the main
        # path's ``tool-line`` marker) so the web snapshot can map them to
        # ``cls: inline tool`` and the frontend's turn fold can count them.
        css = "tool-line" if event.type is EventType.TOOL_END else ""
        blocks.append(TranscriptBlock("ansi", line, css_class=css))
    flush()
    return blocks


def render_history_blocks(
    messages: list[object], ctx: RenderContext
) -> list[TranscriptBlock]:
    """Render a stored ``list[Message]`` into display blocks for session replay.

    Session-rail switching (docs/architecture/frontend-adapters.md §5.3) must repaint a session's
    past conversation in ``#log`` identically to the live view. This adapts the
    persisted message format into the SAME event stream the live sink produces,
    then defers to :func:`render_transcript_blocks` — so replay and live rendering
    share one path (P1). User turns are not stream events (the live path injects
    them as ``.user-echo`` cards), so they surface here as ``kind="user"`` blocks
    the caller mounts as echo cards; tool results (carried in user-role messages)
    are paired back to their ``tool_use`` and emitted as ``TOOL_END`` so they fold
    exactly like the live tool cards. Pure: no widget calls, no I/O (P3)."""
    from autoverse.types.events import make_event
    from autoverse.types.messages import (
        ImageBlock,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    blocks: list[TranscriptBlock] = []
    pending: list[StreamEvent] = []
    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, dict[str, object]] = {}

    def tool_image_metadata(md: dict[str, object], tool_name: str) -> dict[str, object] | None:
        images = md.get("images")
        if not isinstance(images, (list, tuple)):
            return None
        kept = [data for data in images[:8] if isinstance(data, str) and data]
        if not kept:
            return None
        media_type = str(md.get("image_media_type") or "image/png")
        if not media_type.startswith("image/"):
            media_type = "image/png"
        return {
            "images": kept,
            "image_media_type": media_type,
            "label": f"{tool_name} screenshot",
        }

    def flush() -> None:
        if pending:
            blocks.extend(render_transcript_blocks(pending, ctx))
            pending.clear()

    for msg in messages:
        role = getattr(msg, "role", "")
        content = getattr(msg, "content", ())
        if role == "assistant":
            for b in content:
                if isinstance(b, TextBlock):
                    if b.text:
                        pending.append(make_event(TextDeltaPayload(b.text)))
                elif isinstance(b, ThinkingBlock):
                    if b.text:
                        pending.append(make_event(ThinkingDeltaPayload(b.text)))
                elif isinstance(b, ToolUseBlock):
                    tool_names[b.id] = b.name
                    tool_inputs[b.id] = dict(b.input)
            continue
        if role == "user":
            text_parts: list[str] = []
            attachments: list[dict[str, str]] = []
            for b in content:
                if isinstance(b, ToolResultBlock):
                    # Replay must rebuild the SAME rich card the live sink drew:
                    # every per-tool renderer reads its label (path, command,
                    # pattern, diff, todos, …) from ``ToolEndPayload.metadata``,
                    # which the engine persisted onto ``ToolResultBlock.metadata``
                    # (turn_loop._to_result_blocks). Dropping it here is why a
                    # backgrounded session's tools came back as bare labels
                    # (docs/architecture/frontend-adapters.md §5.3 — replay is the only render path
                    # for a turn that ran detached, §5.6.3). ``error_message`` /
                    # ``error_kind`` mirror dispatcher._emit_end so benign vs
                    # hard errors fold identically to the live view.
                    md = dict(b.metadata) if b.metadata else {}
                    tool_name = tool_names.get(b.tool_use_id, "tool")
                    event = make_event(
                        ToolEndPayload(
                            tool_use_id=b.tool_use_id,
                            tool_name=tool_name,
                            output=str(b.content),
                            is_error=bool(b.is_error),
                            error_kind=str(md.get("error_kind", "")),
                            error_message=str(b.content) if b.is_error else "",
                            metadata=md,
                        )
                    )
                    if tool_name == "ExitPlanMode" and not b.is_error:
                        plan = str(md.get("plan") or "").strip()
                        if plan:
                            flush()
                            head = ctx.theme.style(
                                "◔ Plan proposed", "success", ansi=ctx.ansi
                            )
                            blocks.append(TranscriptBlock("ansi", head))
                            blocks.append(TranscriptBlock("markdown", plan))
                            continue
                    if tool_name in ("agent", "Agent"):
                        flush()
                        try:
                            line = render_event(event, ctx)
                        except Exception:  # noqa: BLE001 - one bad replay card
                            line = "[agent]"
                        req = tool_inputs.get(b.tool_use_id, {})
                        job_id = f"history-agent:{b.tool_use_id}"
                        blocks.append(
                            TranscriptBlock(
                                "agent",
                                line,
                                metadata={
                                    "job_id": job_id,
                                    "subagent_type": str(md.get("subagent_type") or "general"),
                                    "description": str(md.get("description") or ""),
                                    "task": str(req.get("task") or ""),
                                    "status": "failed" if b.is_error else "done",
                                    "tool_count": md.get("tool_count", 0),
                                    "summary": str(b.content),
                                },
                            )
                        )
                        continue
                    pending.append(event)
                    if not b.is_error:
                        img_md = tool_image_metadata(md, tool_name)
                        if img_md is not None:
                            flush()
                            blocks.append(
                                TranscriptBlock("tool_images", "", metadata=img_md)
                            )
                elif isinstance(b, TextBlock):
                    if b.text:
                        text_parts.append(b.text)
                elif isinstance(b, ImageBlock):
                    attachments.append(
                        {
                            "media_type": b.media_type,
                            "data": b.data,
                            "source_type": b.source_type,
                        }
                    )
            real = "\n".join(t for t in text_parts if t.strip())
            meta = getattr(msg, "metadata", {}) or {}
            if real.strip() or attachments:
                flush()  # close prior assistant span before the user echo
                user_metadata: dict[str, object] = {}
                if attachments:
                    user_metadata["attachments"] = attachments
                if isinstance(meta, dict) and meta.get("rewind_id"):
                    user_metadata["rewind_id"] = str(meta["rewind_id"])
                blocks.append(
                    TranscriptBlock(
                        "user",
                        real,
                        metadata=user_metadata,
                    )
                )
            if isinstance(meta, dict):
                card = meta.get("image_read_card")
                if isinstance(card, dict):
                    title = str(card.get("title") or "read_image")
                    body = str(card.get("body") or "")
                    if body:
                        blocks.append(
                            TranscriptBlock(
                                "collapsible",
                                body,
                                title=f"◉ {title}",
                                css_class="tool-block",
                                metadata={
                                    "collapsed": not bool(card.get("is_error")),
                                },
                            )
                        )
        # system messages are not part of the visible transcript — skip.
    flush()
    return blocks


def _maybe_tool_collapsible(
    event: StreamEvent, rendered: str
) -> "TranscriptBlock | None":
    """Return a foldable tool block if *event* is a collapsible multi-line
    TOOL_END, else ``None``. Mirrors ``TuiEventSink._is_collapsible_tool`` /
    ``_write_tool_collapsible`` so the transcript folds exactly what the live
    conversation folds (P3: pure, no widget calls)."""
    if event.type is not EventType.TOOL_END:
        return None
    payload = event.payload
    if not isinstance(payload, ToolEndPayload):
        return None
    name = payload.tool_name
    output = str(payload.output or "")
    from autoverse.frontends.repl.tool_renderer import TOOL_RENDERERS
    from autoverse.frontends.repl.tool_error import (
        error_text,
        is_benign_error,
        is_hard_error,
    )

    first, _, _ = rendered.partition("\n")
    head = Text.from_ansi(first).plain.replace("┃", "").strip()

    # A *hard* (non-benign) error on ANY tool folds into an expandable card. The
    # failed tool head is struck through with a red ``Error (N lines)`` cue and
    # the body holds the full error text — MiMo ``CollapsibleError`` parity
    # (matches the live sink path). No ``tool-block`` class: the grey wash + ``│``
    # left bar is the block-tool frame and looks wrong on a one-line error.
    if is_hard_error(payload):
        full = error_text(payload)
        n = len(full.splitlines()) or 1
        label = f"Error ({n} {'line' if n == 1 else 'lines'})"
        title = (
            f"[$av-text-muted][strike]{head}[/strike][/$av-text-muted]"
            f"  [$av-tool-err]· {label}[/$av-tool-err]"
        )
        return TranscriptBlock(
            "collapsible", full or rendered, title=title
        )

    if is_benign_error(payload) and error_text(payload).strip():
        full = error_text(payload)
        n = len(full.splitlines()) or 1
        label = f"Details ({n} {'line' if n == 1 else 'lines'})"
        title = (
            f"[$av-text-muted][strike]{head}[/strike][/$av-text-muted]"
            f"  [$av-text-dim]· {label}[/$av-text-dim]"
        )
        return TranscriptBlock("collapsible", full, title=title)

    is_multiline = "\n" in rendered

    # Curated always-collapse cards (todos / ask / edit-write diff / jobs): the
    # head is the title and the remaining rendered rows are the (ANSI-styled)
    # body — mirrors the live sink's always-collapse branch. A single-line render
    # (e.g. a Write with no diff, an empty JobList) stays inline.
    if name in _ALWAYS_COLLAPSE_TOOLS and is_multiline:
        _, _, rest = rendered.partition("\n")
        body_lines = [
            ln.partition("┃")[2].lstrip() if "┃" in ln else ln
            for ln in rest.splitlines()
        ]
        title = f"[$av-text-muted]{head}[/$av-text-muted]"
        return TranscriptBlock(
            "collapsible", "\n".join(body_lines), title=title, css_class="tool-block"
        )

    # Curated (registered) tools render their own compact card and never fold;
    # unregistered (generic/MCP) tools and bash fold whenever their rendered
    # output is multi-line (a single line stays inline).
    known = name in TOOL_RENDERERS
    if not (is_multiline and (name in _COLLAPSE_TOOLS or not known)):
        return None
    n = output.count("\n") + 1 if output else 0
    title = f"[$av-text-muted]{head}" + (f"  · {n} lines" if n else "") + "[/$av-text-muted]"
    return TranscriptBlock(
        "collapsible", output or rendered, title=title, css_class="tool-block"
    )


def render_transcript(events: list[StreamEvent], ctx: RenderContext) -> list[str]:
    """Back-compat string view of :func:`render_transcript_blocks`.

    Flattens typed blocks to display strings: ``thinking`` is muted-styled,
    ``markdown`` keeps its raw text, ``ansi`` passes through. Front-ends that can
    mount a real Markdown widget should call :func:`render_transcript_blocks`."""
    out: list[str] = []
    for b in render_transcript_blocks(events, ctx):
        if b.kind == "thinking":
            out.append(ctx.theme.style(b.text, "text_muted", ansi=ctx.ansi))
        elif b.kind == "markdown":
            out.append(ctx.theme.style(b.text, "text", ansi=ctx.ansi))
        else:
            out.append(b.text)
    return out


__all__ = [
    "RenderContext",
    "EventRenderer",
    "RENDERERS",
    "DEFAULT_RENDERER",
    "register_renderer",
    "render_event",
    "render_transcript",
    "render_transcript_blocks",
    "render_history_blocks",
    "TranscriptBlock",
    "SUBAGENT_ROLE",
]
