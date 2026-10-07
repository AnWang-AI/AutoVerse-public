"""TurnLoop — the agentic main loop (docs/architecture/runtime-flow.md).

``run_turn`` orchestrates one user turn: it drives the LLM, dispatches tool
calls, manages compaction/recovery/stop-gates, and accumulates usage — but it
*delegates every judgement* to an injected policy/port and to private helpers,
so the loop body itself stays a thin, readable skeleton (P2). Compaction,
recovery, stopping, cancellation, and metering are never inlined.

``core`` purity: this module imports only ``ports``/``types`` and sibling
``core`` modules. The :class:`AppContext` reference is type-only.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from autoverse.core.context.compaction import CompactionPolicy, ContextBudgetError
from autoverse.core.context.recovery import RecoveryClass, RecoveryPolicy
from autoverse.core.loop.accumulator import CollectedTurn, UsageAccumulator, collect_assistant
from autoverse.core.loop.repair import merge_trailing_user, repair_orphaned_tool_use
from autoverse.core.loop.types import StopCause, TurnOptions, TurnResult
from autoverse.core.state.conversation import Conversation
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.core.workflow.stop_gate import GateDecision, GateRegistry, Verdict
from autoverse.ports import EventSink, LLMProvider, PermissionPort, TurnIndexPort
from autoverse.types import (
    AbortController,
    AbortError,
    AbortSignal,
    Decision,
    ErrorPayload,
    LLMRequest,
    RequestPayload,
    RetryPayload,
    StreamEvent,
    ToolSchema,
    TurnEndPayload,
    TurnStartPayload,
    Usage,
    make_event,
)
from autoverse.types.messages import (
    ContentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from autoverse.types.tools import ToolCall, ToolResult

if TYPE_CHECKING:
    from autoverse.app.context import AppContext

DispatchFn = Callable[[list[ToolCall], ToolContext], Awaitable[list[ToolResult]]]

_DEFAULT_MAX_TOKENS = 8192
_CONTINUE_PROMPT = "Please continue."


class _AllowAllPermissions:
    """Core-local permissive PermissionPort used when none is supplied via app."""

    async def check(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def check_sync(self, call: ToolCall) -> Decision:
        return Decision.allow()

    async def request_approval(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def cancel_all(self) -> None:
        return None


@dataclass
class _LLMOutcome:
    collected: CollectedTurn | None
    cause: StopCause | None


async def _collect_assistant_with_abort(
    stream: AsyncIterator[StreamEvent],
    events: EventSink,
    signal: AbortSignal,
) -> CollectedTurn:
    """Consume an LLM stream while making abort wake a blocked network read."""
    if signal.aborted:
        raise AbortError(signal.reason)

    collect_task = asyncio.create_task(collect_assistant(stream, events))
    abort_task = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            {collect_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # Abort wins ties: once the user has requested cancellation, do not
        # report a concurrently completed transport read as a successful turn.
        if abort_task in done:
            collect_task.cancel()
            await asyncio.gather(collect_task, return_exceptions=True)
            raise AbortError(signal.reason)
        return collect_task.result()
    finally:
        if not abort_task.done():
            abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)


class TurnLoop:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        conversation: Conversation,
        events: EventSink,
        compaction: CompactionPolicy,
        recovery: RecoveryPolicy,
        gates: GateRegistry,
        dispatch_tools: DispatchFn,
        app: "AppContext",
        initial_turn_index: int = 0,
        turn_indices: TurnIndexPort | None = None,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self.conversation = conversation
        self._events = events
        self._compaction = compaction
        self._recovery = recovery
        self._gates = gates
        self._dispatch = dispatch_tools
        self._app = app
        self._tracker = FileTracker()
        self._turn_index = max(0, initial_turn_index)
        self._turn_indices = turn_indices
        self._session_id = session_id
        self._controller = AbortController()
        # message snapshot_hash -> SnapshotRef, for /rewind (docs/reference/tools.md §9.4).
        self._turn_snapshots: dict[str, Any] = {}
        self._wrapup_note = ""
        self._force_text_only = False
        # Tool schemas are derived once from the registry (P1) and cached: the
        # registry is fixed for the session, so re-deriving per request is waste.
        self._tool_schemas_cache: tuple[ToolSchema, ...] | None = None

    # ── public ──────────────────────────────────────────────────────────────
    def abort(self, reason: str = "cancel") -> None:
        self._controller.abort("interrupt" if reason == "interrupt" else "cancel")

    async def run_turn(self, options: TurnOptions | None = None) -> TurnResult:
        options = options or TurnOptions()
        signal = self._controller.signal
        try:
            if self._turn_indices is not None and self._session_id:
                turn_index = await self._turn_indices.reserve(
                    self._session_id, minimum=self._turn_index
                )
                self._turn_index = turn_index + 1
            else:
                turn_index = self._turn_index
                self._turn_index += 1
            self._recovery.begin_turn()
            self._wrapup_note = ""
            self._force_text_only = False
            usage = UsageAccumulator()
            gate_decisions: list[GateDecision] = []
            await self._events.emit(
                make_event(TurnStartPayload(turn_index=turn_index, iteration=0, model=self._model()))
            )

            iteration = 0
            final_message: Message | None = None
            cause = StopCause.END_TURN
            while True:
                if self._plan_approval_pending():
                    cause = StopCause.PLAN_PROPOSED
                    break
                if iteration >= options.max_iterations:
                    cause = StopCause.MAX_TURNS
                    break
                if signal.aborted:
                    cause = StopCause.ABORTED
                    break
                iteration += 1
                self._maybe_wrap_up(iteration, options)
                try:
                    await self._maybe_compact(options, turn_index)
                except ContextBudgetError as exc:
                    await self._emit_error(exc, recoverable=False)
                    cause = StopCause.UNRECOVERABLE
                    break
                outcome = await self._call_llm_with_recovery(signal, options, turn_index)
                if outcome.cause is not None:
                    cause = outcome.cause
                    break
                assert outcome.collected is not None
                collected = outcome.collected
                await self.conversation.append(collected.message)
                final_message = collected.message
                usage.add(collected.usage)
                if collected.tool_calls:
                    await self._handle_tool_calls(collected.tool_calls, signal)
                    if self._plan_approval_pending():
                        cause = StopCause.PLAN_PROPOSED
                        break
                    continue
                if self._recovery.maybe_resume_text(collected.stop_reason):
                    continue
                decision = await self._gates.evaluate_all(
                    self.conversation.view(), self._app, self._events, options.gate_ids
                )
                gate_decisions.append(decision)
                if decision.verdict is Verdict.CONTINUE:
                    await self.conversation.append(self._continue_message(decision))
                    continue
                break

            return await self._finish(
                turn_index, iteration, cause, final_message, usage.usage, gate_decisions,
                reason=signal.reason if (cause is StopCause.ABORTED and signal.aborted) else None,
            )
        finally:
            # Keep one controller for the next-or-current turn. Rotating it here
            # preserves an abort sent after scheduling but before run_turn starts.
            if self._controller.signal is signal:
                self._controller = AbortController()

    def set_provider(self, llm: LLMProvider) -> None:
        """Hot-swap the LLM provider (used on a model switch).

        A provider is bound to one model's transport/auth at build time (the
        Router resolves model→spec→transport up front and ``WireProvider`` sends
        ``spec.wire_model``, ignoring per-request model). So switching models must
        replace the provider, not just update ``app.model`` — otherwise requests
        keep hitting the old provider's endpoint/credential. Called between turns
        by the engine's ``switch_model``."""
        self._llm = llm

    # ── helpers (all judgement lives here, not in run_turn) ──────────────────
    def _model(self) -> str:
        return str(getattr(self._app, "model", "") or "")

    def _plan_approval_pending(self) -> bool:
        extra = getattr(self._app, "extra", {}) or {}
        plan_mode = extra.get("plan_mode") if isinstance(extra, dict) else None
        return bool(isinstance(plan_mode, dict) and plan_mode.get("pending_approval"))

    async def _maybe_compact(self, options: TurnOptions, turn_index: int) -> None:
        self._compaction.note_turn()
        if not options.allow_compaction:
            return
        # Rebuild and re-measure after every rewrite. Emergency truncation can
        # need more than one pass when the protected tail itself contains a very
        # large message; never send a stale pre-compaction request estimate.
        for _ in range(4):
            view = self.conversation.view()
            request = self._build_request(options, consume_override=False)
            outcome = await self._compaction.maybe_compact(
                view.messages,
                self._events,
                request=request,
                turn_index=turn_index,
                signal=self._controller.signal,
            )
            if not outcome.changed:
                if not await self._compaction.fits(view.messages, request):
                    raise ContextBudgetError(
                        "Complete request cannot fit the active model context window"
                    )
                return
            await self.conversation.rewrite(
                list(outcome.messages), reason=outcome.kind
            )
        final_view = self.conversation.view()
        final_request = self._build_request(options, consume_override=False)
        if not await self._compaction.fits(final_view.messages, final_request):
            raise ContextBudgetError(
                "Context remains over budget after the maximum compaction passes"
            )

    async def compact_now(self) -> dict[str, int | bool | str]:
        """Force a manual compaction and rewrite history when it changes."""
        view = self.conversation.view()
        outcome = await self._compaction.force_compact(view.messages, self._events)
        if outcome.changed:
            await self.conversation.rewrite(list(outcome.messages), reason=outcome.kind)
        return {
            "tokens_before": outcome.tokens_before,
            "tokens_after": outcome.tokens_after,
            "changed": outcome.changed,
            "kind": outcome.kind,
        }

    def _maybe_wrap_up(self, iteration: int, options: TurnOptions) -> None:
        remaining = options.max_iterations - iteration
        if remaining == 3:
            self._wrapup_note = "You are approaching the iteration limit; begin wrapping up."
        elif remaining == 1:
            final_note = "This is the final iteration; produce your final answer now."
            if options.metadata.get("final_iteration_text_only"):
                self._force_text_only = True
                final_note = (
                    f"{final_note} Do NOT call tools. Summarize the evidence and "
                    "progress already available, and include an explicit verdict or "
                    "blocker if the task cannot be completed."
                )
            self._wrapup_note = final_note

    def _build_request(
        self, options: TurnOptions, *, consume_override: bool = True
    ) -> LLMRequest:
        messages = repair_orphaned_tool_use(list(self.conversation.view().messages))
        messages = merge_trailing_user(messages)
        messages = self._request_messages(messages)
        override = (
            self._recovery.consume_max_tokens_override() if consume_override else None
        )
        model_cfg = self._model_config()
        cfg_max = int(getattr(model_cfg, "max_tokens", _DEFAULT_MAX_TOKENS) or _DEFAULT_MAX_TOKENS)
        max_tokens = override or cfg_max
        temperature = float(getattr(model_cfg, "temperature", 1.0))
        system = self._system_prompt()
        req = LLMRequest(
            messages=tuple(messages),
            system=system,
            tools=self._tool_schemas(),
            model=self._model(),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return req

    def _request_messages(self, messages: list[Message]) -> list[Message]:
        """Apply persisted per-message LLM-only context and overrides.

        Frontends can preserve the human-facing message exactly as submitted
        (e.g. text + ImageBlock) while giving a text-only model a compatible
        request body or prepending runtime state that should not be rendered as
        user-authored text. These changes are deliberately request-local:
        persistence, replay, title generation, and compaction continue to see
        the original message.
        """
        vision_enabled = self._vision_enabled()
        out: list[Message] = []
        for msg in messages:
            meta = getattr(msg, "metadata", {}) or {}
            override = meta.get("llm_text_override") if isinstance(meta, dict) else None
            prefix = meta.get("llm_context_prefix") if isinstance(meta, dict) else None
            content = msg.content
            if (
                msg.role == "user"
                and not vision_enabled
                and isinstance(override, str)
                and override.strip()
            ):
                content = (TextBlock(text=override),)
            if msg.role == "user" and isinstance(prefix, str) and prefix.strip():
                content = (TextBlock(text=prefix), *content)
            if content != msg.content:
                out.append(
                    Message(
                        role=msg.role,
                        content=content,
                        metadata=msg.metadata,
                    )
                )
            else:
                out.append(msg)
        return out

    def _model_config(self) -> object | None:
        """The user-configured model tunables (``max_tokens``/``temperature``).

        Read duck-typed off ``app.config.model`` so core never imports the app
        config tree (P3); an absent/odd shape falls back to LLMRequest defaults
        via the ``getattr`` defaults at the call site (P5)."""
        cfg = getattr(self._app, "config", None)
        return getattr(cfg, "model", None)

    def _tool_schemas(self) -> tuple[ToolSchema, ...]:
        """Derive the request tool schemas from the injected registry (P1, P3).

        Duck-typed via ``app.tools`` so core never hard-depends on a concrete
        ToolRegistry; if no registry (or derivation fails) the loop degrades to a
        text-only turn rather than crashing (P5)."""
        if self._force_text_only:
            return ()
        if self._tool_schemas_cache is not None:
            return self._tool_schemas_cache
        schemas: tuple[ToolSchema, ...] = ()
        reg = getattr(self._app, "tools", None)
        if reg is not None:
            try:
                getter = getattr(reg, "tool_schemas", None)
                if callable(getter):
                    schemas = tuple(getter())
                else:
                    raw = reg.schemas() if hasattr(reg, "schemas") else []
                    schemas = tuple(
                        ToolSchema(
                            name=str(d.get("name", "")),
                            description=str(d.get("description", "")),
                            json_schema=dict(d.get("input_schema", {})),
                        )
                        for d in raw
                    )
            except Exception:  # noqa: BLE001 — never break a turn on tool derivation
                schemas = ()
        self._tool_schemas_cache = schemas
        return schemas

    def _system_prompt(self) -> str:
        # Session-level base prompt from the injected PromptAssembler
        # (docs/architecture/runtime-flow.md §7). Duck-typed via AppContext.prompt so core does
        # not hard-depend on a concrete assembler (P3); assembly failure degrades
        # to empty rather than crashing the loop (P5).
        base = ""
        prompt = getattr(self._app, "prompt", None)
        if prompt is not None:
            try:
                base = prompt.render() or ""
            except Exception:  # noqa: BLE001 — never break the turn on prompt build
                base = ""
        extra = getattr(self._app, "extra", {}) or {}
        plan_mode = extra.get("plan_mode")
        worktree_mode = extra.get("worktree_mode")
        # Interaction modes are mutually exclusive (docs/reference/subagents.md §5a.2); plan takes
        # precedence in the unlikely event both flags are set.
        if isinstance(plan_mode, dict) and plan_mode.get("active"):
            reason = str(plan_mode.get("reason") or "")
            base = (
                f"{base}\n\n## Plan Mode\n\n"
                "You are in planning mode: research only, do not change anything. "
                "Do NOT edit files or run mutating tools, and do NOT dispatch "
                "sub-agents (e.g. builder/generalist) to make changes on your "
                "behalf — write-tier tools are hard-blocked while plan mode is "
                "active. Read, search, and analyse to understand the work.\n"
                "If you hit a decision you cannot resolve yourself, batch all such "
                "questions into a single AskUserQuestion call. When ready, call "
                "ExitPlanMode exactly once with a converged, numbered list of "
                "implementation steps (no unresolved 'Option A/B' alternatives). "
                "ExitPlanMode submits the plan for user approval; it does not "
                "authorize implementation, and you must not call any write or "
                "execution tools in the same response."
                + (f"\n\nReason: {reason}" if reason else "")
            ).strip()
        elif isinstance(worktree_mode, dict) and worktree_mode.get("active"):
            branch = str(worktree_mode.get("branch") or "")
            # The Environment section's "working directory" is frozen at session
            # build time and does NOT update when we switch into the worktree, so
            # state the live worktree cwd here and tell the model it supersedes the
            # stale value — otherwise an absolute path copied from Environment would
            # write OUTSIDE the box (leak past isolation).
            cwd = str(getattr(self._app, "cwd", "") or "")
            base = (
                f"{base}\n\n## Worktree Mode\n\n"
                "You are running inside an isolated git worktree"
                + (f" (branch {branch})" if branch else "")
                + ". All file edits and commands you run apply to this worktree "
                "only and do NOT touch the main work-tree or its branch."
                + (
                    f" Your working directory is now `{cwd}`; use it as the base for "
                    "any absolute path — it supersedes the working directory shown "
                    "in the Environment section above."
                    if cwd
                    else ""
                )
                + " Any tool access to the original source-repo path is redirected "
                "to this isolated worktree; output from `git -C <source-root> ...` "
                "or file tools pointed at `<source-root>/...` is NOT evidence about "
                "the main work-tree while this mode is active. To inspect base "
                "branch content without leaving the worktree, use git object reads "
                "such as `git show main:path/to/file`."
                + " Before exiting, commit your changes (git add + git commit). "
                "ExitWorktreeMode(action='keep') will be rejected if you have "
                "uncommitted changes — it returns an error so you can commit "
                "first, then retry. Use action='merge' to auto-commit and "
                "merge back to the base branch, or action='discard' to throw "
                "away the changes. After exiting, explain what the user should "
                "do to review the branch."
            ).strip()
        # Turn-level wrap-up note is appended at the exit point (docs/architecture/runtime-flow.md §6.5).
        if self._wrapup_note:
            base = f"{base}\n\n{self._wrapup_note}".strip()
        return base

    async def _emit_request(self, req: LLMRequest, turn_index: int) -> None:
        await self._events.emit(
            make_event(
                RequestPayload(
                    turn_index=turn_index,
                    system=req.system,
                    system_sha256=_sha256(req.system),
                    tool_names=tuple(t.name for t in req.tools),
                    tools_sha256=_sha256("".join(t.name for t in req.tools)),
                    model=req.model,
                    max_tokens=req.max_tokens,
                    temperature=req.temperature,
                    thinking_budget=req.thinking_budget,
                )
            )
        )

    async def _call_llm_with_recovery(
        self, signal: AbortSignal, options: TurnOptions, turn_index: int
    ) -> _LLMOutcome:
        while True:
            req = self._build_request(options)
            await self._emit_request(req, turn_index)
            try:
                raw_prompt_tokens = await self._count_request_tokens(req)
                collected = await _collect_assistant_with_abort(
                    self._llm.stream(req, signal), self._events, signal
                )
                self._compaction.calibrate(
                    raw_prompt_tokens,
                    collected.usage.input_tokens
                    + collected.usage.cache_read_tokens,
                )
                return _LLMOutcome(collected=collected, cause=None)
            except AbortError:
                # A mid-stream cancel is not a recoverable error: terminate the
                # turn as ABORTED instead of letting generic recovery misclassify
                # it as a model fallback (AbortError carries no http_status/kind).
                return _LLMOutcome(collected=None, cause=StopCause.ABORTED)
            except Exception as exc:  # noqa: BLE001 — recovery classifies, never bubbles
                cause = await self._recover(exc, options)
                if cause is not None:
                    return _LLMOutcome(collected=None, cause=cause)

    async def _recover(self, exc: Exception, options: TurnOptions) -> StopCause | None:
        if bool(getattr(exc, "retry_exhausted", False)):
            await self._emit_error(exc, recoverable=False)
            return StopCause.RETRY_EXHAUSTED
        action = self._recovery.classify(exc)
        if action.cls is RecoveryClass.UNRECOVERABLE:
            await self._emit_error(exc, recoverable=False)
            return StopCause.UNRECOVERABLE
        if action.should_compact and options.allow_compaction:
            # COMPACT_RETRY must be bounded: a backend that keeps reporting
            # context_overflow even after compaction would otherwise loop
            # forever. Consume the retry budget and require real progress
            # (token reduction); escalate to RETRY_EXHAUSTED otherwise.
            if not self._recovery.can_retry():
                await self._emit_error(exc, recoverable=False)
                return StopCause.RETRY_EXHAUSTED
            self._recovery.note_retry()
            view = self.conversation.view()
            request = self._build_request(options, consume_override=False)
            out = await self._compaction.force_compact(
                view.messages,
                self._events,
                request=request,
                turn_index=self._turn_index - 1,
                signal=self._controller.signal,
            )
            if not out.changed or out.tokens_after >= out.tokens_before:
                await self._emit_error(exc, recoverable=False)
                return StopCause.RETRY_EXHAUSTED
            await self.conversation.rewrite(list(out.messages), reason=out.kind)
            return None
        if action.should_retry and self._recovery.can_retry():
            self._recovery.note_retry()
            await self._emit_retry(exc, action.reason)
            return None
        if action.should_fallback and self._recovery.can_fallback():
            # Transport/model fallback belongs to the provider, which can
            # actually rebuild credentials/codecs. Retrying the same provider
            # here would only pretend to fall back.
            self._recovery.note_fallback()
            await self._emit_error(exc, recoverable=False)
            return StopCause.UNRECOVERABLE
        await self._emit_error(exc, recoverable=False)
        return StopCause.RETRY_EXHAUSTED

    async def _count_request_tokens(self, request: LLMRequest) -> int:
        try:
            return await self._llm.count_tokens(request)
        except Exception:  # noqa: BLE001 — usage calibration is best-effort
            return max(1, self.conversation.view().token_estimate)

    async def _handle_tool_calls(
        self, tool_calls: tuple[ToolUseBlock, ...], signal: AbortSignal
    ) -> None:
        calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input)) for b in tool_calls]
        # Per-turn workspace snapshot (docs/reference/tools.md §9.4, KDR-12): capture state
        # before this turn's mutations so /rewind can restore the workspace to
        # the moment of the bound message. Best-effort — a snapshot failure must
        # not block tool execution (degrade with the ref unset).
        snapshot_hash = await self._track_snapshot()
        ctx = self._tool_ctx(signal)
        results = await self._dispatch(calls, ctx)
        msg = self._to_result_blocks(tool_calls, results)
        if snapshot_hash:
            msg = _with_snapshot_hash(msg, snapshot_hash)
        await self.conversation.append(msg)

    async def _track_snapshot(self) -> str:
        snap = getattr(self._app, "snapshot", None)
        track = getattr(snap, "track", None)
        if track is None:
            return ""
        try:
            ref = await track()
        except Exception:  # noqa: BLE001 — snapshot is a safety net, never fatal
            return ""
        if ref is None:
            return ""
        hash_ = str(getattr(ref, "hash", ""))
        if hash_:
            self._turn_snapshots[hash_] = ref
        return hash_

    # ── /rewind (docs/architecture/runtime-flow.md §3.3a, KDR-12) ─────────────────────────────────
    async def resolve_rewind_target(self, anchor: str) -> Any | None:
        """Resolve a message anchor / snapshot hash to a SnapshotRef, or None.

        ``anchor`` is the ``snapshot_hash`` carried in a tool-result message's
        metadata. Empty anchor selects the most recent snapshot.
        """
        if not anchor:
            if not self._turn_snapshots:
                return None
            last_key = list(self._turn_snapshots)[-1]
            return self._turn_snapshots[last_key]
        cached = self._turn_snapshots.get(anchor)
        if cached is not None:
            return cached
        snap = getattr(self._app, "snapshot", None)
        resolve = getattr(snap, "resolve", None)
        if resolve is None:
            return None
        ref = await resolve(anchor)
        if ref is not None:
            self._turn_snapshots[anchor] = ref
        return ref

    async def preview_rewind(self, ref: Any) -> str:
        snap = getattr(self._app, "snapshot", None)
        diff = getattr(snap, "diff", None)
        if diff is None or ref is None:
            return ""
        return str(await diff(ref))

    async def rewind(self, ref: Any) -> tuple[str, ...]:
        """Joint revert: restore workspace files to ``ref`` then truncate history.

        Fail-closed: if the file restore raises, history is left untouched and
        the error propagates (no half-applied rewind).
        """
        changed = await self.restore_snapshot(ref)
        # History side: truncate the conversation to the bound message. The
        # snapshot was captured *before* that turn's mutations, so we drop the
        # messages from the matching turn onward.
        await self._truncate_history_to_snapshot(str(getattr(ref, "hash", "")))
        return changed

    async def capture_rewind_point(self) -> str:
        """Capture the workspace at a frontend-defined turn boundary."""
        snap = getattr(self._app, "snapshot", None)
        capture = getattr(snap, "track_rewind", None)
        if capture is None:
            snapshot_hash = await self._track_snapshot()
            if not snapshot_hash:
                raise RuntimeError("could not capture workspace before the turn")
            return snapshot_hash
        ref = await capture()
        snapshot_hash = str(getattr(ref, "hash", ""))
        if not snapshot_hash:
            raise RuntimeError("could not capture workspace before the turn")
        self._turn_snapshots[snapshot_hash] = ref
        return snapshot_hash

    async def restore_snapshot(self, ref: Any) -> tuple[str, ...]:
        """Restore only workspace files; callers own the matching history rewrite."""
        snap = getattr(self._app, "snapshot", None)
        restore = getattr(snap, "restore", None)
        if restore is None or ref is None:
            raise RuntimeError("workspace snapshots are unavailable")
        return tuple(await restore(ref))

    async def _truncate_history_to_snapshot(self, snapshot_hash: str) -> None:
        if not snapshot_hash:
            return
        view = self.conversation.view()
        kept: list[Message] = []
        for msg in view.messages:
            meta = getattr(msg, "metadata", {}) or {}
            if isinstance(meta, dict) and meta.get("snapshot_hash") == snapshot_hash:
                break
            kept.append(msg)
        await self.conversation.rewrite(kept, reason="rewind")

    def _tool_ctx(self, signal: AbortSignal) -> ToolContext:
        perms = getattr(self._app, "permissions", None)
        permissions: PermissionPort = perms if isinstance(perms, PermissionPort) else _AllowAllPermissions()
        isolation_root, isolation_source = self._isolation_paths()
        return ToolContext(
            cwd=self._app.cwd,
            permissions=permissions,
            tracker=self._tracker,
            signal=signal,
            events=self._events,
            app=self._app,
            isolation_root=isolation_root,
            isolation_source_root=isolation_source,
        )

    def _isolation_paths(self) -> tuple[Any, Any]:
        extra = getattr(self._app, "extra", None)
        if not isinstance(extra, dict):
            return None, None
        worktree_mode = extra.get("worktree_mode")
        if isinstance(worktree_mode, dict) and worktree_mode.get("active"):
            handle = worktree_mode.get("handle")
            source = (
                getattr(handle, "source_root", None)
                or worktree_mode.get("source_root")
                or worktree_mode.get("base_cwd")
            )
            root = (
                getattr(handle, "cwd", None)
                or worktree_mode.get("worktree_root")
                or getattr(self._app, "cwd", None)
            )
            return root, source
        root = extra.get("isolation_root")
        source = extra.get("isolation_source_root")
        if root:
            return root, source
        return None, None

    @staticmethod
    def _result_block_metadata(res: ToolResult) -> dict[str, Any]:
        """Persisted display metadata for a tool result (replay fidelity).

        ``ToolResult.error_kind`` is a first-class field but ``ToolResultBlock``
        only carries ``metadata`` — without folding it in, session replay
        (renderer.render_history_blocks) loses the benign-vs-hard error
        distinction the live ``ToolEndPayload`` had. Stash it under
        ``metadata['error_kind']`` for errors so a detached turn's failures fold
        on switch-back exactly as they did live (docs/architecture/frontend-adapters.md §5.6.3)."""
        md = dict(res.metadata)
        if res.is_error and res.error_kind and "error_kind" not in md:
            md["error_kind"] = res.error_kind
        return md

    def _to_result_blocks(
        self, tool_calls: tuple[ToolUseBlock, ...], results: list[ToolResult]
    ) -> Message:
        blocks: list[ContentBlock] = [
            ToolResultBlock(
                tool_use_id=call.id,
                content=res.output,
                is_error=res.is_error,
                metadata=self._result_block_metadata(res),
            )
            for call, res in zip(tool_calls, results)
        ]
        if not blocks:
            blocks = [ToolResultBlock(tool_use_id="", content="", is_error=True)]
        # Lift tool-produced images (e.g. generate_image, docs/reference/tools.md §11.7) back
        # into the conversation as vision blocks so the model can reason about
        # what it just produced. Only for vision-capable models — otherwise the
        # codec would reject the request (§10); the text output still names the
        # path, so non-vision models lose nothing actionable (P5).
        if self._vision_enabled():
            blocks.extend(_result_image_blocks(results))
        return Message(role="user", content=tuple(blocks))

    def _vision_enabled(self) -> bool:
        caps = getattr(self._llm, "capabilities", None)
        return bool(getattr(caps, "vision", False))

    @staticmethod
    def _continue_message(decision: GateDecision) -> Message:
        if decision.injected_message is not None:
            return decision.injected_message
        return Message(role="user", content=(TextBlock(text=_CONTINUE_PROMPT),))

    async def _emit_retry(self, exc: Exception, reason: str) -> None:
        await self._events.emit(
            make_event(
                RetryPayload(
                    attempt=self._recovery.retry_count,
                    reason=reason or str(exc),
                    error_class=type(exc).__name__,
                    http_status=getattr(exc, "http_status", None),
                )
            )
        )

    async def _emit_error(self, exc: Exception, *, recoverable: bool) -> None:
        kind = str(
            getattr(getattr(exc, "kind", None), "value", "")
            or getattr(exc, "error_kind", "")
        )
        if kind in {"rate_limit", "server", "timeout"}:
            error_class = "retryable"
        elif kind in {"auth", "unsupported"}:
            error_class = "fallback"
        elif kind in {"abort", "bad_request"}:
            error_class = "permanent"
        else:
            error_class = type(exc).__name__
        await self._events.emit(
            make_event(
                ErrorPayload(
                    message=str(exc),
                    error_class=error_class,
                    recoverable=recoverable,
                    http_status=getattr(exc, "http_status", None),
                    error_kind=kind,
                    platform_code=getattr(exc, "platform_code", None),
                    phase="llm",
                )
            )
        )

    async def _finish(
        self,
        turn_index: int,
        iterations: int,
        cause: StopCause,
        final_message: Message | None,
        usage: Usage,
        gate_decisions: list[GateDecision],
        *,
        reason: str | None = None,
    ) -> TurnResult:
        await self._events.emit(
            make_event(
                TurnEndPayload(
                    turn_index=turn_index, iterations=iterations, stop_cause=cause.name
                )
            )
        )
        return TurnResult(
            final_message=final_message,
            cause=cause,
            iterations=iterations,
            usage_total=usage,
            gate_decisions=tuple(gate_decisions),
            reason=reason,
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _with_snapshot_hash(msg: Message, snapshot_hash: str) -> Message:
    """Return a copy of ``msg`` carrying the per-turn snapshot hash in metadata.

    This is the message↔snapshot binding that lets /rewind locate the workspace
    state captured before this turn's mutations (docs/reference/tools.md §9.4)."""
    meta = dict(msg.metadata)
    meta["snapshot_hash"] = snapshot_hash
    return Message(role=msg.role, content=msg.content, metadata=meta)


def _result_image_blocks(results: list[ToolResult]) -> list[ImageBlock]:
    """Extract base64 images a tool stashed in ``metadata['images']`` (§11.7).

    Tolerates malformed metadata defensively (a bad tool must never crash the
    loop): non-string entries and non-list payloads are skipped."""
    blocks: list[ImageBlock] = []
    for res in results:
        if res.is_error:
            continue
        images = res.metadata.get("images")
        if not isinstance(images, (list, tuple)):
            continue
        media_type = str(res.metadata.get("image_media_type") or "image/png")
        for data in images:
            if isinstance(data, str) and data:
                blocks.append(ImageBlock(media_type=media_type, data=data))
    return blocks


__all__ = ["TurnLoop", "DispatchFn"]
