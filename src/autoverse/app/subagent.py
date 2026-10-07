"""Production sub-agent wiring (gap8, docs/reference/subagents.md §3.2).

The :class:`SubAgentRunner` is pure domain logic that needs an ``engine_factory``
producing a child :class:`SubEngine` (something with ``async def run() -> RunOutcome``).
This module supplies the *production* factory: it recursively builds a real child
:class:`TurnLoop` with a fresh history, the role-filtered tool registry, a static
role system prompt (subagents are self-contained — docs/architecture/runtime-flow.md §8), and runs
it under the role's ``max_turns`` safety valve.

Lives in ``app/`` so it may import ``core`` + ``infra`` (P3). ``core`` never imports
this back; it only sees the duck-typed ``SubEngine`` Protocol.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, cast

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.app.engine import MemStore
from autoverse.app.options import EngineOptions
from autoverse.app.policy_map import (
    build_core_compaction_config,
    build_core_recovery_config,
)
from autoverse.core.context.compaction import CompactionPolicy
from autoverse.core.context.recovery import RecoveryPolicy
from autoverse.core.loop.turn_loop import TurnLoop
from autoverse.core.loop.types import StopCause, TurnOptions
from autoverse.core.state.conversation import Conversation
from autoverse.core.subagent.jobs import InMemoryJobStore, ResourceLock
from autoverse.core.subagent.runner import RunOutcome, SubAgentRunner
from autoverse.core.tools.dispatcher import ToolDispatcher
from autoverse.core.tools.registry import ToolRegistry
from autoverse.core.workflow.stop_gate import CompletionGate, GateRegistry
from autoverse.infra.isolation.worktree import GitWorktreeIsolator
from autoverse.infra.llm.registry import get_context_window
from autoverse.infra.persist import InMemoryCompactionArchive, InMemoryTurnIndexStore
from autoverse.ports import EventSink, LLMProvider, SessionMeta
from autoverse.types.events import (
    EventType,
    StreamEvent,
    SubagentActivityPayload,
    SubagentChildPayload,
    make_event,
)
from autoverse.types.messages import Message, TextBlock

# The kickoff turns the self-contained role prompt (which carries the Task) into
# an actionable conversation: the loop needs a user message to act on.
_KICKOFF = (
    "Proceed with the task described in your instructions. When you are done, "
    "end your turn with a concise summary of what you did and any findings."
)

ProviderFor = Callable[[str], LLMProvider]


class _StaticPrompt:
    """A minimal PromptAssembler stand-in: ``render()`` returns a fixed string.

    Sub-agents do NOT inherit the main-agent prompt (docs/architecture/runtime-flow.md §8); the
    runner already assembles a complete role prompt (role + task + context +
    isolation note), so the child loop just renders it verbatim."""

    def __init__(self, text: str) -> None:
        self._text = text

    def render(self) -> str:
        return self._text


# Child events that, if forwarded verbatim to the parent sink, would pollute the
# parent's scrollback AND its trace model (the collector folds child events into
# the same parent trace — there is no per-session split). We translate the turn
# boundary into a single heartbeat row and drop the rest; the child's substantive
# output still returns to the orchestrator as the SpawnResult.summary.
_HEARTBEAT_ON: frozenset[EventType] = frozenset({EventType.TURN_END})
_SUPPRESSED: frozenset[EventType] = frozenset(
    {
        EventType.TEXT_DELTA,
        EventType.THINKING_DELTA,
        EventType.TOOL_START,
        EventType.TOOL_END,
        EventType.TURN_START,
        EventType.TURN_END,
        EventType.USAGE,
        EventType.REQUEST,
        EventType.GATE_DECISION,
        EventType.COMPACTING,
    }
)


class _SubagentProgressSink:
    """Wraps the parent sink so a child engine reports as ONE live panel row.

    A sub-agent's child loop emits the full event stream (text/tool/turn/usage).
    Forwarding those verbatim leaks the child's internals into the parent TUI and
    trace (docs/reference/subagents.md §13 wants the parent to see *progress*, not the raw cards).
    This sink:

    * translates each child ``TURN_END`` into a parent ``SUBAGENT_ACTIVITY`` with
      ``status="running"``, an incrementing ``turn``, and elapsed-since-dispatch —
      keyed on the same ``job_id`` the runner uses, so the panel updates in place;
    * **suppresses the raw child cards from the parent SCROLLBACK** (text/thinking/
      tool/turn/usage/…) — but **wraps each one as ``SUBAGENT_CHILD``** so a
      front-end can RETAIN it in a per-``job_id`` transcript the user can open on
      demand ("enter the sub-agent's context"). The TUI routes ``SUBAGENT_CHILD``
      into that transcript, not the live scrollback; trace/other sinks ignore it.
    * **passes through** everything else (e.g. ERROR / SUBAGENT_ACTIVITY from a
      deeper level) so genuinely cross-cutting signals still reach the parent.

    The runner owns the ``running``→``done``/``failed`` lifecycle transitions; this
    sink only adds the in-between heartbeats.
    """

    def __init__(self, parent: EventSink, *, job_id: str, subagent_type: str) -> None:
        self._parent = parent
        self._job_id = job_id
        self._subagent_type = subagent_type
        self._started = time.monotonic()
        self._turn = 0
        # Count child tool calls so the parent can render MiMo's `N toolcalls`
        # footer. TOOL_END (not TOOL_START) so only completed calls are counted.
        self.tool_count = 0

    async def emit(self, event: StreamEvent) -> None:
        if event.type is EventType.TOOL_END:
            self.tool_count += 1
        if event.type in _HEARTBEAT_ON:
            self._turn += 1
            await self._parent.emit(
                make_event(
                    SubagentActivityPayload(
                        job_id=self._job_id,
                        subagent_type=self._subagent_type,
                        activity="running",
                        detail=f"turn {self._turn}",
                        status="running",
                        turn=self._turn,
                        elapsed_ms=(time.monotonic() - self._started) * 1000.0,
                    )
                )
            )
            return
        if event.type in _SUPPRESSED:
            # Keep the child's internals OUT of the parent scrollback, but wrap
            # them so a front-end can retain a per-job transcript for drill-down.
            await self._parent.emit(
                make_event(
                    SubagentChildPayload(
                        job_id=self._job_id,
                        subagent_type=self._subagent_type,
                        inner=event,
                    )
                )
            )
            return
        await self._parent.emit(event)

    async def aclose(self, reason: str = "normal") -> None:
        close = getattr(self._parent, "aclose", None)
        if close is not None:
            await close(reason)


class _SubEngine:
    """A child engine the runner drives via ``run()`` (docs/reference/subagents.md §3.2 SubEngine)."""

    def __init__(
        self,
        *,
        loop: TurnLoop,
        conversation: Conversation,
        store: MemStore,
        session_id: str,
        model: str,
        cwd: Path,
        max_turns: int,
        progress: "_SubagentProgressSink | None" = None,
    ) -> None:
        self._loop = loop
        self._conversation = conversation
        self._store = store
        self._session_id = session_id
        self._model = model
        self._cwd = cwd
        self._max_turns = max_turns
        # The progress sink counts child tool calls; read after the run for the
        # MiMo `N toolcalls` footer (None in tests that inject a bare loop).
        self._progress = progress

    async def run(self) -> RunOutcome:
        await self._store.create(
            self._session_id,
            SessionMeta(session_id=self._session_id, model=self._model, cwd=str(self._cwd)),
        )
        await self._conversation.hydrate()
        await self._conversation.append(
            Message(role="user", content=(TextBlock(text=_KICKOFF),))
        )
        result = await self._loop.run_turn(
            TurnOptions(
                max_iterations=max(1, self._max_turns),
                metadata={"final_iteration_text_only": True},
            )
        )
        summary = _text_of(result.final_message)
        degraded = "max_turns" if result.cause is StopCause.MAX_TURNS else None
        return RunOutcome(
            summary=summary,
            verdict=None,
            degraded=degraded,
            iterations=result.iterations,
            tool_count=self._progress.tool_count if self._progress else 0,
        )


def _text_of(message: Message | None) -> str:
    if message is None:
        return ""
    parts = [b.text for b in message.content if isinstance(b, TextBlock) and b.text]
    return "\n".join(parts).strip()


def build_engine_factory(
    parent_app: AppContext,
    config: Config,
    provider_for: ProviderFor,
) -> Callable[..., _SubEngine]:
    """Build the production ``engine_factory`` closure for a SubAgentRunner.

    The runner calls it with ``system_prompt``/``tools``/``model``/``cwd``/
    ``max_turns``/``bash_check_mode``/``process_lifecycle``. ``tools`` is the
    already role-filtered registry (check-mode bash is baked in by the filter, so
    ``bash_check_mode`` here is informational); ``process_lifecycle`` is handled
    by the runner's reaping, not the engine."""

    def factory(
        *,
        system_prompt: str,
        tools: Any = None,
        model: str = "",
        cwd: Path | None = None,
        isolation_root: Path | None = None,
        isolation_source_root: Path | None = None,
        max_turns: int = 60,
        bash_check_mode: bool = False,
        process_lifecycle: str = "inherit",
        dispatch_id: str = "",
        subagent_type: str = "subagent",
    ) -> _SubEngine:
        registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry.build(include=set())
        child_cwd = cwd or parent_app.cwd
        # Wrap the parent sink so the child reports as a single live panel row
        # (per-turn heartbeats) instead of leaking its raw cards (§13). Keyed on
        # the runner's dispatch_id so heartbeats land on the same row as the
        # running/done lifecycle events.
        progress = _SubagentProgressSink(
            parent_app.events, job_id=dispatch_id, subagent_type=subagent_type
        )
        sink: EventSink = progress
        provider = provider_for(model or parent_app.model)

        store = MemStore()
        session_id = f"sub-{uuid.uuid4().hex[:12]}"
        conversation = Conversation(store, session_id)

        child_app = AppContext(
            config=config,
            store=store,
            cwd=child_cwd,
            model=model or parent_app.model,
            events=sink,
            tools=registry,
            prompt=_StaticPrompt(system_prompt),
            secrets=parent_app.secrets,
            jobs=parent_app.jobs,
            trace=parent_app.trace,
            context_archive=InMemoryCompactionArchive(),
            spawn=parent_app.spawn,
        )
        # Browser page/profile ownership stays with the parent session or native
        # frontend. Sub-agents that receive browser tools share that same serial
        # port instead of creating a hidden competing browser.
        child_app.browser = parent_app.browser
        if isolation_root is not None:
            child_app.extra["isolation_root"] = str(isolation_root)
        if isolation_source_root is not None:
            child_app.extra["isolation_source_root"] = str(isolation_source_root)
        # Inherit the parent's permission gate so sub-agent tool calls stay gated.
        perms = getattr(parent_app, "permissions", None)
        if perms is not None:
            setattr(child_app, "permissions", perms)
        # Track recursion depth for future ceilings; harmless now
        # because no role sets can_spawn (the filter strips `agent` from children).
        depth = int(cast(int, parent_app.extra.get("subagent_depth", 0))) + 1
        child_app.extra["subagent_depth"] = depth

        dispatcher = ToolDispatcher(registry)
        dispatch: Any = dispatcher.dispatch
        hook_engine = parent_app.extra.get("hook_engine")
        middleware_factory = getattr(hook_engine, "middleware", None)
        if callable(middleware_factory):
            middleware = middleware_factory()

            async def _dispatch(calls: list[Any], ctx: Any) -> list[Any]:
                if not calls:
                    return []
                if dispatcher._plan_exit_barrier_active(calls, ctx):  # noqa: SLF001 - shared safety gate
                    return await dispatcher.dispatch(calls, ctx)
                tasks = [middleware(c, ctx, dispatcher.dispatch_one) for c in calls]
                return list(await asyncio.gather(*tasks))

            dispatch = _dispatch
        gates = GateRegistry()
        gates.register(CompletionGate())
        loop = TurnLoop(
            llm=provider,
            conversation=conversation,
            events=sink,
            # Sub-agents honour the same user-configured compaction/recovery
            # tunables as the main loop (single mapping, app/policy_map.py).
            # The window provider resolves the *child's* model window so a
            # deep-tier sub-agent on a 1M model gets the full budget.
            compaction=CompactionPolicy(
                provider,
                build_core_compaction_config(config.compaction),
                window_provider=lambda: get_context_window(
                    child_app.model or "",
                    override=(int(config.compaction.context_window or 0) or None),
                ),
                archive=child_app.context_archive,
                session_id=session_id,
            ),
            recovery=RecoveryPolicy(build_core_recovery_config(config.recovery)),
            gates=gates,
            dispatch_tools=dispatch,
            app=child_app,
            turn_indices=InMemoryTurnIndexStore(),
            session_id=session_id,
        )
        return _SubEngine(
            loop=loop,
            conversation=conversation,
            store=store,
            session_id=session_id,
            model=model or parent_app.model,
            cwd=child_cwd,
            max_turns=max_turns,
            progress=progress,
        )

    return factory


def build_subagent_runner(
    opts: EngineOptions,
    config: Config,
    app: AppContext,
    make_provider: Callable[[EngineOptions], LLMProvider],
) -> SubAgentRunner:
    """Assemble the production SubAgentRunner (engine_factory + ports).

    Also installs ``app.spawn`` (background task scheduler) and ``app.jobs`` (the
    job store the JobOutput tool reads) so background dispatch + fan-in work."""
    job_store = app.jobs if isinstance(app.jobs, InMemoryJobStore) else InMemoryJobStore()
    app.jobs = job_store

    if app.spawn is None:
        bg: set[asyncio.Task[Any]] = set()

        def _spawn(coro: Any) -> asyncio.Task[Any]:
            task: asyncio.Task[Any] = asyncio.ensure_future(coro)
            bg.add(task)
            task.add_done_callback(bg.discard)
            return task

        app.spawn = _spawn

    def provider_for(model: str) -> LLMProvider:
        return make_provider(replace(opts, model=model))

    factory = build_engine_factory(app, config, provider_for)
    return SubAgentRunner(
        engine_factory=factory,
        isolator=GitWorktreeIsolator(),
        resource_lock=ResourceLock(),
        job_store=job_store,
        events=app.events,
        spawn=app.spawn,
    )


__all__ = ["build_engine_factory", "build_subagent_runner", "_SubEngine"]
