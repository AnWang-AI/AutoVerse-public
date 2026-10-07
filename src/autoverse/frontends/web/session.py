"""WebSession + SessionManager — the engine-facing half of the WebUI backend.

A :class:`WebSession` is the Web analogue of :class:`LineRepl`:
it wires the engine to a reused :class:`TuiEventSink` over a :class:`WebLog`
surface (so blocks + derived state come for free), drives turns, replays history
via :func:`render_history_blocks`, dispatches the shared ``CommandRegistry``,
and handles plan-mode approval exactly like the line tier. Multi-session
management reuses :class:`StoreSessionQuery` for the rail and a shared
``JSONLStore`` for engine builds (docs/architecture/frontend-adapters.md §9.3).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
import os
import shutil
import tempfile
import time
import uuid
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.frontends.repl.plan_approval import approved_plan_prompt
from autoverse.frontends.repl.renderer import (
    RenderContext,
    TranscriptBlock,
    _kfmt,
    format_duration,
    render_history_blocks,
    render_transcript_blocks,
)
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.frontends.repl.tui_sink import TuiEventSink, subagent_meta
from autoverse.frontends.web.bridge import SessionBridge
from autoverse.frontends.web.files import referenced_external_preview_paths
from autoverse.frontends.web.markup_html import markup_to_html, renderable_to_html
from autoverse.frontends.web.web_log import WebLog, _card_class, _line_class
from autoverse.core.loop import worktree_mode
from autoverse.infra.observ.logging import log_context
from autoverse.ports import SecretProvider, SessionHistoryPort, SessionMeta
from autoverse.types import AbortController, EventType, ImageBlock, Message
from autoverse.types.events import (
    ApprovalPayload,
    ErrorPayload,
    ToolEndPayload,
    TurnEndPayload,
    make_event,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import RenderableType

    from autoverse.app.context import AppContext

# (sink, permissions, session_id) -> EngineHandle. The manager supplies it so the
# engine is wired with the live WebLog sink + the web approval adapter.
EngineFactory = Callable[[Any, Any, str], Awaitable[Any]]
ImageAttachments = tuple[ImageBlock, ...]

_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_HISTORY_PAGE_CACHE_LIMIT = 32


def _browser_page_ids(value: object) -> list[str]:
    """Return a bounded list of host-generated ids for an LLM state notice."""

    if not isinstance(value, list):
        return []
    return [str(item) for item in value[:20] if isinstance(item, str) and item]


def _tool_image_block_from_metadata(
    metadata: dict[str, object], *, label: str = "tool image"
) -> dict[str, Any] | None:
    """Build a WebUI-safe image strip from tool-produced image metadata."""
    raw_images = metadata.get("images")
    if not isinstance(raw_images, (list, tuple)):
        return None
    media_type = str(metadata.get("image_media_type") or "image/png")
    if not media_type.startswith("image/"):
        media_type = "image/png"
    images = [
        {"media_type": media_type, "data": data, "label": label}
        for data in raw_images[:8]
        if isinstance(data, str) and data
    ]
    if not images:
        return None
    return {"kind": "tool_images", "images": images}


# ── status glyphs for the sidebar Agents panel / AgentView title ─────────────
_STATUS_GLYPH = {"running": "●", "done": "✓", "completed": "✓", "failed": "✗", "cancelled": "✗"}
_LOG = logging.getLogger(__name__)


def _ansi_to_html(ansi: str) -> str:
    from rich.text import Text

    return renderable_to_html(Text.from_ansi(ansi))


def transcript_block_to_block(
    b: TranscriptBlock, ctx: RenderContext, theme: Theme
) -> dict[str, Any]:
    """Convert one :class:`TranscriptBlock` to a frontend block dict."""
    if b.kind == "user":
        out: dict[str, Any] = {"kind": "user", "text": b.text}
        metadata = b.metadata or {}
        attachments = metadata.get("attachments")
        if isinstance(attachments, list):
            out["attachments"] = attachments
        if metadata.get("rewind_id"):
            out["rewind_id"] = str(metadata["rewind_id"])
        return out
    if b.kind == "tool_images":
        block = _tool_image_block_from_metadata(
            b.metadata or {}, label=str((b.metadata or {}).get("label") or "tool image")
        )
        return block or {"kind": "line", "html": "", "cls": "inline"}
    if b.kind == "markdown":
        return {"kind": "markdown", "md": b.text, "streaming": False}
    if b.kind == "thinking":
        from html import escape

        return {
            "kind": "collapsible",
            "title_html": "Thought",
            "body_html": escape(b.text),
            "body_kind": "text",
            "card_cls": "thought",
            "collapsed": True,
        }
    if b.kind == "collapsible":
        title_plain = b.title if "[" not in b.title else b.title
        title_html = markup_to_html(b.title, theme) if "[" in b.title else b.title
        if b.thinking_body:
            from html import escape

            body_html = escape(b.text)
            body_kind = "text"
        else:
            body_html = _ansi_to_html(b.text)
            body_kind = "html"
        return {
            "kind": "collapsible",
            "title_html": title_html,
            "body_html": body_html,
            "body_kind": body_kind,
            "card_cls": _card_class(_strip_markup(b.title), b.css_class),
            "collapsed": bool((b.metadata or {}).get("collapsed", True)),
        }
    if b.kind == "agent":
        from rich.text import Text

        meta = b.metadata or {}
        lines = Text.from_ansi(b.text).plain.split("\n")
        status = str(meta.get("status", "done"))
        return {
            "kind": "subagent",
            "job": str(meta.get("job_id", "")),
            "head_text": lines[0] if lines else "",
            "foot_text": lines[1] if len(lines) > 1 else "",
            "role": str(meta.get("subagent_type", "generalist")),
            "failed": status in ("failed", "cancelled"),
            "done": status != "running",
        }
    # ansi (compact tool/inline card) — pre-styled string.
    from rich.text import Text

    plain = Text.from_ansi(b.text).plain
    cls = (
        "inline tool"
        if b.css_class == "tool-line"
        else _line_class(plain.lstrip())
    )
    return {"kind": "line", "html": _ansi_to_html(b.text), "cls": cls}


def _strip_markup(s: str) -> str:
    import re

    return re.sub(r"\[[^\]]*\]", "", s)


class WebSession:
    def __init__(
        self,
        session_id: str,
        engine: Any | None,
        sink: TuiEventSink,
        bridge: SessionBridge,
        permission: Any,
        ctx: RenderContext,
        *,
        registry: Any,
        theme: Theme,
        model: str,
        cwd: str,
        version: str,
        context_window: int,
        store: Any = None,
        session_catalog: Any = None,
        session_history: SessionHistoryPort | None = None,
        trace_store: Any = None,
    ) -> None:
        self.session_id = session_id
        self.engine = engine
        self.sink = sink
        self.bridge = bridge
        self.permission = permission
        self.ctx = ctx
        self.store = store
        self.session_catalog = session_catalog
        self.session_history = session_history
        self.trace_store = trace_store
        self.registry = registry
        self.theme = theme
        self.model = model
        self.cwd = cwd
        self.version = version
        self.context_window = context_window
        self.app_ctx = getattr(engine, "app", None)
        self._history_messages: tuple[Message, ...] = ()
        self._history_complete = True
        self._history_pages: dict[str, tuple[dict[str, Any], SessionBridge]] = {}
        self._history_load_ms = 0.0
        self._activation_ms = 0.0
        self._engine_history_complete = True
        self._lock = asyncio.Lock()
        self._running = False
        self._turn_started_at_ms: float | None = None
        self._turn_started_monotonic: float | None = None
        self._turn_paused_elapsed_ms = 0.0
        self._turn_pause_started_monotonic: float | None = None
        # Live phase shown in the Web status bar: the human label of the step the
        # turn is on right now (thinking / writing / a named tool) plus the
        # wall-clock when *that step* began, so the bar can time the current
        # operation rather than the whole turn. Reset at every turn boundary.
        self._activity_label = ""
        self._activity_started_at_ms: float | None = None
        self._activity_started_monotonic: float | None = None
        self._activity_paused_elapsed_ms = 0.0
        self._activity_pause_started_monotonic: float | None = None
        self._approval_waits: set[str] = set()
        # Phase tracking is derived from the events THIS consumer dequeues, not
        # from ``self.sink.activity``. ``sink.activity`` is correct (it clears on
        # TOOL_END — see the tui_sink tests) but it is the *producer's* LIVE
        # state: the engine drives the turn on a separate task and fills an
        # unbounded queue, so the producer routinely runs ahead (e.g. has already
        # emitted TOOL_START(TodoWrite)) while we are still consuming earlier
        # events. Peeking at the live value made the bar jump to whatever the
        # producer was doing — showing "TodoWrite" while the streamed
        # conversation hadn't reached it / had already passed it. So we mirror
        # the sink's active map HERE, advanced strictly by the events we consume,
        # keeping the bar locked to the conversation the user is watching stream.
        # ``_active_ops`` = running tool/sub-agent ops (most-recent wins);
        # ``_stream_phase`` = latest thinking/writing phase. Reset each turn.
        self._active_ops: dict[str, str] = {}
        self._active_op_names: dict[str, str] = {}
        self._stream_phase = ""
        self._last_image_read_card: dict[str, object] | None = None
        self._interaction = 0
        self._uid = 0
        # Block id of the most recent plan-approval card, so ``resolve_plan`` can
        # mark it decided (its buttons render locked, and stay locked across a
        # reconnect/snapshot reload — the card itself is kept as the plan record).
        self._plan_block_id: str | None = None
        self._tps = 0.0
        self._cur_task: asyncio.Task[None] | None = None
        # Follow-up messages submitted while a turn is running. Drained in order
        # once the active turn (and each subsequent queued turn) finishes (R15).
        self._queue: list[dict[str, Any]] = []
        # Pending AskUserQuestion futures, keyed by question_id (gap12/B1).
        self._ask_futures: dict[str, asyncio.Future[list[str]]] = {}
        bridge.meta = self._meta()

    def _require_app_ctx(self) -> "AppContext":
        if self.app_ctx is None:
            raise RuntimeError("session engine is not active")
        return cast("AppContext", self.app_ctx)

    def _require_engine(self) -> Any:
        if self.engine is None:
            raise RuntimeError("session engine is not active")
        return self.engine

    @property
    def is_active(self) -> bool:
        """Whether runtime/provider/tool assembly has completed."""
        return self.engine is not None and self.app_ctx is not None

    def bind_engine(self, engine: Any) -> None:
        """Attach a lazily-built engine to this already-visible view session."""
        self.engine = engine
        self.app_ctx = getattr(engine, "app", None)
        app_model = str(getattr(self.app_ctx, "model", "") or "")
        if app_model:
            self.model = app_model
        app_cwd = getattr(self.app_ctx, "cwd", None)
        if app_cwd is not None:
            self.cwd = str(app_cwd)
        self.bridge.meta = self._meta()

    async def ensure_engine_history(self) -> None:
        """Synchronize a tail-seeded engine before non-turn history mutation."""
        if getattr(self, "_engine_history_complete", True):
            return
        sync = getattr(self._require_engine(), "sync_history", None)
        if callable(sync):
            await sync()
        self._engine_history_complete = True

    # ── id helpers for session-pushed blocks (distinct from WebLog's b{n}) ────
    def _sid(self, prefix: str) -> str:
        self._uid += 1
        return f"{prefix}{self._uid}"

    def _meta(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "cwd": self.cwd,
            "cwd_short": _short_cwd(self.cwd),
            "version": self.version,
            "context_window": self.context_window,
        }

    def external_file_preview_paths(self) -> frozenset[Path]:
        """Return external files explicitly surfaced in this session's UI."""

        sources: list[object] = [self.bridge.blocks]
        sources.extend(
            page_bridge.blocks
            for _page, page_bridge in self._history_pages.values()
        )
        supplemental = [
            _rel(path, self.cwd)
            for path, status, _added, _removed in self.sink.changed_files
            if status != "deleted"
        ]
        supplemental.extend(self._memory_files())
        return referenced_external_preview_paths(
            Path(self.cwd),
            sources,
            supplemental_paths=supplemental,
        )

    # ── status / state publishing ─────────────────────────────────────────────
    def _publish_status(self) -> None:
        # Surface the phase label derived by ``_track_activity`` from the events
        # THIS consumer has dequeued. We deliberately do NOT fall back to
        # ``self.sink.activity`` here: that is the *producer's* live state, which
        # runs ahead of the streamed conversation, so any status frame pushed
        # from a non-event path (turn start, footer refresh, queue edit) would
        # otherwise surface a tool the user hasn't seen stream yet (the lingering
        # "TodoWrite"). ``_activity_label`` is the single consumer-side source.
        activity_label = getattr(self, "_activity_label", "")
        activity_started = getattr(self, "_activity_started_at_ms", None)
        label = activity_label or "Responding…"
        turn_started = getattr(self, "_turn_started_monotonic", None)
        activity_started_monotonic = getattr(
            self, "_activity_started_monotonic", None
        )
        timer_paused = self._timer_paused()
        self.bridge.publish_status(
            {
                "running": self._running,
                # Wall-clock epoch lets a newly connected/refreshed browser
                # resume the same elapsed timer instead of restarting at 0s.
                "started_at_ms": self._turn_started_at_ms if self._running else None,
                "elapsed_ms": (
                    self._elapsed_ms(
                        turn_started,
                        getattr(self, "_turn_paused_elapsed_ms", 0.0),
                        getattr(self, "_turn_pause_started_monotonic", None),
                    )
                    if self._running
                    else None
                ),
                # Per-phase start: when the *current* operation began, so the bar
                # times the active step rather than the whole turn.
                "activity_started_at_ms": activity_started if self._running else None,
                "activity_elapsed_ms": (
                    self._elapsed_ms(
                        activity_started_monotonic,
                        getattr(self, "_activity_paused_elapsed_ms", 0.0),
                        getattr(self, "_activity_pause_started_monotonic", None),
                    )
                    if self._running
                    else None
                ),
                "timer_paused": timer_paused if self._running else False,
                "interaction": self._interaction,
                "label": label,
                # Mirror the consumer-derived label (NOT live sink.activity) so
                # every field on the frame stays in step with the streamed events.
                "activity": activity_label,
                # Pending follow-ups (R15): the client renders these as cancelable
                # chips and flips the composer button between Send ↔ Stop.
                "queued": [self._queue_item_for_client(q) for q in self._queue],
            }
        )

    @staticmethod
    def _elapsed_ms(
        started: float | None,
        paused_elapsed_ms: float = 0.0,
        pause_started: float | None = None,
    ) -> float:
        if started is None:
            return 0.0
        now = time.monotonic()
        elapsed = (now - started) * 1000.0 - paused_elapsed_ms
        if pause_started is not None:
            elapsed -= (now - pause_started) * 1000.0
        return max(elapsed, 0.0)

    def _timer_paused(self) -> bool:
        return getattr(self, "_turn_pause_started_monotonic", None) is not None

    def _turn_elapsed_ms(self) -> float:
        return self._elapsed_ms(
            getattr(self, "_turn_started_monotonic", None),
            getattr(self, "_turn_paused_elapsed_ms", 0.0),
            getattr(self, "_turn_pause_started_monotonic", None),
        )

    def _pause_timers_for_approval(self, call_id: str) -> None:
        if not getattr(self, "_running", False):
            return
        waits: set[str] = getattr(self, "_approval_waits", set())
        if not isinstance(waits, set):
            waits = set()
        first_wait = not waits
        waits.add(call_id)
        self._approval_waits = waits
        if first_wait:
            now = time.monotonic()
            self._turn_pause_started_monotonic = now
            self._activity_pause_started_monotonic = now
        self._publish_status()

    def _resume_timers_for_approval(self, call_id: str) -> None:
        waits: set[str] = getattr(self, "_approval_waits", set())
        if isinstance(waits, set):
            waits.discard(call_id)
            self._approval_waits = waits
            if waits:
                return
        turn_pause_started = getattr(self, "_turn_pause_started_monotonic", None)
        if turn_pause_started is None:
            return
        now = time.monotonic()
        paused_delta_ms = max((now - turn_pause_started) * 1000.0, 0.0)
        self._turn_paused_elapsed_ms = (
            getattr(self, "_turn_paused_elapsed_ms", 0.0) + paused_delta_ms
        )
        self._turn_pause_started_monotonic = None
        activity_pause_started = getattr(
            self, "_activity_pause_started_monotonic", None
        )
        if activity_pause_started is not None:
            activity_delta_ms = max((now - activity_pause_started) * 1000.0, 0.0)
            self._activity_paused_elapsed_ms = (
                getattr(self, "_activity_paused_elapsed_ms", 0.0)
                + activity_delta_ms
            )
        self._activity_pause_started_monotonic = None
        if getattr(self, "_running", False):
            self._publish_status()

    @staticmethod
    def _queue_item_for_client(item: dict[str, Any]) -> dict[str, Any]:
        attachments = tuple(item.get("attachments", ()))
        return {
            "id": str(item.get("id", "")),
            "text": str(item.get("text", "")),
            "attachment_count": len(attachments),
        }

    def _track_activity(self, event: Any) -> None:
        """Advance the live phase label + per-step timer from THIS consumed event.

        Driven by the event the consumer is processing — NOT by reading
        ``self.sink.activity`` (the producer's live state, which races ahead of
        what we're streaming and would surface a tool the conversation hasn't
        reached yet). The active-op map mirrors the sink's logic (TOOL_START adds,
        TOOL_END removes, most-recent running op wins) so a finished tool never
        lingers and a still-running sibling is revealed. A status frame is pushed
        only when the resolved label changes, re-anchoring the per-step timer."""
        etype = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        changed = False
        if etype is EventType.TOOL_START:
            name = getattr(payload, "tool_name", "")
            # The ``agent`` tool is represented by its richer SUBAGENT_ACTIVITY
            # stream (mirrors the sink); skip its bare tool row here.
            if name not in ("agent", "Agent"):
                tid = getattr(payload, "tool_use_id", "") or name
                tool_input = getattr(payload, "tool_input", {}) or {}
                key = f"tool:{tid}"
                self._active_ops.pop(key, None)
                self._active_op_names.pop(key, None)
                self._active_ops[key] = self._tool_label(name, tool_input)
                self._active_op_names[key] = name
                # Entering a tool ends the current thinking/writing phase, so the
                # gap after the tool falls back to neutral, not a stale phase.
                self._stream_phase = ""
                changed = True
        elif etype is EventType.TOOL_END:
            name = getattr(payload, "tool_name", "")
            if name not in ("agent", "Agent"):
                tid = getattr(payload, "tool_use_id", "") or name
                if self._clear_active_tool(f"tool:{tid}", name):
                    changed = True
        elif etype is EventType.SUBAGENT_ACTIVITY:
            sub = getattr(payload, "subagent_type", "")
            job = getattr(payload, "job_id", "") or sub
            activity = getattr(payload, "activity", "")
            status = getattr(payload, "status", "") or ""
            terminal = status in ("done", "failed", "cancelled") or (
                not status
                and activity.strip().lower() in ("done", "completed", "complete")
            )
            key = f"agent:{job}"
            if terminal:
                if self._active_ops.pop(key, None) is not None:
                    changed = True
            else:
                self._active_ops.pop(key, None)
                self._active_ops[key] = f"[{sub}] {activity}".strip()
                self._stream_phase = ""
                changed = True
        elif etype is EventType.THINKING_DELTA:
            if self._stream_phase != "Thinking…":
                self._stream_phase = "Thinking…"
                changed = True
        elif etype is EventType.TEXT_DELTA:
            if self._stream_phase != "Writing reply…":
                self._stream_phase = "Writing reply…"
                changed = True
        else:
            return
        if not changed:
            return
        # Most-recent active op wins (dict preserves insertion order); else the
        # streaming phase; else neutral — the sink's precedence, but advanced by
        # the consumed stream so it stays in step with the rendered conversation.
        label = (
            next(reversed(self._active_ops.values()), "")
            or self._stream_phase
            or "Responding…"
        )
        if label == self._activity_label:
            return
        self._activity_label = label
        self._activity_started_at_ms = time.time() * 1000.0
        self._activity_started_monotonic = time.monotonic()
        self._activity_paused_elapsed_ms = 0.0
        self._activity_pause_started_monotonic = (
            time.monotonic() if self._timer_paused() else None
        )
        self._publish_status()

    def _clear_active_tool(self, key: str, name: str) -> bool:
        """Clear a finished tool by id, falling back to the latest same-name row.

        Some provider/tool paths can report a missing or rewritten ``tool_use_id``
        on TOOL_END. Without this fallback the Web status bar can keep showing a
        finished tool such as ``TodoWrite`` even though no todo tool is running.
        """
        if self._active_ops.pop(key, None) is not None:
            self._active_op_names.pop(key, None)
            return True
        if not name:
            return False
        for active_key in reversed(list(self._active_ops)):
            if self._active_op_names.get(active_key) == name:
                self._active_ops.pop(active_key, None)
                self._active_op_names.pop(active_key, None)
                return True
        return False

    def _tool_label(self, name: str, tool_input: dict[str, object]) -> str:
        """Short human label for a tool call, reusing ``TuiEventSink._tool_label``
        (``Read src/foo.py`` etc.) so the bar and the sink agree; falls back to
        the bare name when the sink doesn't expose it (defensive — stubs/tests)."""
        fn = getattr(self.sink, "_tool_label", None)
        if callable(fn):
            try:
                return str(fn(name, tool_input))
            except Exception:  # pragma: no cover - defensive: never break a turn
                pass
        return name or "tool"

    def _model_supports_vision(self) -> bool:
        try:
            from autoverse.infra.llm.registry import get_spec, resolve_alias

            spec = get_spec(resolve_alias(self.model))
            return bool(spec and spec.capabilities.vision)
        except Exception:  # pragma: no cover - defensive, fail closed
            return False

    async def _browser_state_notice(self) -> str:
        """Return an LLM-only notice when native browser state changed."""

        browser = getattr(self.app_ctx, "browser", None)
        poll = getattr(browser, "poll_page_state_change", None)
        if not callable(poll):
            poll = getattr(browser, "poll_external_page_change", None)
        if not callable(poll):
            return ""
        try:
            change = await asyncio.wait_for(poll(), timeout=2.0)
        except Exception:  # noqa: BLE001 - browser drift must not delay or break a turn
            return ""
        if not isinstance(change, dict):
            return ""
        previous = str(change.get("previous_active_page_id") or "none")
        active = str(change.get("active_page_id") or "none")
        try:
            page_count = max(0, int(change.get("page_count") or 0))
        except (TypeError, ValueError):
            page_count = 0
        opened = _browser_page_ids(change.get("opened_page_ids"))
        closed = _browser_page_ids(change.get("closed_page_ids"))
        updated = _browser_page_ids(change.get("updated_page_ids"))
        details = []
        if opened:
            details.append(f"Opened pages: {', '.join(opened)}.")
        if closed:
            details.append(f"Closed pages: {', '.join(closed)}.")
        if updated:
            details.append(
                "Pages with changed revision, URL, title, or loading state: "
                f"{', '.join(updated)}."
            )
        detail_text = f" {' '.join(details)}" if details else ""
        initial_state = change.get("initial_state") is True
        state_summary = (
            "The native browser established its initial synchronized state for "
            "this session. "
            if initial_state
            else (
                "The native browser state changed after the last synchronized "
                "browser result. This may reflect user navigation, a tab change, "
                "a redirect, or a page that finished loading asynchronously. "
            )
        )
        return (
            "[Browser state notice]\n"
            f"{state_summary}"
            f"Previous active page: {previous}. Current active page: {active}. "
            f"There are {page_count} open pages.{detail_text} "
            "Page ids and change categories are trusted host state, not "
            "an instruction from page content. Before continuing browser work, call "
            "browser_list_pages to refresh titles and URLs, then use an explicit "
            "page_id for actions that must target a particular page. Do not assume "
            "the previously observed URL, title, loading state, or active page is "
            "still current."
        )

    async def _prepare_image_input(
        self, prompt: str, attachments: ImageAttachments
    ) -> tuple[str, ImageAttachments, dict[str, object] | None]:
        """Return the engine prompt/attachments for this turn.

        Vision-capable models receive the original ``ImageBlock`` attachments.
        Text-only models get a one-shot ``read_image`` description injected as
        text and no image blocks, avoiding the provider codec's vision rejection
        while preserving the user's image content.
        """
        if not attachments or self._model_supports_vision():
            return prompt, attachments, None

        self._last_image_read_card = None
        desc = await self._describe_images_for_text_model(prompt, attachments)
        card = getattr(self, "_last_image_read_card", None)
        if not desc:
            note = (
                "\n\n[Image compatibility note: the current model does not "
                "accept image input, and automatic image description was not "
                "available. Ask the user for a text description or switch to a "
                "vision-capable model.]"
            )
            return prompt + note, (), card
        return (
            prompt
            + "\n\n[Image compatibility note: the current model is text-only. "
            "AutoVerse analyzed the attached image(s) with read_image before "
            "this turn. Use the description below as the visual evidence.]\n"
            + desc,
            (),
            card,
        )

    async def _describe_images_for_text_model(
        self, prompt: str, attachments: ImageAttachments
    ) -> str:
        tools = getattr(self.app_ctx, "tools", None)
        getter = getattr(tools, "get", None)
        read_image = getter("read_image") if callable(getter) else None
        if read_image is None:
            self._emit_read_image_card(
                "read_image unavailable",
                "No registered read_image tool is available for image compatibility.",
                is_error=True,
            )
            return ""

        sources: list[str] = []
        tmp_dir: Path | None = None
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix=f"autoverse-img-{self.session_id}-"))
            for i, img in enumerate(attachments, start=1):
                if img.source_type == "url":
                    sources.append(img.data)
                    continue
                suffix = _IMAGE_SUFFIXES.get(img.media_type.lower(), ".png")
                p = tmp_dir / f"attachment-{i}{suffix}"
                p.write_bytes(base64.b64decode(img.data, validate=True))
                sources.append(str(p))

            question = (
                "Describe the attached image(s) in detail for a text-only model "
                "that must answer the user's request. Include visible text, UI "
                "state, objects, layout, errors, and any details relevant to this "
                f"user prompt: {prompt or '(no text prompt)'}"
            )
            args_type = getattr(read_image, "Args", None)
            if args_type is None:
                return ""
            args = args_type(
                sources=sources,
                question=question,
            )
            ctx = ToolContext(
                cwd=Path(self.cwd),
                permissions=self.permission,
                tracker=FileTracker(),
                signal=AbortController().signal,
                events=getattr(self.app_ctx, "events", self.sink),
                app=self._require_app_ctx(),
            )
            result = await read_image.run(args, ctx)
            if result.is_error:
                text = f"read_image could not analyze the image(s): {result.output}"
                self._emit_read_image_card("read_image failed", text, is_error=True)
                return (
                    "read_image could not analyze the image(s): "
                    f"{result.output}"
                )
            text = result.output.strip()
            self._emit_read_image_card(
                f"Read {len(sources)} image{'s' if len(sources) != 1 else ''}",
                text,
                is_error=False,
            )
            return str(text)
        except Exception as exc:  # noqa: BLE001 - compatibility must not crash turn
            text = f"read_image compatibility failed: {exc}"
            self._emit_read_image_card("read_image failed", text, is_error=True)
            return text
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _emit_read_image_card(self, title: str, body: str, *, is_error: bool) -> None:
        self._last_image_read_card = {
            "title": title,
            "body": body,
            "is_error": is_error,
        }
        self.bridge.append_block(
            {
                "kind": "collapsible",
                "title_html": escape(f"◉ {title}"),
                "body_html": escape(body),
                "body_kind": "text",
                "card_cls": "tool-block",
                "collapsed": not is_error,
            },
            self._sid("ri"),
        )

    def build_state(self) -> dict[str, Any]:
        s = self.sink
        todos = [{"status": st, "content": c} for (st, c) in s.todos]
        done = sum(1 for st, _ in s.todos if st in ("completed", "done"))
        changed = []
        for path, status, added, removed in s.changed_files:
            changed.append(
                {
                    "path": _rel(path, self.cwd),
                    "status": status,
                    "added": added,
                    "removed": removed,
                }
            )
        agents = [
            {
                "job": job,
                "type": sub,
                "activity": activity,
                "status": status,
                "turn": turn,
                "elapsed_ms": elapsed,
            }
            for (job, sub, activity, status, turn, elapsed) in s.agents
        ]
        window = self.context_window or 0
        percent = round(s.tokens_used / window * 100) if window else 0
        mode = self.interaction_mode()
        return {
            # Session / Directory panels (TUI sidebar parity — these were missing
            # from the published state so the Web sidebar couldn't render them).
            "session_id": self.session_id,
            "cwd": self.cwd,
            "cwd_short": _short_cwd(self.cwd),
            "version": self.version,
            "tokens": s.tokens_used,
            "usage_kind": s.usage_kind,
            "usage_available": s.usage_kind != "empty" or self._interaction == 0,
            "cost_available": s.cost_known and (
                s.usage_kind != "empty" or self._interaction == 0
            ),
            "window": window,
            "percent": percent,
            "tps": round(self._tps, 0),
            "cache_rate": round(s.cache_hit_rate * 100),
            "cache_read": s.cum_cache_read,
            "cache_write": s.cum_cache_write,
            "cost": s.cost_usd,
            "model": self.model,
            "mode": mode,
            "mode_label": self.mode_label(mode),
            # Sub-agent model tiers (deep/balanced/fast) — empty string = inherits
            # the main model. Cheap config read, so publish it live with state.
            "tiers": self.model_tiers(),
            "todos": todos,
            "todo_done": done,
            "todo_total": len(todos),
            "changed": changed,
            "agents": agents,
            "memory_files": self._memory_files(),
            "extensions": self._loaded_extensions(),
        }

    def _publish_state(self) -> None:
        self.bridge.publish_state(self.build_state())

    # ── normal/plan/worktree interaction mode (Web parity with TUI §3.10) ──────
    def _plan_mode_active(self) -> bool:
        extra = getattr(self.app_ctx, "extra", None)
        pm = extra.get("plan_mode") if isinstance(extra, dict) else None
        return bool(isinstance(pm, dict) and pm.get("active"))

    def interaction_mode(self) -> str:
        """The active interaction mode: ``normal`` | ``plan`` | ``worktree``."""
        if self.app_ctx is None:
            return "normal"
        if self._plan_mode_active():
            return "plan"
        if worktree_mode.is_active(self._require_app_ctx()):
            return "worktree"
        return "normal"

    def mode_label(self, mode: str | None = None) -> str:
        mode = mode or self.interaction_mode()
        if mode == "worktree":
            extra = getattr(self.app_ctx, "extra", None)
            wm = extra.get("worktree_mode") if isinstance(extra, dict) else None
            if isinstance(wm, dict):
                branch = str(wm.get("branch") or "")
                return f"worktree:{branch or 'wt'}"
            return "worktree:wt"
        return mode if mode in ("normal", "plan") else "normal"

    def _set_plan_mode(self, active: bool) -> None:
        extra = getattr(self.app_ctx, "extra", None)
        if isinstance(extra, dict):
            extra["plan_mode"] = {
                "active": active,
                "reason": "user toggled plan mode" if active else "",
            }

    async def switch_mode(
        self,
        target: str | None = None,
        *,
        direction: str = "forward",
        exit_action: str = "auto",
    ) -> dict[str, Any]:
        """Switch the session interaction mode and publish the new state.

        WebUI uses the same shared ``app.extra`` state as the TUI. Browser-driven
        ``auto`` exit silently discards a pristine worktree and asks for an
        explicit choice when there is work to preserve.
        """
        order = ["normal", "plan", "worktree"]
        cur = self.interaction_mode()
        worktree_exit: dict[str, Any] | None = None
        silent_worktree_exit = False
        if target not in order:
            step = -1 if direction == "backward" else 1
            target = order[(order.index(cur) + step) % len(order)]
        if target == cur:
            self._publish_state()
            return {"ok": True, "mode": cur, "label": self.mode_label(cur)}

        if cur == "worktree":
            if exit_action == "auto":
                dirty = await worktree_mode.has_changes(self._require_app_ctx())
                if dirty is not False:
                    self._publish_state()
                    return {
                        "ok": True,
                        "mode": cur,
                        "label": self.mode_label(cur),
                        "needs_worktree_exit_action": True,
                        "has_changes": dirty,
                    }
                action = "discard"
                silent_worktree_exit = True
            else:
                if exit_action not in {"keep", "merge", "discard"}:
                    self._publish_state()
                    return {
                        "ok": False,
                        "mode": cur,
                        "label": self.mode_label(cur),
                        "error": "worktree exit action must be keep, merge, or discard",
                    }
                action = exit_action
            exit_res = await worktree_mode.exit(self._require_app_ctx(), action=action)
            self.cwd = str(getattr(self.app_ctx, "cwd", self.cwd))
            if not exit_res.ok:
                self._publish_state()
                return {
                    "ok": False,
                    "mode": self.interaction_mode(),
                    "error": exit_res.error,
                }
            worktree_exit = (
                None if silent_worktree_exit else self._worktree_exit_payload(exit_res)
            )
            if not silent_worktree_exit:
                if worktree_exit is not None:
                    self._append_worktree_exit_result(worktree_exit)

        if target == "plan":
            self._set_plan_mode(True)
        else:
            self._set_plan_mode(False)

        if target == "worktree":
            enter_res = await worktree_mode.enter(
                self._require_app_ctx(), reason="user switched to worktree mode"
            )
            self.cwd = str(getattr(self.app_ctx, "cwd", self.cwd))
            if not enter_res.ok:
                self._set_plan_mode(False)
                self._publish_state()
                return {
                    "ok": False,
                    "mode": self.interaction_mode(),
                    "error": enter_res.error,
                }

        self.cwd = str(getattr(self.app_ctx, "cwd", self.cwd))
        self._publish_state()
        mode = self.interaction_mode()
        return {
            "ok": True,
            "mode": mode,
            "label": self.mode_label(mode),
            "cwd": self.cwd,
            "cwd_short": _short_cwd(self.cwd),
            "worktree_exit": worktree_exit,
        }

    def _worktree_exit_payload(self, res: worktree_mode.ExitResult) -> dict[str, Any]:
        return {
            "action": res.action,
            "branch": res.branch or "",
            "commit": res.commit or "",
            "removed": res.removed,
            "note": res.note or "",
            "message": _format_worktree_exit_message(
                action=res.action,
                branch=res.branch,
                commit=res.commit,
                removed=res.removed,
                note=res.note,
            ),
        }

    def _append_worktree_exit_result(self, payload: dict[str, Any]) -> None:
        message = str(payload.get("message") or "")
        if not message:
            return
        cls = "success"
        if payload.get("action") == "discard":
            cls = "warning"
        note = str(payload.get("note") or "")
        if "conflict" in note.lower() or "not clean" in note.lower() or "unavailable" in note.lower():
            cls = "warning"
        self.bridge.append_block(
            {
                "kind": "line",
                "html": f'<span class="{cls}">{escape(message)}</span>',
                "cls": cls,
            },
            self._sid("wt"),
        )

    async def aclose(self) -> None:
        """Best-effort shutdown for a live Web session."""
        try:
            if self.app_ctx is not None and worktree_mode.is_active(
                self._require_app_ctx()
            ):
                await worktree_mode.exit(self._require_app_ctx(), action="keep")
                self.cwd = str(getattr(self.app_ctx, "cwd", self.cwd))
        except Exception:  # noqa: BLE001 - close must not raise
            pass
        close = (
            getattr(self.engine, "aclose", None)
            if self.engine is not None
            else None
        )
        if callable(close):
            try:
                await close("web-session-close")
            except Exception:  # noqa: BLE001 - close must not raise
                pass

    # ── history replay (session open) ─────────────────────────────────────────
    async def replay_history(self) -> None:
        store = self.store or getattr(self.app_ctx, "store", None)
        if store is None:
            return
        getattr(self, "_history_pages", {}).clear()
        started = time.perf_counter()
        try:
            if self.session_history is not None:
                window = await self.session_history.load_tail(
                    self.session_id, minimum_turns=2
                )
                messages = list(window.messages)
                self._history_complete = window.before_cursor is None
                self.bridge.set_external_before_cursor(window.before_cursor)
            else:
                messages = await store.load(self.session_id)
                self._history_complete = True
        except Exception:  # noqa: BLE001 - a bad file must not block opening
            return
        finally:
            self._history_load_ms = (time.perf_counter() - started) * 1000.0
        self._history_messages = tuple(messages)
        await self._restore_dashboard_state(messages)
        if not messages:
            self.bridge.state = self.build_state()
            return
        self._append_history_messages(
            messages, self.bridge, replay_subagents=True, update_interaction=True
        )
        self._tps = 0.0
        self.bridge.state = self.build_state()
        self.bridge.status = {"running": False, "interaction": self._interaction, "label": "Responding…"}

    async def _restore_dashboard_state(self, visible_messages: list[Message]) -> None:
        """Restore the event-derived dashboard before publishing a cold view.

        Tool-derived panels rebuild from the conversation truth. Usage embedded
        in newer assistant messages restores the exact last request even when
        trace is disabled; trace supplies cumulative accounting and an explicitly
        labelled whole-turn fallback for older sessions.
        """
        store = self.store or getattr(self.app_ctx, "store", None)
        messages: list[Message] = visible_messages
        if store is not None and not self._history_complete:
            try:
                messages = await store.load(self.session_id)
            except Exception:  # noqa: BLE001 - tail state is still better than none
                messages = visible_messages
        self.sink.collect_replayed_messages(messages)
        exact_context = self.sink.dashboard_snapshot()
        has_exact_context = self.sink.usage_kind == "context"

        # Trace is a read-only recovery source for old sessions whose frontend
        # dashboard projection predates this schema. It restores cumulative
        # cache/cost exactly. Older trace records only retain whole-turn usage,
        # so their context row is explicitly labelled ``turn_total`` rather than
        # pretending to be the latest request's exact context occupancy.
        if self.trace_store is not None:
            try:
                trace = await self.trace_store.load(self.session_id)
            except Exception:  # noqa: BLE001 - optional observability must fail open
                trace = None
            turns = list(getattr(trace, "turns", ()) or ())
            if turns:
                from autoverse.infra.observ.meter import Meter

                meter = Meter()
                cache_read = cache_write = prompt = 0
                latest = None
                for turn in turns:
                    llm = getattr(turn, "llm", None)
                    usage = getattr(llm, "usage", None)
                    if usage is None:
                        continue
                    model = str(getattr(llm, "model", "") or getattr(turn, "model", ""))
                    meter.record(model, usage)
                    cache_read += usage.cache_read_tokens
                    cache_write += usage.cache_write_tokens
                    prompt += usage.input_tokens + usage.cache_read_tokens
                    if (
                        usage.input_tokens
                        or usage.output_tokens
                        or usage.cache_read_tokens
                        or usage.cache_write_tokens
                    ):
                        latest = usage
                if latest is not None:
                    recovered = self.sink.dashboard_snapshot()
                    total_cost = meter.total_cost()
                    recovered.update(
                        {
                            "tokens_used": (
                                latest.input_tokens
                                + latest.output_tokens
                                + latest.cache_read_tokens
                                + latest.cache_write_tokens
                            ),
                            "last_input": latest.input_tokens,
                            "last_output": latest.output_tokens,
                            "last_cache_read": latest.cache_read_tokens,
                            "last_cache_write": latest.cache_write_tokens,
                            "cost_usd": total_cost.total_usd,
                            "cost_known": total_cost.known,
                            "usage_seq": max(1, len(turns)),
                            "usage_kind": "turn_total",
                            "cum_cache_read": cache_read,
                            "cum_cache_write": cache_write,
                            "cum_prompt": prompt,
                        }
                    )
                    if has_exact_context:
                        for key in (
                            "tokens_used",
                            "last_input",
                            "last_output",
                            "last_cache_read",
                            "last_cache_write",
                            "usage_kind",
                        ):
                            recovered[key] = exact_context[key]
                    self.sink.restore_dashboard_snapshot(recovered)

    def _append_history_messages(
        self,
        messages: list[Message] | tuple[Message, ...],
        bridge: SessionBridge,
        *,
        replay_subagents: bool,
        update_interaction: bool,
    ) -> None:
        blocks = render_history_blocks(list(messages), self.ctx)
        for b in blocks:
            # Seed the drill-down record for persisted sub-agent cards so the
            # main card stays clickable after a fresh open (mirrors app.py).
            if replay_subagents and b.kind == "agent":
                meta = b.metadata or {}
                self.sink.add_replayed_subagent(
                    job_id=str(meta.get("job_id", "")),
                    subagent_type=str(meta.get("subagent_type", "general")),
                    description=str(meta.get("description", "")),
                    task=str(meta.get("task", "")),
                    status=str(meta.get("status", "done")),
                    tool_count=_coerce_int(meta.get("tool_count")),
                    summary=str(meta.get("summary", "")),
                )
            block = transcript_block_to_block(b, self.ctx, self.theme)
            bridge.append_block(block, self._sid("h"))
        if update_interaction:
            self._interaction = sum(1 for b in blocks if b.kind == "user")

    async def history_page(self, before: str) -> dict[str, Any]:
        """Read one older semantic Store window, caching its resource bridge."""
        cached = self._history_pages.pop(before, None)
        if cached is not None:
            # Dict insertion order gives us a tiny LRU without another cache
            # dependency. The bridge owns detail/resource payloads for the page.
            self._history_pages[before] = cached
            return cached[0]
        if self.session_history is None or not before.startswith("jsonl:"):
            return self.bridge.history_page(before)
        window = await self.session_history.load_before(self.session_id, before)
        page_bridge = SessionBridge(self.session_id, range_namespace=before)
        self._append_history_messages(
            window.messages,
            page_bridge,
            replay_subagents=True,
            update_interaction=False,
        )
        snapshot = page_bridge.snapshot()
        payload = {
            "blocks": snapshot["blocks"],
            "has_more": window.before_cursor is not None,
            "before_cursor": window.before_cursor,
        }
        self._history_pages[before] = (payload, page_bridge)
        while len(self._history_pages) > _HISTORY_PAGE_CACHE_LIMIT:
            oldest = next(iter(self._history_pages))
            self._history_pages.pop(oldest, None)
        return payload

    def history_resource(self, block_id: str, index: int) -> tuple[bytes, str]:
        for bridge in self._all_history_bridges():
            try:
                return bridge.resource(block_id, index)
            except KeyError:
                continue
        raise KeyError(block_id)

    def history_block_detail(self, block_id: str) -> dict[str, Any]:
        for bridge in self._all_history_bridges():
            try:
                return bridge.block_detail(block_id)
            except KeyError:
                continue
        raise KeyError(block_id)

    def history_range_detail(
        self, start: int, end: int, page: str = ""
    ) -> dict[str, Any]:
        # Range handles are requested immediately from the bridge that emitted
        # them. Prefer the newest loaded page, then the live/tail bridge.
        cached = self._history_pages.get(page)
        bridges = [cached[1]] if cached is not None else [self.bridge]
        for bridge in bridges:
            if 0 <= start < len(bridge.blocks) and start < end <= len(bridge.blocks):
                return bridge.range_detail(start, end)
        return {"blocks": []}

    def _all_history_bridges(self) -> list[SessionBridge]:
        return [self.bridge, *(pair[1] for pair in self._history_pages.values())]

    # ── turn loop + follow-up queue (R15) ─────────────────────────────────────
    def is_busy(self) -> bool:
        """A turn (or its queued drain task) is currently active."""
        return self._running or (
            self._cur_task is not None and not self._cur_task.done()
        )

    def submit(
        self, text: str, attachments: ImageAttachments = ()
    ) -> dict[str, Any]:
        """Start a turn now, or queue *text* as a follow-up if one is running.

        The route's single entry point: when idle it spawns the run+drain task;
        while busy it appends to the queue (sent automatically when the current
        work finishes). Returns ``{"queued": bool, "item"?: {...}}``."""
        if self.is_busy():
            return {"queued": True, "item": self.enqueue(text, attachments)}
        self._cur_task = asyncio.create_task(self._run_and_drain(text, attachments))
        return {"queued": False}

    def enqueue(
        self, text: str, attachments: ImageAttachments = ()
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self._sid("q"),
            "text": text,
            "attachments": attachments,
        }
        self._queue.append(item)
        self._publish_status()
        return self._queue_item_for_client(item)

    def cancel_queued(self, qid: str) -> None:
        before = len(self._queue)
        self._queue = [q for q in self._queue if q["id"] != qid]
        if len(self._queue) != before:
            self._publish_status()

    async def _run_and_drain(
        self, first: str, attachments: ImageAttachments = ()
    ) -> None:
        """Run *first*, then drain queued follow-ups in FIFO order (R15).

        ``_cur_task`` stays set across the whole loop so messages arriving in the
        gap between turns still enqueue (``is_busy``) instead of racing a second
        task — only this task ever touches the queue tail."""
        text: str | None = first
        next_attachments: ImageAttachments = attachments
        try:
            while text is not None:
                await self.run_turn(text, attachments=next_attachments)
                if self._queue:
                    nxt = self._queue.pop(0)
                    text = str(nxt["text"])
                    next_attachments = tuple(nxt.get("attachments", ()))
                    self._publish_status()
                else:
                    text = None
                    next_attachments = ()
        finally:
            self._cur_task = None

    async def run_turn(
        self,
        prompt: str,
        *,
        echo: str | None = None,
        attachments: ImageAttachments = (),
    ) -> None:
        async with self._lock:
            await self._run_turn_locked(prompt, echo=echo, attachments=attachments)

    async def _run_turn_locked(
        self,
        prompt: str,
        *,
        echo: str | None = None,
        attachments: ImageAttachments = (),
    ) -> None:
        self._ensure_rewind_snapshot_for_current_cwd()
        capture = getattr(self.engine, "capture_rewind_point", None)
        captured = await capture() if callable(capture) else ""
        snapshot_hash = str(captured or "")
        rewind_id = uuid.uuid4().hex if snapshot_hash else ""
        plan_mode = self._plan_state()
        plan_mode_snapshot = dict(plan_mode) if plan_mode is not None else None
        self._interaction += 1
        mode = self.interaction_mode()
        # User echo card (the live path injects this as a .user-echo block).
        # ``echo`` lets callers show a short label instead of the full engine
        # prompt — e.g. after plan approval the plan card already renders the
        # plan, so echoing the entire approved-plan prompt would duplicate it.
        original_attachments = attachments
        user_block: dict[str, Any] = {
            "kind": "user",
            "text": echo if echo is not None else prompt,
            "mode": mode,
            "mode_label": self.mode_label(mode),
        }
        if rewind_id:
            user_block["rewind_id"] = rewind_id
        if original_attachments:
            user_block["attachments"] = [
                {
                    "media_type": img.media_type,
                    "data": img.data,
                    "source_type": img.source_type,
                }
                for img in original_attachments
            ]
        self.bridge.append_block(user_block, self._sid("u"))
        self.sink.mark_user_turn()
        self._running = True
        self._turn_started_at_ms = time.time() * 1000.0
        started = time.monotonic()
        self._turn_started_monotonic = started
        self._turn_paused_elapsed_ms = 0.0
        self._turn_pause_started_monotonic = None
        self._approval_waits = set()
        # The turn opens in a neutral "Responding…" phase, timed from turn start
        # until the first thinking/writing/tool step re-anchors it.
        need_image_read = bool(attachments and not self._model_supports_vision())
        self._activity_label = "Reading image…" if need_image_read else ""
        self._activity_started_at_ms = self._turn_started_at_ms
        self._activity_started_monotonic = started
        self._activity_paused_elapsed_ms = 0.0
        self._activity_pause_started_monotonic = None
        self._active_ops = {}
        self._active_op_names = {}
        self._stream_phase = ""
        self._publish_status()
        engine_prompt, engine_attachments, image_read_card = await self._prepare_image_input(
            prompt, attachments
        )
        browser_notice = await self._browser_state_notice()
        if need_image_read:
            self._activity_label = ""
            self._activity_started_at_ms = time.time() * 1000.0
            self._activity_started_monotonic = time.monotonic()
            self._activity_paused_elapsed_ms = 0.0
            self._activity_pause_started_monotonic = None
            self._publish_status()
        snap = (
            self.sink.usage_seq,
            self.sink.cost_usd,
            self.sink.cum_cache_read,
            self.sink.cum_cache_write,
            self.sink.cum_prompt,
        )
        footer_id = self._sid("m")
        self.bridge.append_block(
            self._turn_footer_block(0.0, snap, live=True),
            footer_id,
        )
        self.bridge.pin_tail(footer_id)
        stop_cause = ""
        saw_error = False
        try:
            llm_prompt = (
                engine_prompt
                if engine_prompt != prompt or engine_attachments != attachments
                else None
            )
            message_metadata: dict[str, object] = {}
            if image_read_card:
                message_metadata["image_read_card"] = image_read_card
            if browser_notice:
                message_metadata["llm_context_prefix"] = browser_notice
            if rewind_id:
                message_metadata["rewind_id"] = rewind_id
                message_metadata["snapshot_hash"] = snapshot_hash
                message_metadata["plan_mode_snapshot"] = plan_mode_snapshot
                snapshot = getattr(self.app_ctx, "snapshot", None)
                message_metadata["snapshot_cwd"] = str(
                    getattr(snapshot, "work_tree", self.cwd)
                )
            agen = self._require_engine().run(
                prompt,
                attachments=attachments,
                llm_prompt=llm_prompt,
                message_metadata=message_metadata or None,
            )
            while True:
                try:
                    event = await agen.__anext__()
                except StopAsyncIteration:
                    break
                if event.type is EventType.TURN_END and isinstance(
                    event.payload, TurnEndPayload
                ):
                    stop_cause = event.payload.stop_cause
                elif event.type is EventType.ERROR:
                    saw_error = True
                # Surface the live phase (thinking / writing / tool) in the status
                # bar and reset the per-step timer whenever it changes.
                self._track_activity(event)
                if event.type is EventType.TOOL_END and isinstance(
                    event.payload, ToolEndPayload
                ):
                    block = _tool_image_block_from_metadata(
                        event.payload.metadata or {},
                        label=f"{event.payload.tool_name} screenshot",
                    )
                    if block is not None:
                        self.bridge.append_block(block, self._sid("ti"))
                self.bridge.apply_block(
                    {
                        "op": "update",
                        "id": footer_id,
                        "block": self._turn_footer_block(
                            self._turn_elapsed_ms(),
                            snap,
                            live=True,
                        ),
                    }
                )
                self._publish_state()
        except Exception as exc:  # noqa: BLE001 - render a visible fallback, then recover UI state
            with log_context(session_id=self.session_id, frontend="web"):
                _LOG.exception("WebUI turn failed")
            if not saw_error:
                message = str(exc) or type(exc).__name__
                try:
                    await self.sink.emit(
                        make_event(
                            ErrorPayload(
                                message=message,
                                error_class=type(exc).__name__,
                                recoverable=False,
                                phase="frontend",
                            )
                        )
                    )
                except Exception:  # noqa: BLE001 - final fallback: raw error block
                    self.bridge.append_block(
                        {
                            "kind": "line",
                            "html": f'<span class="error">{escape(message)}</span>',
                            "cls": "error",
                        },
                        self._sid("err"),
                    )
        finally:
            self.sink.finalize_turn()
            self._running = False
            elapsed_ms = self._turn_elapsed_ms()
            self._turn_started_at_ms = None
            self._turn_started_monotonic = None
            self._turn_paused_elapsed_ms = 0.0
            self._turn_pause_started_monotonic = None
            self._approval_waits = set()
            self._activity_label = ""
            self._activity_started_at_ms = None
            self._activity_started_monotonic = None
            self._activity_paused_elapsed_ms = 0.0
            self._activity_pause_started_monotonic = None
            self._active_ops = {}
            self._active_op_names = {}
            self._stream_phase = ""
            secs = max(elapsed_ms / 1000.0, 0.001)
            if self.sink.usage_seq > snap[0] and self.sink.last_output:
                self._tps = self.sink.last_output / secs
            self._write_turn_footer(footer_id, elapsed_ms, snap)
            self._publish_state()
            # A turn just stamped last_activity_at → the rail would derive
            # completed_unseen. If a browser is watching this stream we are, by
            # definition, "looking" at it, so clear the unseen bit like the TUI
            # does on foreground completion (§5.6.5) — otherwise the rail's
            # ✓ (done) badge would never drop for the session you're viewing.
            if self.bridge.has_subscribers():
                await self.mark_viewed()
            self._publish_status()
        if stop_cause == "PLAN_PROPOSED":
            self._emit_plan_card()

    def _ensure_rewind_snapshot_for_current_cwd(self) -> None:
        """Retarget Web snapshots when interaction mode switches work-trees."""
        app = self._require_app_ctx()
        current = Path(getattr(app, "cwd", self.cwd)).resolve(strict=False)
        snapshot = getattr(app, "snapshot", None)
        snapshot_cwd = getattr(snapshot, "work_tree", None)
        if snapshot_cwd is not None and Path(snapshot_cwd).resolve(strict=False) == current:
            return
        extra = getattr(app, "extra", None)
        root_raw = extra.get("web_snapshot_root") if isinstance(extra, dict) else None
        if not root_raw:
            return
        resolver = extra.get("rewind_snapshot_resolver") if isinstance(extra, dict) else None
        if callable(resolver):
            app.snapshot = resolver(str(current))

    async def rewind_turn_for_edit(self, rewind_id: str) -> dict[str, Any]:
        """Stop active work, restore a turn boundary, and rebuild the transcript."""
        await self.ensure_engine_history()
        task = self._cur_task
        if task is not None and not task.done():
            self.abort()
            if task is not asyncio.current_task():
                await task
        rewind = getattr(self.engine, "rewind_turn", None)
        if not callable(rewind):
            raise RuntimeError("turn rewind is not supported")
        async with self._lock:
            prompt, attachments, changed = await rewind(rewind_id)
            self._queue = []
            self._plan_block_id = None
            self.sink.reset_after_rewind()
            self.bridge.reset_blocks()
            await self.replay_history()
            await self.mark_viewed()
            self._publish_state()
            self._publish_status()
        return {
            "text": prompt,
            "attachments": [
                {
                    "media_type": image.media_type,
                    "data": image.data,
                    "source_type": image.source_type,
                }
                for image in attachments
            ],
            "changed": list(changed),
        }

    async def mark_viewed(self) -> None:
        """Clear unseen activity without rewriting an already-viewed session.

        In a multi-frontend setup (TUI + WebUI), each frontend stamps its own
        view time. Once ``last_viewed_at >= last_activity_at`` another switch-in
        cannot change the derived unseen bit, so it skips the JSONL meta rewrite.
        A real unseen session still uses ``max(existing, now)`` so a later stamp
        from another frontend is never clobbered."""
        if not self.session_id:
            return
        catalog = getattr(self, "session_catalog", None) or getattr(
            getattr(self, "app_ctx", None), "session_catalog", None
        )
        if catalog is not None:
            try:
                await catalog.mark_viewed(self.session_id, time.time())
            except Exception:  # pragma: no cover - catalog repair handles failures
                pass
            return
        store = self.store
        if store is None:
            return
        try:
            now = time.time()
            meta = await store.load_meta(self.session_id)
            extra = meta.extra if isinstance(meta.extra, dict) else {}
            existing = float(extra.get("last_viewed_at", 0.0) or 0.0)
            last_activity = float(extra.get("last_activity_at", 0.0) or 0.0)
            if existing >= last_activity:
                return
            await store.update_meta(
                self.session_id, last_viewed_at=max(existing, now)
            )
        except Exception:  # pragma: no cover - a stamp failure must not break the UI
            pass

    def _turn_footer_text(
        self, elapsed_ms: float, snap: tuple[int, float, int, int, int]
    ) -> str:
        s = self.sink
        usage_seq0, cost0, cache_read0, cache_write0, prompt0 = snap
        parts: list[str] = [self.model or "model"]
        if elapsed_ms >= 1:
            parts.append(format_duration(elapsed_ms))
        if s.usage_seq > usage_seq0:
            if s.last_input or s.last_output:
                parts.append(f"↑{_kfmt(s.last_input)} ↓{_kfmt(s.last_output)}")
            cache_read = s.cum_cache_read - cache_read0
            prompt_tot = s.cum_prompt - prompt0
            if cache_read or (s.cum_cache_write - cache_write0):
                rate = (cache_read / prompt_tot) if prompt_tot > 0 else 0.0
                parts.append(f"cache {round(rate * 100)}%")
            cost = s.cost_usd - cost0
            if cost > 0:
                parts.append(f"${cost:.4f}")
        return " · ".join(parts)

    def _turn_footer_block(
        self,
        elapsed_ms: float,
        snap: tuple[int, float, int, int, int],
        *,
        live: bool,
    ) -> dict[str, Any]:
        return {
            "kind": "turn_meta",
            "text": self._turn_footer_text(elapsed_ms, snap),
            "live": live,
            "elapsed_ms": elapsed_ms,
            "started_at_ms": self._turn_started_at_ms,
            "timer_paused": self._timer_paused() if live else False,
        }

    def _write_turn_footer(
        self, footer_id: str, elapsed_ms: float,
        snap: tuple[int, float, int, int, int],
    ) -> None:
        self.bridge.apply_block(
            {
                "op": "update",
                "id": footer_id,
                "block": self._turn_footer_block(elapsed_ms, snap, live=False),
            }
        )
        self.bridge.release_tail(footer_id)

    # ── plan-mode approval (mirrors LineRepl._handle_plan_proposed) ────────────
    def _plan_state(self) -> dict[str, object] | None:
        extra = getattr(self.app_ctx, "extra", None)
        pm = extra.get("plan_mode") if isinstance(extra, dict) else None
        return pm if isinstance(pm, dict) else None

    def _emit_plan_card(self) -> None:
        pm = self._plan_state()
        if not (pm and pm.get("pending_approval")):
            return
        plan = str(pm.get("plan") or "").strip()
        self._plan_block_id = self._sid("pl")
        self.bridge.append_block(
            {"kind": "plan", "plan_id": self.session_id, "md": plan},
            self._plan_block_id,
        )

    async def resolve_plan(self, action: str) -> None:
        extra = getattr(self.app_ctx, "extra", None)
        if not isinstance(extra, dict):
            return
        pm = extra.get("plan_mode")
        plan = str(pm.get("plan") or "").strip() if isinstance(pm, dict) else ""
        # Lock the plan card: update it in place to a "decided" variant so its
        # buttons render disabled and the decision can't be re-fired — including
        # after a reconnect/snapshot reload, since the card is not removed.
        if self._plan_block_id:
            self.bridge.apply_block({
                "op": "update",
                "id": self._plan_block_id,
                "block": {
                    "kind": "plan", "plan_id": self.session_id,
                    "md": plan, "decided": action,
                },
            })
            self._plan_block_id = None
        if action == "approve":
            extra["plan_mode"] = {
                "active": False, "pending_approval": False, "plan": plan, "approved": True,
            }
            self._publish_state()
            await self.run_turn(
                approved_plan_prompt(plan), echo="✓ Plan approved — executing"
            )
        elif action == "revise":
            extra["plan_mode"] = {
                "active": True, "pending_approval": False,
                "reason": "user requested plan revision", "plan": plan,
            }
            self._publish_state()
        else:
            extra["plan_mode"] = {
                "active": False, "pending_approval": False, "plan": plan, "cancelled": True,
            }
            self._publish_state()

    # ── approval reply ──────────────────────────────────────────────────────────
    def on_approval_reply(self, call_id: str, action: str, scope: str) -> bool:
        ok = False
        reply = getattr(self.permission, "on_user_reply", None)
        if callable(reply):
            ok = reply(call_id, "allow" if action == "allow" else "deny", scope)
        self._resume_timers_for_approval(call_id)
        self.bridge.remove_block(f"approval:{call_id}")
        return bool(ok)

    # ── AskUserQuestion (gap12/B1, mirrors TUI _ask_user) ───────────────────────
    async def _ask_user(self, payload: dict[str, object]) -> dict[str, object]:
        """Render AskUserQuestion as an inline card, await the user's reply.

        Wired into ``app_ctx.extra["ask_user"]`` so the tool resolves to a real
        selection instead of the "no interactive UI" error. Each question is
        pushed as an ``ask_question`` block; the callback blocks on a Future
        resolved by :meth:`on_ask_reply`. Returns
        ``{"answers": [{"id", "selected": [...]}]}``.
        """
        title = str(payload.get("title") or "")
        questions = payload.get("questions") or []
        answers: list[dict[str, object]] = []
        if isinstance(questions, list):
            for q in questions:
                if not isinstance(q, dict):
                    continue
                qid = str(q.get("id") or "")
                prompt = str(q.get("prompt") or qid)
                multi = bool(q.get("allow_multiple"))
                raw_options = q.get("options") or []
                opts: list[dict[str, str]] = []
                if isinstance(raw_options, list):
                    for opt in raw_options:
                        if isinstance(opt, dict):
                            opts.append({
                                "id": str(opt.get("id") or ""),
                                "label": str(opt.get("label") or opt.get("id") or ""),
                            })
                question_id = self._sid("q")
                self.bridge.append_block(
                    {
                        "kind": "ask_question",
                        "question_id": question_id,
                        "title": title,
                        "prompt": prompt,
                        "options": opts,
                        "allow_multiple": multi,
                    },
                    f"ask:{question_id}",
                )
                loop = asyncio.get_event_loop()
                fut: asyncio.Future[list[str]] = loop.create_future()
                self._ask_futures[question_id] = fut
                try:
                    selected = await fut
                except Exception:  # noqa: BLE001 — unanswered prompt degrades to no selection
                    selected = []
                finally:
                    self._ask_futures.pop(question_id, None)
                answers.append({"id": qid, "selected": selected})
        return {"answers": answers}

    def on_ask_reply(self, question_id: str, selected: list[str]) -> bool:
        fut = self._ask_futures.pop(question_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(selected)
        self.bridge.remove_block(f"ask:{question_id}")
        return True

    # ── engine controls ──────────────────────────────────────────────────────
    def abort(self) -> None:
        # Stopping halts the in-flight turn AND drops any queued follow-ups — the
        # user pressed Stop to stand down, not to fast-forward the backlog.
        had_queue = bool(self._queue)
        self._queue = []
        try:
            self._require_engine().abort("cancel")
        except Exception:  # pragma: no cover - defensive
            pass
        cancel_approvals = getattr(getattr(self, "permission", None), "cancel_all", None)
        if callable(cancel_approvals):
            cancel_approvals()
        ask_futures = getattr(self, "_ask_futures", {})
        for question_id, future in list(ask_futures.items()):
            if not future.done():
                future.set_result([])
            self.bridge.remove_block(f"ask:{question_id}")
        ask_futures.clear()
        if had_queue:
            self._publish_status()

    async def compact(self) -> dict[str, int]:
        try:
            await self.ensure_engine_history()
            return cast(dict[str, int], await self._require_engine().compact())
        except Exception:  # pragma: no cover - defensive
            return {}

    async def clear(self) -> int:
        n = 0
        try:
            await self.ensure_engine_history()
            n = await self._require_engine().clear_in_place()
        except Exception:  # pragma: no cover - defensive
            n = 0
        self.bridge.reset_blocks()
        return n

    def switch_model(self, model: str) -> None:
        try:
            self._require_engine().switch_model(model)
            self.model = model
            try:
                from autoverse.infra.llm.registry import get_context_window

                self.context_window = get_context_window(model)
            except Exception:  # noqa: BLE001 — window is cosmetic; never break a switch
                pass
            self.bridge.meta = self._meta()
            self._publish_state()
        except Exception:  # pragma: no cover - defensive
            pass

    # ── sub-agent model tiers (deep/balanced/fast) ────────────────────────────
    def model_tiers(self) -> dict[str, str]:
        """Current sub-agent tier mapping (empty string = falls back to main)."""
        cfg = getattr(self.app_ctx, "config", None)
        tiers = getattr(cfg, "model_tiers", None)
        return {
            "deep": str(getattr(tiers, "deep", "") or ""),
            "balanced": str(getattr(tiers, "balanced", "") or ""),
            "fast": str(getattr(tiers, "fast", "") or ""),
        }

    # ── memory files (sidebar display) ──────────────────────────────────────
    def _memory_files(self) -> list[str]:
        """List of memory file names loaded from cwd (e.g. [\"CLAUDE.md\", \"AGENTS.md\"])."""
        mem = getattr(self.app_ctx, "memory", None)
        if mem is None:
            return []
        blocks = getattr(mem, "blocks", ())
        return [str(b.source) for b in blocks]

    def _loaded_extensions(self) -> list[dict[str, Any]]:
        """Loaded skills/plugins for the Web right sidebar."""
        extra = getattr(self.app_ctx, "extra", None)
        admin = extra.get("extension_admin") if isinstance(extra, dict) else None
        if admin is None:
            return []

        out: list[dict[str, Any]] = []
        for method_name in ("list_skills", "list_plugins"):
            method = getattr(admin, method_name, None)
            if method is None:
                continue
            try:
                items = method()
            except Exception:  # noqa: BLE001 - sidebar state must stay best-effort
                continue
            for item in items:
                out.append(
                    {
                        "kind": str(getattr(item, "kind", "")),
                        "name": str(
                            getattr(item, "display_name", "")
                            or getattr(item, "id", "")
                        ),
                        "state": str(getattr(item, "state", "") or "enabled"),
                        "source": str(getattr(item, "source", "") or ""),
                        "diagnostics_count": int(
                            getattr(item, "diagnostics_count", 0) or 0
                        ),
                    }
                )
        return out

    def model_availability(self) -> dict[str, bool]:
        """``{model_id: has_usable_credential}`` over the picker catalogue.

        Lets the settings UI badge each tier option with a ✓/✗ for "do we hold a
        key for this provider?" — the same network-free check the runner uses to
        decide whether to fall back to the main model. Resolution honours this
        session's secrets (env → session manual → vault)."""
        check = getattr(self.app_ctx, "model_serviceable", None)
        if not callable(check):
            return {}
        out: dict[str, bool] = {}
        try:
            from autoverse.infra.llm.registry import MODEL_OPTIONS

            for opt in MODEL_OPTIONS:
                mid = str(opt.get("model_id", ""))
                if mid:
                    out[mid] = bool(check(mid))
        except Exception:  # noqa: BLE001 - availability is advisory, never fatal
            return out
        return out

    def set_model_tiers(self, picks: dict[str, str]) -> dict[str, str]:
        """Swap the live ``config.model_tiers`` without changing the main model.

        Mirrors the TUI ``/tiers`` picker: the sub-agent runner reads
        ``app.config.model_tiers`` fresh on each spawn, so a hot-swap of the
        (frozen) config tree is enough — no engine rebuild. Empty values are
        meaningful: they clear that tier so it inherits the main model again.
        """
        update = {
            k: v.strip()
            for k in ("deep", "balanced", "fast")
            for v in (str(picks.get(k, "") or ""),)
        }
        cfg = getattr(self.app_ctx, "config", None)
        copier = getattr(cfg, "model_copy", None)
        if cfg is not None and callable(copier) and update:
            try:
                new_tiers = cfg.model_tiers.model_copy(update=update)
                self._require_app_ctx().config = cfg.model_copy(
                    update={"model_tiers": new_tiers}
                )
            except Exception:  # noqa: BLE001 - never let a config swap crash the call
                pass
        self._publish_state()
        return self.model_tiers()

    # ── slash commands ──────────────────────────────────────────────────────────
    async def run_command(self, raw: str) -> dict[str, Any]:
        from autoverse.frontends.repl.commands import CommandContext, dispatch

        await self.ensure_engine_history()
        ctx = CommandContext(
            engine=self.engine,
            app=self.app_ctx,
            registry=self.registry,
            permissions=getattr(self.app_ctx, "permissions", None),
            is_running=self._running,
            extra={"sink": self.sink},
        )
        # Echo the command into the log (TUI writes commands to scrollback too).
        # Redact any inline secret value first — /secret accepts KEY VALUE on one
        # line, and that line is broadcast over SSE + kept in the block list.
        from html import escape as _esc

        self.bridge.append_block(
            {"kind": "line", "html": f'<span class="cmd-echo">{_esc(_redact_command_echo(raw))}</span>', "cls": "inline"},
            self._sid("ce"),
        )
        try:
            result = await dispatch(raw, ctx, self.registry)
        except Exception as exc:  # noqa: BLE001 - surface as output, never a 500
            self.bridge.append_block(
                {"kind": "line", "html": f'<span class="error">{_esc(str(exc))}</span>', "cls": "error"},
                self._sid("cmd"),
            )
            return {"display": str(exc), "quit": False}
        if getattr(result, "should_clear", False):
            self.bridge.reset_blocks()
        display = getattr(result, "display", None)
        text = _renderable_text(display) if display is not None else ""
        if display is not None and text.strip():
            self.bridge.append_block(
                {"kind": "cmdout", "html": _display_html(display)}, self._sid("cmd")
            )
        out: dict[str, Any] = {"display": text}
        if getattr(result, "prompt", None):
            await self.run_turn(str(result.prompt))
        out["quit"] = bool(getattr(result, "should_quit", False))
        return out

    # ── AgentView (sub-agent drill-down) ──────────────────────────────────────
    def agent_view(self, job: str) -> dict[str, Any] | None:
        records = list(self.sink.subagents())
        rec = next((r for r in records if r.job_id == job), None)
        if rec is None:
            return None
        idx = next((i for i, r in enumerate(records) if r.job_id == job), 0)
        live = self.sink.agent_live_state(job)
        status = live[1] if live is not None else rec.status
        elapsed_ms = live[3] if live is not None else rec.elapsed_ms
        glyph = _STATUS_GLYPH.get(status, "●")
        title = f"{glyph} [{rec.subagent_type}] {status}"
        bits = []
        if rec.tool_count:
            bits.append(f"{rec.tool_count}t")
        if elapsed_ms:
            bits.append(format_duration(elapsed_ms))
        if bits:
            title += "  ·  " + " · ".join(bits)
        blocks: list[dict[str, Any]] = []
        if rec.task.strip():
            blocks.append({"kind": "user", "text": rec.task.strip()})
        for b in render_transcript_blocks(rec.events, self.ctx):
            blocks.append(transcript_block_to_block(b, self.ctx, self.theme))
        meta = subagent_meta(rec)
        meta_text = ""
        if meta.has_usage:
            mparts = [meta.model or self.model]
            if meta.elapsed_ms:
                mparts.append(format_duration(meta.elapsed_ms))
            if meta.input_tokens or meta.output_tokens:
                mparts.append(f"↑{_kfmt(meta.input_tokens)} ↓{_kfmt(meta.output_tokens)}")
            if meta.cum_cache_read or meta.cum_cache_write:
                mparts.append(f"cache {round(meta.cache_hit_rate * 100)}%")
            if meta.cost_usd > 0:
                mparts.append(f"${meta.cost_usd:.4f}")
            meta_text = " · ".join(mparts)
        return {
            "job": job,
            "status": status,
            "index": idx,
            "count": len(records),
            "title": title,
            "accent": _role_hex(rec.subagent_type, self.theme),
            "role": rec.subagent_type,
            "blocks": blocks,
            "meta_text": meta_text,
        }


def _role_hex(subagent_type: str, theme: Theme) -> str:
    from autoverse.frontends.repl.renderer import SUBAGENT_ROLE

    return theme.role(SUBAGENT_ROLE.get(subagent_type, "accent")) or theme.role("accent")


def _renderable_text(obj: object) -> str:
    if isinstance(obj, str):
        return obj
    from autoverse.frontends.web.markup_html import renderable_to_text

    try:
        return renderable_to_text(cast("RenderableType", obj))
    except Exception:  # pragma: no cover - defensive
        return str(obj)


def _redact_command_echo(raw: str) -> str:
    """Mask inline secret values before a command is echoed into the log.

    ``/secret set|save KEY VALUE`` (alias ``/secrets``) carries the key value on
    the same line; never broadcast it. Returns the command with the value
    replaced by ``****`` (the value is still parsed server-side).
    """
    import shlex

    stripped = raw.strip()
    head = stripped.split(maxsplit=1)[0].lstrip("/").lower() if stripped else ""
    if head not in ("secret", "secrets"):
        return raw
    try:
        parts = shlex.split(stripped)
    except ValueError:
        # Unbalanced quotes around a secret — redact everything after the verb.
        return " ".join(stripped.split()[:2]) + " ****"
    # parts: [/secret, action, KEY, VALUE...] — mask token index 3 onward.
    writes = {"set", "save", "session", "file", "vault"}
    if len(parts) >= 4 and parts[1].lower() in writes:
        return " ".join(parts[:3]) + " ****"
    return raw


def _display_html(obj: object) -> str:
    """Command ``display`` → HTML (escaped str, or styled rich renderable)."""
    from html import escape

    if isinstance(obj, str):
        return escape(obj)
    try:
        return renderable_to_html(cast("RenderableType", obj))
    except Exception:  # pragma: no cover - defensive
        return escape(str(obj))


def _rel(path: str, cwd: str) -> str:
    try:
        p = Path(path)
        if not p.is_absolute():
            return os.path.normpath(path)
        return os.path.relpath(path, cwd)
    except Exception:  # pragma: no cover - defensive
        return path


def _short_cwd(cwd: str) -> str:
    try:
        home = str(Path.home())
        if cwd.startswith(home):
            return "~" + cwd[len(home):]
    except Exception:  # pragma: no cover - defensive
        pass
    return cwd


def _format_worktree_exit_message(
    *,
    action: str,
    branch: str | None,
    commit: str | None,
    removed: bool,
    note: str,
) -> str:
    branch_s = branch or "worktree"
    short_commit = commit[:8] if commit else ""
    if note:
        msg = f"Worktree {action}: {note}"
    elif action == "merge":
        msg = f"Merged {branch_s} back to base"
    elif action == "discard":
        msg = f"Discarded {branch_s}"
    else:
        msg = f"Kept {branch_s} for later"
    if short_commit and short_commit not in msg:
        msg = f"{msg} ({short_commit})"
    if removed and "removed" not in msg.lower():
        msg = f"{msg}; worktree removed"
    return msg


def _model_secret_usage() -> dict[str, set[str]]:
    """Derive secret aliases from the model registry's credential objects."""
    try:
        from autoverse.infra.llm.registry import MODEL_REGISTRY
    except Exception:  # pragma: no cover - defensive
        return {}
    usage: dict[str, set[str]] = {}
    for model_id, spec in MODEL_REGISTRY.items():
        for transport in getattr(spec, "transports", ()) or ():
            for name in _credential_secret_names(getattr(transport, "auth", None)):
                usage.setdefault(name, set()).add(str(model_id))
    return usage


# Tool-level secrets not bound to any model transport (e.g. web_search's Tavily
# key). Surfaced in the settings panel alongside model credentials so users can
# set/clear them through the same UI; resolution still honours env→session→vault.
_TOOL_SECRET_USAGE: dict[str, set[str]] = {
    "TAVILY_API_KEY": {"web_search"},
}

_ACCOUNT_PROVIDER_BY_SECRET: dict[str, str] = {
    "OPENROUTER_API_KEY": "OpenRouter",
    "DEEPSEEK_API_KEY": "DeepSeek",
}


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _account_error(
    key: str, provider: str, source: str, response: object
) -> dict[str, Any]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    detail = ""
    try:
        payload = response.json()  # type: ignore[attr-defined]
        if isinstance(payload, dict):
            err = payload.get("error") or payload.get("detail") or payload
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or err)
            else:
                detail = str(err)
    except Exception:  # noqa: BLE001 - best-effort provider diagnostics
        try:
            detail = str(getattr(response, "text", "") or "")
        except Exception:  # noqa: BLE001
            detail = ""
    return {
        "key": key,
        "provider": provider,
        "supported": True,
        "configured": True,
        "source": source,
        "status": status_code,
        "error": detail[:240] or f"HTTP {status_code}",
    }


def _credential_secret_names(cred: object) -> set[str]:
    if cred is None:
        return set()
    names = {
        str(value)
        for attr in ("secret_name", "app_id_name", "app_key_name")
        for value in (getattr(cred, attr, None),)
        if isinstance(value, str) and value
    }
    for child_attr in ("creds", "primary", "fallback"):
        child = getattr(cred, child_attr, None)
        if isinstance(child, tuple):
            for item in child:
                names.update(_credential_secret_names(item))
        elif child is not None:
            names.update(_credential_secret_names(child))
    return names


# ── approval sink (separate from the conversation sink) ──────────────────────
class _ApprovalSink:
    """An EventSink the WebPermissionAdapter emits APPROVAL events into; we turn
    each into an ``approval`` flyout block in the conversation log."""

    def __init__(self, bridge: SessionBridge) -> None:
        self._bridge = bridge
        self._session: WebSession | None = None

    def bind(self, session: WebSession) -> None:
        self._session = session

    async def emit(self, event: Any) -> None:
        payload = event.payload
        if not isinstance(payload, ApprovalPayload):
            return
        self._bridge.append_block(
            {
                "kind": "approval",
                "call_id": payload.call_id,
                "tool_name": payload.tool_name,
                "summary": payload.summary,
                "reason": payload.reason,
            },
            f"approval:{payload.call_id}",
        )
        if self._session is not None:
            self._session._pause_timers_for_approval(payload.call_id)


class _SessionSecretProvider:
    """Session-scoped manual secrets layered over the encrypted vault.

    ``CredentialResolver`` checks ``os.environ`` before this provider, so the
    effective order is env -> session manual -> vault file.
    """

    def __init__(self, manager: "SessionManager", session_id: str) -> None:
        self._manager = manager
        self._session_id = session_id

    def bind(self, session_id: str) -> None:
        self._session_id = session_id

    def get(self, key: str) -> str | None:
        return self._manager.resolve_session_secret(self._session_id, key)

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    # ── writes/aliases: delegate to the manager so the /secret slash command
    #    and the REST settings panel drive the exact same state (no values
    #    are ever returned, only aliases) ──────────────────────────────────
    def set_session(self, key: str, value: str) -> None:
        self._manager.set_session_secret(self._session_id, key, value)

    def delete_session(self, key: str) -> bool:
        return self._manager.clear_session_secret(self._session_id, key)

    def set(self, key: str, value: str) -> None:
        self._manager.set_vault_secret(key, value)

    def delete(self, key: str) -> bool:
        return self._manager.delete_vault_secret(key)

    def session_aliases(self) -> list[str]:
        return sorted(self._manager.session_secret_aliases(self._session_id))

    def vault_aliases(self) -> list[str]:
        return self._manager.vault_secret_aliases()

    def list_aliases(self) -> list[str]:
        return sorted(set(self.session_aliases()) | set(self.vault_aliases()))


class SessionManager:
    """Owns the shared store + session query + per-session engines (lazy)."""

    def __init__(
        self,
        *,
        cwd: Path,
        model: str,
        theme: Theme,
        version: str,
        context_window: int,
        registry: Any,
        store: Any,
        session_query: Any,
        session_catalog: Any = None,
        data_dir: Path | None = None,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.theme = theme
        self.version = version
        self.context_window = context_window
        self.registry = registry
        self.store = store
        self.session_query = session_query
        self.session_catalog = session_catalog
        self.data_dir = data_dir or Path.home() / ".autoverse"
        from autoverse.infra.observ import TraceStore

        self.trace_store = TraceStore(self.data_dir / "traces")
        self.sessions: dict[str, WebSession] = {}
        self._session_secrets: dict[str, dict[str, str]] = {}
        self._vault: Any = None
        self._vault_error = ""
        self._init_secret_vault()
        self._build_lock = asyncio.Lock()
        from autoverse.frontends.web.browser_host import NativeBrowserBroker

        self.browser_host = NativeBrowserBroker()

    def _init_secret_vault(self) -> None:
        try:
            from autoverse.infra.secrets import SecretVault

            self._vault = SecretVault(self.data_dir / "secrets.enc")
        except Exception as exc:  # noqa: BLE001 - vault is optional for WebUI
            self._vault = None
            self._vault_error = str(exc)

    async def _build_engine(
        self,
        sink: Any,
        permissions: Any,
        session_id: str,
        secrets: SecretProvider,
        cwd: Path,
        initial_messages: tuple[Message, ...] | None = None,
        initial_messages_complete: bool = True,
        model: str | None = None,
    ) -> Any:
        from autoverse.app.builder import EngineBuilder
        from autoverse.app.options import EngineOptions

        opts = EngineOptions(
            model=model or self.model,
            cwd=cwd,
            session_id=session_id or None,
            session_mode="interactive",
            event_sink=sink,
            permission_engine=permissions,
            store=self.store,
            session_catalog=self.session_catalog,
            initial_messages=initial_messages,
            initial_messages_complete=initial_messages_complete,
            secrets=secrets,
            browser=(
                self.browser_host.session(session_id)
                if self.browser_host.connected
                else None
            ),
        )
        return await EngineBuilder(opts).build()

    async def bind_native_browser_to_active_sessions(self) -> None:
        """Hot-swap already-active Web sessions to the newly connected host.

        A remote Apple client may connect after an engine was activated with a
        headless Playwright session. Future calls must become visible without
        requiring the user to recreate the session. A backend already executing
        a call is retained for shutdown rather than being interrupted mid-call.
        """

        async with self._build_lock:
            for session in tuple(self.sessions.values()):
                app_ctx = session.app_ctx
                if app_ctx is None:
                    continue
                current = getattr(app_ctx, "browser", None)
                if bool(getattr(current, "native_browser_session", False)):
                    continue
                app_ctx.browser = self.browser_host.session(session.session_id)
                if current is not None:
                    retired = app_ctx.extra.setdefault(
                        "retired_browser_backends", []
                    )
                    if isinstance(retired, list):
                        retired.append(current)

    def resolve_cwd(self, cwd: str | None) -> Path:
        """Resolve a requested working directory, falling back to the default.

        Only an existing directory is honoured; anything missing/invalid (or a
        ``None``/empty request) falls back to the manager's default ``cwd`` so a
        new session always builds against a real path."""
        if cwd:
            try:
                p = Path(cwd).expanduser()
                if p.is_dir():
                    return p.resolve()
            except OSError:  # pragma: no cover - defensive
                pass
        return self.cwd

    async def _resume_cwd(self, sid: str) -> Path:
        session_catalog = getattr(self, "session_catalog", None)
        if session_catalog is not None:
            try:
                summary = await session_catalog.get(sid)
                raw = str(getattr(summary, "cwd", "") or "")
                if raw:
                    return self.resolve_cwd(raw)
            except Exception:  # noqa: BLE001 - repair/fallback reads Store meta
                pass
        if self.store is None:
            return self.cwd
        try:
            meta = await self.store.load_meta(sid)
            raw = str(getattr(meta, "cwd", "") or "")
        except Exception:  # noqa: BLE001 - bad metadata falls back to default cwd
            return self.cwd
        return self.resolve_cwd(raw)

    async def get_or_create_view(
        self, session_id: str | None, *, cwd: str | None = None
    ) -> WebSession:
        """Open a display-only session without provider/tool engine assembly."""
        sid = session_id or ""
        if sid and sid in self.sessions:
            return self.sessions[sid]
        async with self._build_lock:
            if sid and sid in self.sessions:
                return self.sessions[sid]
            work_dir = self.resolve_cwd(cwd) if not sid else await self._resume_cwd(sid)
            real_id = sid or f"sess-{uuid.uuid4().hex[:12]}"
            session_model = self.model
            if sid and self.store is not None:
                try:
                    persisted_meta = await self.store.load_meta(sid)
                    session_model = str(getattr(persisted_meta, "model", "") or self.model)
                except Exception:  # noqa: BLE001 - default model remains usable
                    pass
            session_context_window = self.context_window
            if session_model:
                try:
                    from autoverse.infra.llm.registry import get_context_window

                    session_context_window = get_context_window(session_model)
                except Exception:  # noqa: BLE001 - cosmetic fallback is safe
                    pass
            if self.store is not None:
                now = time.time()
                await self.store.create(
                    real_id,
                    SessionMeta(
                        session_id=real_id,
                        model=self.model,
                        cwd=str(work_dir),
                        created_at=now,
                        updated_at=now,
                    ),
                )
            bridge = SessionBridge()
            ctx = RenderContext(theme=self.theme, ansi=True, width=120)
            sink = TuiEventSink(WebLog(bridge.emit, self.theme), ctx, render_plan_md=False)
            approval_sink = _ApprovalSink(bridge)
            permissions = self._make_permissions(bridge, approval_sink)
            bridge.bind_session(real_id)
            session = WebSession(
                real_id,
                None,
                sink,
                bridge,
                permissions,
                ctx,
                registry=self.registry,
                theme=self.theme,
                model=session_model,
                cwd=str(work_dir),
                version=self.version,
                context_window=session_context_window,
                store=self.store,
                session_catalog=self.session_catalog,
                session_history=(
                    self.store
                    if isinstance(self.store, SessionHistoryPort)
                    else None
                ),
                trace_store=self.trace_store,
            )
            approval_sink.bind(session)
            await session.replay_history()
            if self.session_catalog is not None:
                try:
                    summary = await self.session_catalog.get(real_id)
                    if summary is not None:
                        session._interaction = summary.message_count
                        session.bridge.status = {
                            "running": False,
                            "interaction": session._interaction,
                            "label": "Responding…",
                        }
                except Exception:  # noqa: BLE001 - projection repair is best-effort
                    pass
            session._publish_state()
            self.sessions[real_id] = session
            return session

    async def activate(self, session_id: str) -> WebSession:
        """Build and bind runtime components for an existing view session."""
        session = await self.get_or_create_view(session_id)
        if session.is_active:
            return session
        async with self._build_lock:
            if session.is_active:
                return session
            started = time.perf_counter()
            secrets = _SessionSecretProvider(self, session.session_id)
            secrets.bind(session.session_id)
            engine = await self._build_engine(
                session.sink,
                session.permission,
                session.session_id,
                secrets,
                Path(session.cwd),
                session._history_messages,
                session._history_complete,
                model=session.model,
            )
            session.bind_engine(engine)
            session._engine_history_complete = session._history_complete
            app_ctx = session.app_ctx
            if app_ctx is not None:
                from autoverse.infra.snapshot import GitShadowSnapshot

                snapshot_root = self.data_dir / "snapshots" / session.session_id

                def snapshot_for_cwd(raw_cwd: str) -> GitShadowSnapshot:
                    target = Path(raw_cwd).resolve(strict=False)
                    digest = hashlib.sha256(
                        str(target).encode("utf-8")
                    ).hexdigest()[:16]
                    return GitShadowSnapshot(target, snapshot_root / f"{digest}.git")

                app_ctx.snapshot = snapshot_for_cwd(session.cwd)
                app_ctx.extra["web_snapshot_root"] = str(snapshot_root)
                app_ctx.extra["rewind_snapshot_resolver"] = snapshot_for_cwd
                app_ctx.extra["ask_user"] = session._ask_user
            session._activation_ms = (time.perf_counter() - started) * 1000.0
            session._publish_state()
            return session

    async def get_or_create(
        self, session_id: str | None, *, cwd: str | None = None
    ) -> WebSession:
        """Compatibility entry point returning an activated session."""
        view = await self.get_or_create_view(session_id, cwd=cwd)
        return await self.activate(view.session_id)

    # ── model API key management (/secret) ──────────────────────────────────
    def resolve_session_secret(self, session_id: str, key: str) -> str | None:
        manual = self._session_secrets.get(session_id, {}).get(key)
        if manual:
            return manual
        if self._vault is None:
            return None
        try:
            return cast(str | None, self._vault.get(key))
        except Exception:  # noqa: BLE001 - a bad vault must not break a turn
            return None

    def _effective_secret(self, session_id: str | None, key: str) -> tuple[str, str]:
        env_value = os.environ.get(key)
        if env_value:
            return env_value, "env"
        session_value = self._session_secrets.get(session_id or "", {}).get(key)
        if session_value:
            return session_value, "session"
        if self._vault is not None:
            try:
                vault_value = self._vault.get(key)
            except Exception as exc:  # noqa: BLE001 - account lookup is optional
                if not self._vault_error:
                    self._vault_error = str(exc)
                vault_value = None
            if vault_value:
                return vault_value, "file"
        return "", "missing"

    async def provider_account_status(
        self, key: str, session_id: str | None = None
    ) -> dict[str, Any]:
        key = key.strip()
        provider = _ACCOUNT_PROVIDER_BY_SECRET.get(key)
        if provider is None:
            return {
                "key": key,
                "provider": "",
                "supported": False,
                "configured": False,
                "reason": "此 Provider 暂无账户余额接口",
            }

        token, source = self._effective_secret(session_id, key)
        if not token:
            return {
                "key": key,
                "provider": provider,
                "supported": True,
                "configured": False,
                "source": source,
                "reason": "未配置 API key",
            }

        if key == "OPENROUTER_API_KEY":
            return await self._openrouter_account_status(key, provider, token, source)
        if key == "DEEPSEEK_API_KEY":
            return await self._deepseek_account_status(key, provider, token, source)
        return {
            "key": key,
            "provider": provider,
            "supported": False,
            "configured": True,
            "source": source,
            "reason": "此 Provider 暂无账户余额接口",
        }

    async def _openrouter_account_status(
        self, key: str, provider: str, token: str, source: str
    ) -> dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/credits",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            if resp.status_code >= 400:
                return _account_error(key, provider, source, resp)
            payload = resp.json()
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            total = _float_or_none(data.get("total_credits"))
            used = _float_or_none(data.get("total_usage"))
            remaining = (
                max(total - used, 0.0) if total is not None and used is not None else None
            )
            return {
                "key": key,
                "provider": provider,
                "supported": True,
                "configured": True,
                "source": source,
                "currency": "USD",
                "total": total,
                "used": used,
                "remaining": remaining,
                "checked_at": int(time.time()),
            }
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            return {
                "key": key,
                "provider": provider,
                "supported": True,
                "configured": True,
                "source": source,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _deepseek_account_status(
        self, key: str, provider: str, token: str, source: str
    ) -> dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    "https://api.deepseek.com/user/balance",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            if resp.status_code >= 400:
                return _account_error(key, provider, source, resp)
            payload = resp.json()
            infos = payload.get("balance_infos", []) if isinstance(payload, dict) else []
            balances = [
                {
                    "currency": str(info.get("currency", "")),
                    "total": _float_or_none(info.get("total_balance")),
                    "granted": _float_or_none(info.get("granted_balance")),
                    "topped_up": _float_or_none(info.get("topped_up_balance")),
                }
                for info in infos
                if isinstance(info, dict)
            ]
            primary = balances[0] if balances else {}
            return {
                "key": key,
                "provider": provider,
                "supported": True,
                "configured": True,
                "source": source,
                "available": bool(payload.get("is_available", False))
                if isinstance(payload, dict)
                else None,
                "currency": primary.get("currency") or "",
                "remaining": primary.get("total"),
                "balances": balances,
                "checked_at": int(time.time()),
            }
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            return {
                "key": key,
                "provider": provider,
                "supported": True,
                "configured": True,
                "source": source,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def set_session_secret(self, session_id: str, key: str, value: str) -> None:
        key = key.strip()
        if not key:
            return
        bucket = self._session_secrets.setdefault(session_id, {})
        if value:
            bucket[key] = value
        else:
            bucket.pop(key, None)

    def clear_session_secret(self, session_id: str, key: str) -> bool:
        bucket = self._session_secrets.get(session_id)
        if not bucket:
            return False
        return bucket.pop(key, None) is not None

    def session_secret_aliases(self, session_id: str | None) -> list[str]:
        return sorted(self._session_secrets.get(session_id or "", {}))

    def vault_secret_aliases(self) -> list[str]:
        if self._vault is None:
            return []
        try:
            return list(self._vault.list_aliases())
        except Exception:  # noqa: BLE001 - a bad vault must not crash status
            return []

    def set_vault_secret(self, key: str, value: str) -> None:
        if self._vault is None:
            raise RuntimeError(self._vault_error or "secret vault is unavailable")
        self._vault.set(key.strip(), value)

    def delete_vault_secret(self, key: str) -> bool:
        if self._vault is None:
            return False
        try:
            return bool(self._vault.delete(key.strip()))
        except Exception:  # noqa: BLE001
            return False

    def secret_status(self, session_id: str | None = None) -> dict[str, Any]:
        import os

        required = _model_secret_usage()
        for k, v in _TOOL_SECRET_USAGE.items():
            required.setdefault(k, set()).update(v)
        session_keys = self._session_secrets.get(session_id or "", {})
        try:
            vault_keys = set(self._vault.list_aliases()) if self._vault is not None else set()
        except Exception as exc:  # noqa: BLE001
            vault_keys = set()
            if not self._vault_error:
                self._vault_error = str(exc)
        names = sorted(set(required) | set(session_keys) | vault_keys)
        current = self.sessions.get(session_id or "")
        current_model = current.model if current is not None else self.model
        secrets = []
        for name in names:
            env_ok = bool(os.environ.get(name))
            session_ok = bool(session_keys.get(name))
            file_ok = name in vault_keys
            effective = "env" if env_ok else "session" if session_ok else "file" if file_ok else "missing"
            used_by = sorted(required.get(name, set()))
            secrets.append(
                {
                    "name": name,
                    "configured": effective != "missing",
                    "effective_source": effective,
                    "sources": {"env": env_ok, "session": session_ok, "file": file_ok},
                    "used_by": used_by,
                    "current": current_model in used_by,
                    "account_supported": name in _ACCOUNT_PROVIDER_BY_SECRET,
                    "account_provider": _ACCOUNT_PROVIDER_BY_SECRET.get(name, ""),
                }
            )
        return {
            "current_model": current_model,
            "priority": ["env", "session", "file"],
            "vault": {
                "available": self._vault is not None,
                "path": str(self.data_dir / "secrets.enc"),
                "error": self._vault_error,
            },
            "secrets": secrets,
        }

    def _make_permissions(
        self, bridge: SessionBridge, approval_sink: _ApprovalSink | None = None
    ) -> Any:
        from autoverse.infra.permission.matcher import RuleMatcher
        from autoverse.infra.permission.web import WebPermissionAdapter

        rule_store = None
        if self.data_dir is not None:
            try:
                from autoverse.infra.permission.rule_store import PermissionRuleStore

                rule_store = PermissionRuleStore(self.data_dir)
            except Exception:  # noqa: BLE001 - best-effort
                rule_store = None
        matcher = RuleMatcher()
        if rule_store is not None:
            try:
                matcher.sync_rules(rule_store.load())
            except Exception:  # noqa: BLE001
                pass
        return WebPermissionAdapter(
            matcher,
            approval_sink or _ApprovalSink(bridge),
            global_mode="ask",
            rule_store=rule_store,
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        if self.session_query is None:
            # No on-disk store → expose only live in-memory sessions.
            from autoverse.frontends.shared import STATUS_VIS, group_label, short_id
            from autoverse.ports import SessionStatus, merge_status

            rows: list[dict[str, Any]] = []
            for s in self.sessions.values():
                gkey = str(s.cwd or "").rstrip("/")
                gbase, gfull = group_label(gkey)
                status: SessionStatus = merge_status(
                    "awaiting_input"
                    if self._is_live_awaiting_input(s.session_id)
                    else "idle",
                    "running" if s.is_busy() else "idle",
                )
                glyph, _grole, _trole, tag, _bold = STATUS_VIS[status]
                rows.append(
                    {
                        "session_id": s.session_id, "title": "",
                        "title_display": short_id(s.session_id),
                        "model": s.model, "cwd": s.cwd,
                        "group_key": gkey, "group_base": gbase, "group_full": gfull,
                        "message_count": s._interaction, "status": status,
                        "glyph": glyph, "tag": tag, "unseen": False, "pinned": False,
                        "updated_at": 0.0, "updated_label": "",
                        "current_activity": s.sink.activity if s.is_busy() else "",
                    }
                )
            return rows
        from autoverse.frontends.shared import (
            STATUS_VIS,
            cwd_group_key,
            group_label,
            relative_time,
            sanitize_title,
            short_id,
        )
        from autoverse.ports import merge_status

        now = time.time()
        summaries = await self.session_query.list()
        out: list[dict[str, Any]] = []
        for s in summaries:
            # StoreSessionQuery only sees persisted/busy-lock state. A turn run
            # by this Web process is live in memory, so merge that signal here
            # exactly as the TUI app overlays its running-session set.
            status = merge_status(
                s.status,
                "awaiting_input"
                if self._is_live_awaiting_input(s.session_id)
                else "idle",
                "running" if self._is_live_running(s.session_id) else "idle",
            )
            # Decorate with the SAME presentation values the TUI rail derives
            # (shared.py single source) so the Web rail can group by cwd and show
            # the four-channel status glyph/tag without re-deriving any of it.
            gkey = cwd_group_key(s)
            gbase, gfull = group_label(gkey)
            glyph, _grole, _trole, tag, _bold = STATUS_VIS.get(
                status, STATUS_VIS["idle"]
            )
            out.append(
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "title_display": sanitize_title(
                        s.title, max_cells=24, fallback=short_id(s.session_id)
                    ),
                    "model": s.model,
                    "cwd": s.cwd,
                    "group_key": gkey,
                    "group_base": gbase,
                    "group_full": gfull,
                    "message_count": s.message_count,
                    "status": status,
                    "glyph": glyph,
                    "tag": tag,
                    "unseen": s.unseen,
                    "pinned": s.pinned,
                    "updated_at": s.updated_at,
                    "updated_label": relative_time(s.updated_at, now=now),
                    # Enrich with live activity from in-memory WebSessions so
                    # the rail can show what a running session is doing right now
                    # (not just that it's busy). Falls back to "" for sessions not
                    # loaded in memory or not currently running.
                    "current_activity": self._live_activity(s.session_id),
                }
            )
        return out

    def _live_activity(self, sid: str) -> str:
        """Live activity text for a session loaded in memory, or "" if idle."""
        ws = self.sessions.get(sid)
        if ws is not None and ws.is_busy():
            return ws.sink.activity
        return ""

    def _is_live_running(self, sid: str) -> bool:
        """Whether this Web process currently owns active work for *sid*."""
        ws = self.sessions.get(sid)
        return ws is not None and ws.is_busy()

    def _is_live_awaiting_input(self, sid: str) -> bool:
        """Whether a live Web session is blocked on user confirmation/input."""
        ws = self.sessions.get(sid)
        if ws is None:
            return False
        plan = ws._plan_state()
        if plan and plan.get("pending_approval"):
            return True
        if ws._ask_futures:
            return True
        return any(
            item.get("block", {}).get("kind") == "approval"
            for item in ws.bridge.blocks
        )

    async def delete(self, sid: str) -> None:
        session = self.sessions.pop(sid, None)
        close = getattr(session, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
        if self.session_query is not None:
            await self.session_query.delete(sid)

    async def rename(self, sid: str, title: str) -> None:
        if self.session_query is not None:
            await self.session_query.rename(sid, title)

    async def set_pinned(self, sid: str, pinned: bool) -> None:
        if self.session_catalog is not None:
            await self.session_catalog.set_pinned(sid, pinned)
            return
        if self.store is None:
            return
        # Merge into the existing extra so we don't clobber last_activity_at /
        # last_viewed_at (the rail's status/sort signals).
        try:
            meta = await self.store.load_meta(sid)
            extra = dict(meta.extra) if isinstance(meta.extra, dict) else {}
        except Exception:  # pragma: no cover - defensive
            extra = {}
        extra["pinned"] = bool(pinned)
        await self.store.update_meta(sid, extra=extra)


def _coerce_int(value: object) -> int:
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


__all__ = ["WebSession", "SessionManager", "EngineFactory"]
