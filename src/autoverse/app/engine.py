"""EngineHandle adapter + a queue-backed EventSink bridge (docs/architecture/contracts.md §6.6).

``EngineAdapter`` wraps a :class:`~autoverse.core.loop.turn_loop.TurnLoop` and
implements the narrow :class:`~autoverse.ports.EngineHandle` Protocol the
frontends/gateway depend on. ``run(prompt)`` drives one ``run_turn`` while
streaming the events it emits out as an ``AsyncIterator[StreamEvent]``: the
TurnLoop is wired with a :class:`QueueEventSink`, ``run_turn`` runs as a task,
and ``run`` async-iterates the queue until the turn finishes.

This module lives in ``app/`` and may import ``core`` (only ``core → infra/app``
is forbidden).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import AsyncIterator, Callable

from autoverse.app.context import AppContext
from autoverse.app.session_query import sync_session_catalog
from autoverse.core.loop.turn_loop import TurnLoop
from autoverse.core.loop.types import TurnOptions
from autoverse.core.state.conversation import Conversation
from autoverse.infra.observ.logging import log_context
from autoverse.infra.persist.busy_lock import BusyLock
from autoverse.ports import EventSink
from autoverse.ports import SessionMeta
from autoverse.types import AbortReason, EventType, ImageBlock, StreamEvent
from autoverse.types.abort import AbortController
from autoverse.types.events import ErrorPayload, make_event
from autoverse.types.llm import LLMRequest, ModelTier
from autoverse.types.messages import ContentBlock, Message, TextBlock

# System prompt for the per-turn auto-title generator (§9.1). Asks the fast-tier
# model for a short, punctuation-free, semantic title summarising the user's
# intent across the whole conversation.
_TITLE_SYSTEM_PROMPT = (
    "你是一个会话标题生成器。根据用户在整个对话中提出的所有请求，"
    "总结出一个简洁、语义化的中文标题，长度约 5-8 个字。"
    "只输出标题本身，不要任何标点、引号、前缀或解释。"
)

_LOG = logging.getLogger(__name__)


def _clean_title(raw: str) -> str:
    """Normalise a model-produced title into a short, single-line string.

    Takes the first non-empty line, folds whitespace, strips wrapping quotes and
    leading/trailing punctuation the model may have added, then hard-caps the
    length (the display layer truncates further by cell width)."""
    if not raw:
        return ""
    line = ""
    for candidate in raw.splitlines():
        if candidate.strip():
            line = candidate
            break
    line = " ".join(line.split())
    line = line.strip("\"'“”‘’`「」『』《》()【】[]<> 　")
    line = line.strip(" 　.,;:!?。，、；：！？…-—")
    return line[:24]


class QueueEventSink:
    """An :class:`EventSink` that pushes events onto an asyncio.Queue.

    Optionally fans out to a downstream sink (e.g. a trace collector or the
    caller-supplied ``event_sink``) so the same StreamEvent feeds both the
    streaming consumer and any side sinks (P2).
    """

    def __init__(self, queue: "asyncio.Queue[StreamEvent | None]", downstream: EventSink | None = None) -> None:
        self._queue = queue
        self._downstream = downstream

    async def emit(self, event: StreamEvent) -> None:
        await self._queue.put(event)
        if self._downstream is not None:
            await self._downstream.emit(event)


class EngineAdapter:
    """Concrete :class:`EngineHandle` wrapping a TurnLoop (P3 narrow facade)."""

    def __init__(
        self,
        *,
        loop: TurnLoop,
        conversation: Conversation,
        app: AppContext,
        queue: "asyncio.Queue[StreamEvent | None]",
        session_id: str,
        provider_factory: "Callable[[str], object] | None" = None,
        busy: "BusyLock | None" = None,
        rule_store: "object | None" = None,
    ) -> None:
        self._loop = loop
        self._conversation = conversation
        self._app = app
        self._queue = queue
        self._session_id = session_id
        self._provider_factory = provider_factory
        self._busy = busy
        self._rule_store = rule_store
        # Lazily-built, cached provider for the fast-tier title generator so we
        # don't reconstruct an HTTP client every turn (§9.1). Rebuilt if the
        # resolved fast model changes.
        self._title_provider: object | None = None
        self._title_provider_model: str = ""
        # Background task that (re)generates the session title; launched as soon
        # as a user prompt arrives (run()) so the sidebar title updates without
        # waiting for the assistant's full reply. Joined at turn end.
        self._title_task: "asyncio.Future[None] | None" = None

    # ── EngineHandle ─────────────────────────────────────────────────────────
    async def sync_history(self) -> None:
        """Refresh Conversation before a non-turn command reads or rewrites it."""
        await self._conversation.sync_from_store()

    async def run(
        self,
        prompt: str,
        attachments: tuple[ImageBlock, ...] = (),
        *,
        llm_prompt: str | None = None,
        message_metadata: dict[str, object] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Re-sync from disk before appending the user message so a multi-frontend
        # setup (TUI + WebUI sharing one store) never sends the model a stale
        # context — picks up turns / compactions / clears the other process did.
        await self._conversation.sync_from_store()
        # Re-load persisted permission rules so "always allow" rules the other
        # frontend wrote to disk take effect in this process too.
        if self._rule_store is not None:
            perm = getattr(self._app, "permissions", None) or self._app.extra.get("permissions")
            matcher = getattr(perm, "matcher", None)
            sync_rules = getattr(matcher, "sync_rules", None)
            load_rules = getattr(self._rule_store, "load", None)
            if callable(sync_rules) and callable(load_rules):
                sync_rules(load_rules())
        # Mark this session as busy in the cross-process lock so the other
        # frontend's session rail shows ``busy_external`` and can warn the user.
        if self._busy is not None:
            self._busy.acquire(self._session_id, frontend="engine")
        content: tuple[ContentBlock, ...] = (TextBlock(text=prompt), *attachments)
        metadata: dict[str, object] = dict(message_metadata or {})
        if "goal_state_snapshot" not in metadata:
            store = getattr(self._app, "store", None)
            if store is not None:
                try:
                    meta = await store.load_meta(self._session_id)
                    extra = getattr(meta, "extra", {}) or {}
                    metadata["goal_state_snapshot"] = copy.deepcopy(
                        extra.get("goal_state") if isinstance(extra, dict) else None
                    )
                except Exception:  # noqa: BLE001 — checkpoint metadata is best-effort
                    metadata["goal_state_snapshot"] = None
        if llm_prompt is not None:
            metadata["llm_text_override"] = llm_prompt
        await self._conversation.append(
            Message(role="user", content=content, metadata=metadata)
        )

        # Kick off title (re)generation immediately on the new user prompt so the
        # rail updates within seconds, concurrently with the assistant's reply
        # (joined in _stamp_meta at turn end). Cancel any still-pending prior one.
        if self._title_task is not None and not self._title_task.done():
            self._title_task.cancel()
        self._title_task = asyncio.ensure_future(self._refresh_title(prompt))

        task = asyncio.ensure_future(self._drive())
        # Detect model drift (advisory notice, non-blocking — does not yield
        # before the main turn task gets scheduled).
        if self._app is not None:
            asyncio.ensure_future(self._detect_model_drift())
        outcome = "ok"
        try:
            while True:
                item = await self._queue.get()
                if item is None:  # done sentinel
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()
            # Surface a genuine failure from the turn (cancellation is expected).
            try:
                await task
            except asyncio.CancelledError:
                outcome = "aborted"
            except Exception:  # noqa: BLE001 — record then re-raise to caller
                outcome = "error"
                await self._stamp_meta(prompt, outcome)
                raise
            await self._stamp_meta(prompt, outcome)
            # Release the cross-process busy marker after meta is stamped so the
            # other frontend's rail transitions from ``busy_external`` to the
            # timestamp-derived status (completed_unseen / idle).
            if self._busy is not None:
                self._busy.release(self._session_id)

    async def _stamp_meta(self, first_prompt: str, outcome: str) -> None:
        """Persist the session-rail Phase-1 timestamps at turn end (§9.1/§9.4).

        Writes ``last_activity_at`` + ``last_outcome`` (drives the rail's
        ``unseen`` / ``completed_unseen`` / ``error_unseen`` derivation, §5.6.4)
        and bumps ``updated_at`` to *now* so the rail shows "last reply N ago"
        and sorts most-recent-first (``append`` doesn't touch ``updated_at``).

        The session title is generated by ``_refresh_title``, launched in run()
        as soon as the user prompt arrives. We *join* that task here first so its
        title write lands before this timestamp write (avoiding a lost-update
        race on the meta file). Best-effort: never breaks the turn."""
        import time

        # Join the title task so its meta write completes before ours (the store
        # rewrites the whole meta line, so concurrent writers would race).
        if self._title_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._title_task), timeout=30.0)
            except Exception:  # noqa: BLE001 — title is best-effort
                pass

        store = getattr(self._app, "store", None)
        if store is None:
            return
        now = time.time()
        try:
            await store.update_meta(
                self._session_id,
                last_activity_at=now,
                last_outcome=outcome,
                updated_at=now,
            )
            await self._sync_catalog_projection()
        except Exception:  # noqa: BLE001 — meta is a side effect, not the turn
            pass

    async def _sync_catalog_projection(self) -> None:
        catalog = self._app.session_catalog
        store = self._app.store
        if catalog is None:
            return
        raw_root = str(self._app.extra.get("session_root") or "")
        source_path = (
            Path(raw_root) / f"{self._session_id}.jsonl" if raw_root else None
        )
        await sync_session_catalog(
            catalog,
            store,
            self._session_id,
            self._conversation.view().messages,
            source_path=source_path,
        )

    async def _refresh_title(
        self, first_prompt: str, *, replace_existing: bool = False
    ) -> None:
        """Generate + persist the session title from all user prompts in context.

        Launched on each new user prompt so the rail updates within seconds,
        concurrently with the assistant reply. Skips manually-renamed sessions
        (``title_locked``). A rewind sets ``replace_existing`` so a stale title is
        replaced from the retained history, or cleared when no user prompt remains.
        Best-effort: any failure leaves metadata writable by later turns."""
        store = getattr(self._app, "store", None)
        if store is None:
            return
        try:
            meta = await store.load_meta(self._session_id)
            extra = getattr(meta, "extra", {}) or {}
            if isinstance(extra, dict) and extra.get("title_locked"):
                return  # preserve a manual rename
            current_title = getattr(meta, "title", "") or ""
            retained_prompts = self._collect_user_prompts()
            if replace_existing and not retained_prompts:
                await store.update_meta(self._session_id, title="")
                return
            title = await self._auto_title(first_prompt)
            if not title:
                if current_title and not replace_existing:
                    return  # keep the existing good title
                fallback = (
                    retained_prompts.splitlines()[0]
                    if replace_existing and retained_prompts
                    else first_prompt
                )
                title = " ".join(fallback.split())[:40]
            if title:
                await store.update_meta(self._session_id, title=title)
        except asyncio.CancelledError:  # superseded by a newer prompt — drop it
            raise
        except Exception:  # noqa: BLE001 — title is a side effect, not the turn
            pass

    async def _detect_model_drift(self) -> None:
        """Surface a notice if the other frontend switched this session's model.

        Reads the latest ``meta.model`` from disk; if it differs from this
        engine's model and is non-empty, emits a RETRY event with an informative
        message. We do NOT auto-switch — a mid-turn provider rebuild could 401.
        The user can ``/model`` to align manually."""
        store = getattr(self._app, "store", None)
        if store is None:
            return
        try:
            meta = await store.load_meta(self._session_id)
            persisted_model = str(getattr(meta, "model", "") or "")
            current_model = str(getattr(self._app, "model", "") or "")
            if persisted_model and current_model and persisted_model != current_model:
                from autoverse.types.events import RetryPayload, make_event

                await self._queue.put(
                    make_event(
                        RetryPayload(
                            attempt=1,
                            reason=f"会话模型已被另一端切换为 {persisted_model}，"
                            f"当前仍使用 {current_model}（可用 /model 对齐）",
                        )
                    )
                )
        except Exception:  # noqa: BLE001 — drift detection is advisory
            pass

    async def _auto_title(self, first_prompt: str) -> str:
        """Summarise all user prompts in context into a short semantic title.

        Returns ``""`` on any failure (caller decides the fallback). Uses the
        fast-tier model, a 20s timeout, and is fully best-effort so a slow or
        failing title call can never wedge or break the turn."""
        prompts = self._collect_user_prompts() or first_prompt.strip()
        if not prompts:
            return ""
        model = self._fast_model()
        # ``echo`` and ``scripted`` (test doubles) would just parrot the input
        # or consume a scripted turn — skip so we fall back to first-prompt
        # truncation instead of garbage / a wasted provider call.
        if not model or model.startswith("echo") or model.startswith("scripted"):
            return ""
        try:
            provider = self._fast_provider(model)
            if provider is None:
                return ""
            # Put the instruction in the *user* turn (the prompts are quoted as
            # data) — relying on the system prompt alone lets some models answer
            # the request instead of titling it. ``max_tokens`` is generous so a
            # reasoning fast-model (e.g. deepseek-flash) can finish its thinking
            # phase and still emit the short title; only TEXT_DELTA is collected.
            instruction = (
                "请为下面的对话起一个简洁、语义化的中文标题，长度约 5-8 个字。"
                "只输出标题本身，不要解释、标点、引号或代码。\n\n"
                "=== 用户请求 ===\n" + prompts
            )
            req = LLMRequest(
                messages=(Message(role="user", content=(TextBlock(text=instruction),)),),
                system=_TITLE_SYSTEM_PROMPT,
                model=model,
                max_tokens=512,
                temperature=0.3,
            )
            signal = AbortController().signal
            raw = await asyncio.wait_for(self._stream_text(provider, req, signal), timeout=30.0)
        except Exception:  # noqa: BLE001 — best-effort; caller falls back
            return ""
        return _clean_title(raw)

    def _collect_user_prompts(self) -> str:
        """Concatenate the text of every user message in the live conversation."""
        try:
            messages = self._conversation.view().messages
        except Exception:  # noqa: BLE001
            return ""
        parts: list[str] = []
        for msg in messages:
            if getattr(msg, "role", "") != "user":
                continue
            for block in getattr(msg, "content", ()):  # TextBlock only; skip images
                text = getattr(block, "text", None)
                if isinstance(text, str) and text.strip():
                    parts.append(" ".join(text.split()))
        return "\n".join(parts)

    def _fast_model(self) -> str:
        """Resolve the fast-tier model id, falling back to the main model."""
        try:
            fast = self._app.config.model_for_tier(ModelTier.FAST)
        except Exception:  # noqa: BLE001
            fast = None
        return (fast or getattr(self._app, "model", "") or "").strip()

    def _fast_provider(self, model: str) -> object | None:
        """Lazily build + cache the fast-tier provider for title generation."""
        if self._provider_factory is None:
            return None
        if self._title_provider is None or self._title_provider_model != model:
            try:
                self._title_provider = self._provider_factory(model)
                self._title_provider_model = model
            except Exception:  # noqa: BLE001
                self._title_provider = None
                self._title_provider_model = ""
                return None
        return self._title_provider

    @staticmethod
    async def _stream_text(provider: object, req: LLMRequest, signal: object) -> str:
        """Drain a provider stream, accumulating only TEXT_DELTA payloads."""
        buf: list[str] = []
        async for ev in provider.stream(req, signal):  # type: ignore[attr-defined]
            if ev.type == EventType.TEXT_DELTA:
                buf.append(ev.payload.text)
        return "".join(buf)

    async def _drive(self) -> None:
        with log_context(session_id=self._session_id, frontend="engine"):
            try:
                # Honour the user-configured main-loop iteration ceiling
                # (Config.max_turns) instead of the TurnOptions default.
                max_turns = int(getattr(self._app.config, "max_turns", 100) or 100)
                await self._loop.run_turn(TurnOptions(max_iterations=max_turns))
            except Exception as exc:  # noqa: BLE001 — convert turn crash into ERROR event
                _LOG.exception("unhandled turn failure")
                try:
                    await self._app.events.emit(
                        make_event(
                            ErrorPayload(
                                message=str(exc) or type(exc).__name__,
                                error_class=type(exc).__name__,
                                recoverable=False,
                                phase="app",
                            )
                        )
                    )
                except Exception:  # noqa: BLE001 — diagnostics must never mask root cause
                    _LOG.exception("failed to emit turn failure event")
                raise
            finally:
                await self._queue.put(None)  # done sentinel

    def abort(self, reason: AbortReason = "cancel") -> None:
        self._loop.abort(reason)

    async def clear_in_place(self) -> int:
        n = len(self._conversation)
        await self._conversation.rewrite([], reason="clear")
        try:
            await self._sync_catalog_projection()
        except Exception:  # noqa: BLE001 - catalog is a repairable projection
            pass
        goal = getattr(self._app, "goal", None)
        if goal is not None:
            clear = getattr(goal, "clear", None)
            if callable(clear):
                clear(self._session_id)
            persist = getattr(goal, "persist", None)
            if callable(persist):
                await persist(self._session_id)
        return n

    async def compact(self) -> dict[str, int]:
        out = await self._loop.compact_now()
        return {
            "tokens_before": int(out["tokens_before"]),
            "tokens_after": int(out["tokens_after"]),
        }

    def switch_model(self, model: str) -> None:
        self._app.model = model
        # A provider is bound to one model's transport/auth at build time
        # (WireProvider sends ``spec.wire_model`` and ignores per-request model),
        # so updating ``app.model`` alone keeps requests hitting the *previous*
        # model's endpoint/credential → cross-provider switches 401. Rebuild the
        # provider for the new model and hot-swap it into the loop.
        if self._provider_factory is None:
            return
        try:
            provider = self._provider_factory(model)
        except Exception:  # noqa: BLE001 — a bad rebuild must not break the switch
            return
        if provider is not None:
            self._loop.set_provider(provider)  # type: ignore[arg-type]
            self._app.extra["model_supports_vision"] = bool(
                getattr(getattr(provider, "capabilities", None), "vision", False)
            )

    # ── /rewind delegation (docs/architecture/runtime-flow.md §3.3a, KDR-12) ──────────────────────
    async def resolve_rewind_target(self, anchor: str) -> object | None:
        return await self._loop.resolve_rewind_target(anchor)

    async def preview_rewind(self, ref: object) -> str:
        return await self._loop.preview_rewind(ref)

    async def rewind(self, ref: object) -> tuple[str, ...]:
        return await self._loop.rewind(ref)

    async def capture_rewind_point(self) -> str:
        """Capture the workspace immediately before a frontend submits a turn."""
        return await self._loop.capture_rewind_point()

    async def rewind_turn(
        self, rewind_id: str
    ) -> tuple[str, tuple[ImageBlock, ...], tuple[str, ...]]:
        """Restore one user turn's workspace boundary and drop it plus all later history."""
        await self._cancel_title_refresh()
        await self._cancel_pending_jobs()
        await self._conversation.sync_from_store()
        messages = self._conversation.view().messages
        target_index = -1
        prompt = ""
        attachments: tuple[ImageBlock, ...] = ()
        snapshot_hash = ""
        snapshot_cwd = ""
        plan_mode_snapshot: object = None
        goal_state_snapshot: object = None
        for index, message in enumerate(messages):
            metadata = message.metadata
            if metadata.get("rewind_id") != rewind_id:
                continue
            target_index = index
            prompt = "\n".join(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            attachments = tuple(
                block for block in message.content if isinstance(block, ImageBlock)
            )
            snapshot_hash = str(metadata.get("snapshot_hash") or "")
            snapshot_cwd = str(metadata.get("snapshot_cwd") or "")
            plan_mode_snapshot = metadata.get("plan_mode_snapshot")
            goal_state_snapshot = metadata.get("goal_state_snapshot")
            break
        if target_index < 0:
            raise ValueError("turn is no longer available")
        resolver = self._app.extra.get("rewind_snapshot_resolver")
        if snapshot_cwd and callable(resolver):
            self._app.snapshot = resolver(snapshot_cwd)
        ref = await self._loop.resolve_rewind_target(snapshot_hash)
        if ref is None:
            raise RuntimeError("workspace snapshot is no longer available")
        changed = await self._loop.restore_snapshot(ref)
        await self._conversation.rewrite(
            list(messages[:target_index]), reason="edit_turn"
        )
        if isinstance(plan_mode_snapshot, Mapping):
            self._app.extra["plan_mode"] = dict(plan_mode_snapshot)
        else:
            self._app.extra.pop("plan_mode", None)
        goal = getattr(self._app, "goal", None)
        restore_goal = getattr(goal, "restore", None)
        if callable(restore_goal):
            restore_goal(self._session_id, goal_state_snapshot)
            persist_goal = getattr(goal, "persist", None)
            if callable(persist_goal):
                await persist_goal(self._session_id)
        await self._refresh_title("", replace_existing=True)
        await self._stamp_meta("", "ok")
        return prompt, attachments, changed

    async def _cancel_title_refresh(self) -> None:
        """Stop a title task derived from history that is about to be discarded."""
        task = self._title_task
        self._title_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _cancel_pending_jobs(self) -> None:
        """Settle background sub-agents before restoring their workspace."""
        jobs = getattr(self._app, "jobs", None)
        list_jobs = getattr(jobs, "list", None)
        cancel = getattr(jobs, "cancel", None)
        if not callable(list_jobs) or not callable(cancel):
            return
        pending = [
            cancel(str(getattr(record, "job_id", "")))
            for record in list_jobs()
            if getattr(record, "job_id", "")
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def app(self) -> AppContext:
        """The DI container, so frontends can reach trace/secrets/tasks (A12)."""
        return self._app

    # ── workflow pipelines (docs/reference/workflows.md §7.4) ────────────────────────────────
    async def run_workflow(self, name: str, task: str = "") -> str:
        """Run a built-in multi-agent pipeline by name (commit/review/pr/init/coordinate).

        Resolves the definition from the single-source WorkflowRegistry (P1) and
        dispatches its phases through a sub-agent-backed PhaseRunner. Returns a
        human-readable outcome summary; degrades gracefully (never raises) when
        the workflow/runner is unavailable so the REPL can surface a message.
        """
        from autoverse.app.workflow.pipeline import Pipeline
        from autoverse.app.workflow.pipelines import SubAgentPhaseRunner

        workflows = self._app.extra.get("workflows")
        get_workflow = getattr(workflows, "get", None)
        defn = get_workflow(name) if callable(get_workflow) else None
        if defn is None:
            return f"unknown workflow: {name!r}"
        runner = self._app.extra.get("subagent_runner")
        permissions = self._app.extra.get("workflow_permissions")
        if runner is None or permissions is None:
            return f"workflow {name!r} unavailable: no sub-agent runner wired"
        phase_runner = SubAgentPhaseRunner(
            runner=runner,  # type: ignore[arg-type]
            app=self._app,
            permissions=permissions,  # type: ignore[arg-type]
        )
        pipe = Pipeline(runner=phase_runner, events=self._app.events)
        if name == "coordinate":
            outcome = await pipe.coordinate(task)
        elif defn.phases:
            outcome = await pipe.run(list(defn.phases), max_retries=1)
        elif defn.runner is not None:
            # Runner-based workflow (e.g. /dream, /distill) — KDR-11: the runner
            # is pure assembly over injected interfaces; no Pipeline needed.
            from autoverse.app.workflow.self_improve import invoke_runner

            return invoke_runner(defn, self._app, self._session_id)
        else:
            return f"workflow {name!r} has no executable phases"
        tail = outcome.results[-1].output if outcome.results else ""
        # Child agents use a small completion protocol. It is useful to the
        # orchestrator but noisy when nested verbatim under the workflow heading.
        # Remove only the leading protocol banner; keep the substantive summary.
        cleaned = tail.strip()
        if cleaned.startswith("---"):
            cleaned = cleaned[3:].lstrip()
        if cleaned.upper().startswith("## COMPLETED"):
            cleaned = cleaned[len("## COMPLETED"):].lstrip()

        ok = outcome.cause.value == "end_turn"
        state = "completed" if ok else "stopped"
        heading = f"## {'✓' if ok else '✗'} /{name} {state}"
        reason = f"\n\n> {outcome.reason}" if outcome.reason else ""
        body = f"\n\n{cleaned}" if cleaned else ""
        return f"{heading}{reason}{body}".strip()

    async def aclose(self, reason: str = "normal") -> None:
        """Finalise side stores and extension sources on shutdown — best-effort."""
        # Release any leftover busy marker so the other frontend doesn't see a
        # stale ``busy_external`` state after this process exits.
        if self._busy is not None:
            self._busy.release(self._session_id)
        await self._close_session_worktree()
        for src in getattr(self._app, "extension_sources", ()):
            teardown = getattr(src, "teardown", None)
            if teardown is not None:
                try:
                    await teardown()
                except Exception:  # noqa: BLE001 — shutdown must not raise
                    pass
        trace = getattr(self._app, "trace", None)
        close = getattr(trace, "aclose", None)
        if close is not None:
            try:
                await close(reason)
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
        browser = getattr(self._app, "browser", None)
        close_browser = getattr(browser, "close", None)
        if close_browser is not None and bool(
            getattr(browser, "close_on_engine_shutdown", True)
        ):
            try:
                await close_browser()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
        retired_backends = self._app.extra.get("retired_browser_backends", [])
        if isinstance(retired_backends, list):
            for retired in retired_backends:
                close_retired = getattr(retired, "close", None)
                if close_retired is not None:
                    try:
                        await close_retired()
                    except Exception:  # noqa: BLE001 — shutdown must not raise
                        pass

    async def _close_session_worktree(self) -> None:
        handle = self._app.extra.get("session_worktree_handle")
        cm = self._app.extra.get("session_worktree_cm")
        if handle is None:
            return
        try:
            from autoverse.ports.isolation import FinalizeAction

            action_name = str(self._app.extra.get("session_worktree_on_end") or "keep")
            action = {
                "keep": FinalizeAction.KEEP,
                "merge": FinalizeAction.MERGE,
                "discard": FinalizeAction.DISCARD,
            }.get(action_name, FinalizeAction.KEEP)
            isolator = getattr(self._app, "isolator", None)
            finalize = getattr(isolator, "finalize", None)
            if callable(finalize):
                await finalize(handle, action)
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass
        try:
            exit_cm = getattr(cm, "__aexit__", None)
            if callable(exit_cm):
                await exit_cm(None, None, None)
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass
        self._app.extra.pop("session_worktree_handle", None)
        self._app.extra.pop("session_worktree_cm", None)


class MemStore:
    """In-process Store used as the default when no durable store is configured.

    Mirrors the 9-method Store Protocol; suitable for one-shot / offline / test
    runs where persistence is unnecessary.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, list[Message]] = {}
        self._meta: dict[str, SessionMeta] = {}

    async def append(self, session_id: str, *msgs: Message) -> None:
        self._sessions.setdefault(session_id, []).extend(msgs)

    async def load(self, session_id: str, *, since: int = 0) -> list[Message]:
        return list(self._sessions.get(session_id, []))[since:]

    async def rewrite(self, session_id: str, msgs: list[Message], reason: str) -> None:
        self._sessions[session_id] = list(msgs)

    async def create(self, session_id: str, meta: SessionMeta) -> None:
        self._sessions.setdefault(session_id, [])
        self._meta[session_id] = meta

    async def load_meta(self, session_id: str) -> SessionMeta:
        return self._meta.get(session_id, SessionMeta(session_id=session_id))

    async def update_meta(self, session_id: str, **fields: object) -> None:
        meta = self._meta.setdefault(session_id, SessionMeta(session_id=session_id))
        for k, v in fields.items():
            setattr(meta, k, v)

    async def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._meta.pop(session_id, None)


__all__ = ["EngineAdapter", "QueueEventSink", "MemStore"]
