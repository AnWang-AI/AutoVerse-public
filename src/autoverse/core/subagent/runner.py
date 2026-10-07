"""SubAgentRunner — the orchestration core (docs/reference/subagents.md §3.2).

A pure domain object that depends only on injected Ports: ``engine_factory``,
``isolator``, ``resource_lock``, ``job_store``, ``events``. All isolation / lock /
filter / audit / fan-in / lifecycle logic is collected here (P2); ``AgentTool``
(infra) is a thin shell over ``spawn``.

Lifecycle of one dispatch (§8.1):
  dispatch → lock (sorted) → isolation (fail-closed) → filter → engine_factory →
  safety valves → audit-then-finalize (audit BEFORE commit) → bound reap → result.

``core`` purity: imports only ``types``/``ports``/sibling ``core`` modules + stdlib.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, Field

from autoverse.core.subagent.audit import TAINT_PREFIX
from autoverse.core.subagent.definition import (
    AgentDefinition,
    get_definition,
    is_known_definition,
)
from autoverse.core.subagent.filter import filter_registry
from autoverse.core.subagent.jobs import reap_process_group
from autoverse.core.subagent.prompts import (  # noqa: F401 — re-exported for callers
    CHECKPOINT_WRITER_PROMPT,
)
from autoverse.ports import EventSink
from autoverse.ports.isolation import (
    FinalizeAction,
    IsoHandle,
    Isolator,
    IsoSpec,
)
from autoverse.types.abort import AbortSignal
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import SubagentActivityPayload, make_event

if TYPE_CHECKING:
    from autoverse.core.tools.context import ToolContext

_DEFAULT_WALL_CLOCK = 3600.0
_MAX_TASK_CHARS = 32_000  # task body cap injected into the child prompt (§3.2)

# System-dispatched roles exempt from the plan-mode capability downgrade: they
# are not user-selectable and represent infrastructure writes (e.g. memory
# reconciliation), not user-driven edits the plan gate intends to block.
_PLAN_MODE_DOWNGRADE_EXEMPT: frozenset[str] = frozenset({"checkpoint-writer"})


def _parent_plan_mode_active(ctx: "ToolContext") -> bool:
    """True when the parent session is in plan mode (docs/guides/using-cli-and-tui.md §3.10).

    Same single source of truth read by ToolDispatcher and TurnLoop:
    ``app.extra["plan_mode"]["active"]``. Plan mode hard-blocks write-tier tools
    in the dispatcher; this lets the runner extend that block recursively to
    spawned sub-agents so they cannot be used to bypass it."""
    extra = getattr(ctx.app, "extra", None)
    pm = extra.get("plan_mode") if isinstance(extra, dict) else None
    return bool(isinstance(pm, dict) and pm.get("active"))


class SpawnRequest(BaseModel):
    """A complete dispatch intent — explicit params, no implicit probing (P4).

    Also the single-source schema for ``AgentTool`` (P1): JSON Schema is derived
    via ``model_json_schema()``. ``subagent_type`` has no default → omitting it is
    a pydantic ValidationError (S1), backed by a defensive ValueError in spawn.
    """

    subagent_type: str = Field(..., description="Explicit role; never inferred.")
    description: str = Field(
        "", description="A short (3-5 words) description of the task (card label)."
    )
    task: str = Field("", description="Task description (scope-injected, ~32k chars).")
    context: str = Field("", description="Background (unbounded; keep <2000 tokens).")
    review_mode: str | None = Field(None, description="reviewer only: compliance|quality")
    model: str | None = Field(None, description="Per-call model override (highest).")
    run_in_background: bool = Field(False, description="Only decides whether to await.")
    isolation: str = Field("none", description="none|worktree; agent_def may force.")
    target_cwd: str | None = Field(
        None,
        description=(
            "Optional working directory/repository for the child. When "
            "isolation='worktree', the worktree is created from this repo instead "
            "of the parent session cwd."
        ),
    )
    exclusive_resources: tuple[str, ...] = Field(
        default=(), description="Resource lock keys (sorted acquisition)."
    )


@dataclass(frozen=True)
class SpawnResult:
    """Structured result back to the orchestrator — guaranteed non-empty (P5)."""

    subagent_type: str
    summary: str
    verdict: str | None = None
    tainted: bool = False
    degraded: str | None = None  # max_turns | wall_clock | None
    worktree_commit: str | None = None
    worktree_branch: str | None = None
    worktree_path: str | None = None
    trace_id: str | None = None
    tool_count: int = 0  # child tool calls observed (MiMo `N toolcalls` footer)


@dataclass
class RunOutcome:
    """What the child engine returns from one run (degraded carries the cause)."""

    summary: str
    verdict: str | None = None
    degraded: str | None = None  # max_turns | wall_clock | None
    iterations: int = 0
    tool_count: int = 0  # child tool calls observed during the run
    pgids: tuple[int, ...] = field(default_factory=tuple)  # bound process groups


class SubEngine(Protocol):
    """The child engine the factory produces; the runner only calls ``run``."""

    async def run(self) -> RunOutcome: ...


EngineFactory = Callable[..., SubEngine]
SpawnCoro = Callable[[Awaitable[Any]], Any]


def _resolve_model(per_call: str | None, agent_def: str | None, parent: str) -> str:
    """Single-pass model resolution, no sentinel (§3.4): per-call > def > parent."""
    return per_call or agent_def or parent


def _config_tier_model(app: Any, adef: AgentDefinition) -> str | None:
    """Resolve the role's tier → model id from the live config (docs/reference/subagents.md §3.4).

    Defensive duck-typing keeps ``core`` ⊥ ``app``: we never import the config
    type, only call ``app.config.model_for_tier(tier)`` if present. Any failure
    (missing attr / unexpected shape) degrades to None → fall back to the parent
    model, so a stale or minimal config can never abort a spawn (fail-safe)."""
    try:
        config = getattr(app, "config", None)
        resolver = getattr(config, "model_for_tier", None)
        if callable(resolver):
            resolved = resolver(adef.tier)
            return str(resolved) if resolved else None
    except Exception:  # noqa: BLE001 — tier lookup must never break dispatch
        return None
    return None


def _model_serviceable(app: Any, model_id: str) -> bool:
    """Duck-typed bridge to ``AppContext.model_serviceable`` (keeps core ⊥ infra).

    Absent hook or any failure → True: treat the model as serviceable so this
    guard can only ever *remove* a footgun, never block an otherwise-valid spawn."""
    check = getattr(app, "model_serviceable", None)
    if not callable(check):
        return True
    try:
        return bool(check(model_id))
    except Exception:  # noqa: BLE001 — never block a spawn on this heuristic
        return True


def _degraded_summary(adef: AgentDefinition, reason: str, partial: str) -> str:
    """Hard-floor non-empty progress summary on overrun (§8.2, S10)."""
    head = (
        f"[DEGRADED:{reason}] {adef.name} stopped at safety valve ({reason}). "
        f"Work is NOT lost — continue from here."
    )
    body = partial.strip() or "(no textual output was produced before the valve fired)"
    return f"{head}\nProgress so far:\n{body}"


class SubAgentRunner:
    """Sub-agent orchestrator (core pure logic, only Ports — P3)."""

    def __init__(
        self,
        *,
        engine_factory: EngineFactory,
        isolator: Isolator,
        resource_lock: Any,
        job_store: Any,
        events: EventSink,
        spawn: SpawnCoro | None = None,
    ) -> None:
        self._engine_factory = engine_factory
        self._isolator = isolator
        self._lock = resource_lock
        self._jobs = job_store
        self._events = events
        self._spawn = spawn

    # ── public ────────────────────────────────────────────────────────────────
    async def spawn(self, req: SpawnRequest, ctx: "ToolContext") -> SpawnResult:
        """Unified path — fg/bg differ only in whether the coro is awaited now (§6)."""
        if not req.subagent_type:
            # Defensive (S1): schema is required, but never route by string-guessing.
            raise ValueError("spawn requires an explicit subagent_type")
        adef = get_definition(req.subagent_type)
        if not is_known_definition(req.subagent_type):
            # Unknown type → generalist fallback, but surface it (docs/reference/subagents.md §4.2)
            # so the fallback is observable rather than silent.
            await self._emit_debug(
                ctx,
                f"unknown subagent_type {req.subagent_type!r}; "
                f"falling back to '{adef.name}'",
            )
        # Plan-mode capability downgrade (docs/guides/using-cli-and-tui.md §3.10.2): when the
        # parent session is in plan mode, the dispatcher hard-blocks write-tier
        # tools — but the `agent` tool itself is READ_ONLY, so a write-tier
        # sub-agent (builder/generalist/…) would otherwise be a bypass. Clamp the
        # spawned agent's ceiling to READ_ONLY so its write tools and bash are
        # dropped by `filter_registry`. Read-only roles (scout/architect/
        # researcher) are unaffected and stay usable for in-plan research.
        # System roles (checkpoint-writer) are exempt — they are not user-driven.
        plan_downgraded = (
            _parent_plan_mode_active(ctx)
            and adef.name not in _PLAN_MODE_DOWNGRADE_EXEMPT
            and adef.max_tier > CapabilityTier.READ_ONLY
        )
        if plan_downgraded:
            adef = replace(
                adef, max_tier=CapabilityTier.READ_ONLY, bash_check_mode=False
            )
        # Resolution order: per-call override > role explicit model (rare) >
        # config tier→model mapping > parent/main model (§3.4). The tier mapping
        # is folded into the middle term so _resolve_model stays a pure 3-arg fn.
        tier_model = _config_tier_model(ctx.app, adef)
        # Guard the config tier→model term (docs/reference/subagents.md §3.4 footgun): a tier
        # pinned to a provider whose key is missing would 401 the whole spawn,
        # even though the parent's main model is authenticating fine. When the
        # tier model *would* be the effective choice (no per-call / role-explicit
        # override) but has no usable credential, drop it so resolution falls
        # back to the main model — and surface the swap (observable, not silent).
        if (
            tier_model
            and not req.model
            and not adef.model
            and tier_model != ctx.app.model
            and not _model_serviceable(ctx.app, tier_model)
        ):
            await self._emit_debug(
                ctx,
                f"tier model {tier_model!r} for '{adef.name}' has no usable "
                f"credential — falling back to main model {ctx.app.model!r}",
            )
            tier_model = None
        model = _resolve_model(req.model, adef.model or tier_model, ctx.app.model)
        # Stable per-dispatch id: front-ends key the live Agents panel on it so
        # two concurrent same-type agents (e.g. two scouts) don't collide. For
        # background dispatch the job id supersedes it (set in _dispatch_background).
        dispatch_id = f"sa-{uuid.uuid4().hex[:8]}"
        coro = self._run_one(
            adef, req, model, ctx, dispatch_id, plan_downgraded=plan_downgraded
        )
        if req.run_in_background:
            return await self._dispatch_background(adef, req, coro, ctx)
        return await coro

    async def write_checkpoint(self, ctx: "ToolContext", *, task: str, context: str = "") -> SpawnResult:
        """Checkpoint-writer dispatch hook (§4.2a). Forwards to the same path."""
        req = SpawnRequest(subagent_type="checkpoint-writer", task=task, context=context)
        return await self.spawn(req, ctx)

    # ── single execution body (the only path) ─────────────────────────────────
    async def _run_one(
        self,
        adef: AgentDefinition,
        req: SpawnRequest,
        model: str,
        ctx: "ToolContext",
        dispatch_id: str,
        *,
        plan_downgraded: bool = False,
    ) -> SpawnResult:
        """Lifecycle wrapper: emit running → done/failed around one execution.

        The front-end Agents panel keys on ``dispatch_id`` (stable per spawn) and
        transitions on ``status``. ``failed`` covers any exception escaping the
        execution body (lock/isolation/engine), guaranteeing the live panel never
        leaves a phantom "running" row behind on error (the bg path also records
        the Job terminal state separately, §6.1)."""
        started = time.monotonic()
        # The short brief shown on the card head (MiMo "{Type} Task — {description}").
        # Prefer the explicit ``description`` (a 3-5 word summary the caller passes,
        # MiMo actor parity) so the card is a real abbreviation, not the truncated
        # instruction; fall back to the first non-empty task line for legacy callers
        # (workflow phases / older dispatches) that don't set one.
        brief = (req.description or "").strip()[:80] or next(
            (ln.strip() for ln in req.task.splitlines() if ln.strip()), ""
        )[:80] or req.task[:80]
        # "running" (not "dispatched"): the panel shows a live, in-progress state
        # from turn 0; child-turn heartbeats refine the turn/elapsed fields.
        await self._emit(
            adef, "running", brief, None, job_id=dispatch_id, status="running",
            task=req.task, description=brief,
        )
        try:
            result = await self._execute(
                adef, req, model, ctx, dispatch_id, plan_downgraded=plan_downgraded
            )
        except BaseException as exc:  # noqa: BLE001 — surface to the panel, then re-raise
            # A JobStop / shutdown cancel reads as "cancelled"; anything else is a
            # genuine failure. Both clear the live row; the colour/glyph differ.
            cancelled = isinstance(exc, asyncio.CancelledError)
            await self._emit(
                adef,
                "cancelled" if cancelled else "failed",
                "cancelled" if cancelled else repr(exc)[:80],
                None,
                job_id=dispatch_id,
                status="cancelled" if cancelled else "failed",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
            raise
        await self._emit(
            adef, "completed", "", result.trace_id,
            job_id=dispatch_id, status="done",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            tool_count=result.tool_count,
        )
        return result

    async def _execute(
        self,
        adef: AgentDefinition,
        req: SpawnRequest,
        model: str,
        ctx: "ToolContext",
        dispatch_id: str,
        *,
        plan_downgraded: bool = False,
    ) -> SpawnResult:
        prof = adef.isolation
        isolate = (prof.force_isolation if prof else False) or req.isolation == "worktree"

        tainted = False
        note: str | None = None
        commit: str | None = None
        worktree_branch: str | None = None
        worktree_path: str | None = None
        pgids: tuple[int, ...] = ()
        base_cwd = Path(req.target_cwd).expanduser() if req.target_cwd else ctx.cwd

        # 2. acquire resource lock FIRST (before isolation — avoid holding the box).
        async with self._lock.acquire(req.exclusive_resources, ctx.signal):
            # 3. establish isolation — fail-closed (no except-degrade branch; §3.2/S6).
            async with AsyncExitStack() as stack:
                iso: IsoHandle | None = None
                if isolate:
                    iso = await stack.enter_async_context(
                        self._isolator.scope(IsoSpec(lifecycle="turn"), base_cwd)
                    )
                    worktree_branch = iso.branch
                    worktree_path = str(iso.cwd)
                work_cwd = iso.cwd if iso is not None else base_cwd

                # 4. tool filtering (general bans + capability ceiling + intersect).
                tools = _filter_tools(ctx.app.tools, adef)

                # 5. construct the child engine (fresh history / tracker / session).
                #    dispatch_id + subagent_type let the production factory wrap the
                #    child's event sink so per-turn heartbeats key onto the same
                #    panel row as the runner's lifecycle events (display §13).
                engine = self._engine_factory(
                    system_prompt=_build_prompt(
                        adef,
                        req,
                        iso,
                        plan_downgraded=plan_downgraded,
                    ),
                    tools=tools,
                    model=model,
                    cwd=work_cwd,
                    isolation_root=iso.cwd if iso is not None else None,
                    isolation_source_root=(
                        iso.source_root if iso is not None else None
                    ),
                    max_turns=adef.max_turns,
                    bash_check_mode=adef.bash_check_mode,
                    process_lifecycle=(prof.process_lifecycle if prof else "inherit"),
                    dispatch_id=dispatch_id,
                    subagent_type=adef.name,
                )

                # 6. execute + safety valves — always returns a non-empty summary.
                #    Bound process reclamation (§7.4/S9) must run on EVERY exit
                #    path (return / timeout / engine exception / cancel), so it
                #    lives in a finally and reads pgids from whichever channel is
                #    populated — the RunOutcome on success, or the engine's
                #    registered groups when run() never returned.
                try:
                    run = await _run_with_safety_valves(engine, adef, ctx.signal)
                    pgids = run.pgids or _engine_pgids(engine)

                    # 7. audit BEFORE finalize (autocommit would hide the diff; §7.3/S8).
                    #    A tainted audit must NOT autocommit polluted tracked
                    #    changes into a mergeable branch — discard them instead.
                    if iso is not None:
                        tainted, note = await self._isolator.audit(
                            iso, prof.post_audit if prof else None
                        )
                        action = (
                            FinalizeAction.DISCARD if tainted else FinalizeAction.AUTOCOMMIT
                        )
                        iso_result = await self._isolator.finalize(iso, action)
                        commit = iso_result.commit
                finally:
                    # 8. bound process reclamation (§7.4/S9), in finally order.
                    if prof is not None and prof.process_lifecycle == "bound":
                        leaked = pgids or _engine_pgids(engine)
                        if leaked:
                            reap_process_group(leaked)

        return _build_result(
            adef,
            run,
            tainted,
            note,
            commit,
            worktree_branch,
            worktree_path,
            dispatch_id,
        )

    # ── background dispatch (same coro, wrapped in a job) ──────────────────────
    async def _dispatch_background(
        self,
        adef: AgentDefinition,
        req: SpawnRequest,
        coro: Awaitable[SpawnResult],
        ctx: "ToolContext",
    ) -> SpawnResult:
        parent_trace = getattr(getattr(ctx.app, "trace", None), "trace_id", None)
        job_id = await self._jobs.create(
            subject=f"{req.subagent_type}: {req.task[:60]}",
            kind="subagent",
            parent_trace=parent_trace,
        )

        async def _runner_with_job() -> None:
            try:
                result = await coro
                await self._jobs.complete(job_id, result.summary, result)
            except asyncio.CancelledError:
                # JobStop / shutdown: record a terminal CANCELLED so fan-in never
                # hangs, then propagate so the task truly stops (P5).
                fail = getattr(self._jobs, "fail", None)
                if callable(fail):
                    await self._jobs.fail(job_id, "cancelled")
                raise
            except BaseException as exc:  # noqa: BLE001 — terminal state or fan-in hangs
                await self._jobs.fail(job_id, repr(exc))

        task = self._launch(_runner_with_job(), ctx)
        # Bind the running task so JobStop can cancel it (no-op if the store or
        # spawn hook returned nothing trackable).
        register = getattr(self._jobs, "register_task", None)
        if callable(register) and task is not None:
            register(job_id, task)
        return SpawnResult(
            subagent_type=req.subagent_type,
            trace_id=job_id,
            summary=(
                f"Background agent dispatched (job={job_id}). "
                f"Call JobOutput() to collect when it finishes."
            ),
        )

    def _launch(self, coro: Awaitable[None], ctx: "ToolContext") -> "asyncio.Task[Any] | None":
        # Prefer the AppContext.spawn hook (the orchestrator's background task set);
        # then the runner-injected spawn; finally a detached task on the loop.
        # Returns the asyncio.Task when the hook exposes one (so JobStop can
        # cancel it); spawn hooks that return None simply aren't cancellable.
        app_spawn = getattr(ctx.app, "spawn", None)
        if callable(app_spawn):
            return self._as_task(app_spawn(coro))
        if self._spawn is not None:
            return self._as_task(self._spawn(coro))
        return asyncio.ensure_future(coro)

    @staticmethod
    def _as_task(handle: Any) -> "asyncio.Task[Any] | None":
        return handle if isinstance(handle, asyncio.Task) else None

    async def _emit_debug(self, ctx: "ToolContext", detail: str) -> None:
        """Surface a non-fatal diagnostic via SUBAGENT_ACTIVITY (no new mechanism)."""
        events = getattr(ctx, "events", None) or self._events
        await events.emit(
            make_event(
                SubagentActivityPayload(
                    job_id="",
                    subagent_type="(resolver)",
                    activity="fallback",
                    detail=detail,
                )
            )
        )

    async def _emit(
        self,
        adef: AgentDefinition,
        activity: str,
        detail: str,
        child_session: str | None,
        *,
        job_id: str = "",
        status: str = "",
        elapsed_ms: float = 0.0,
        tool_count: int = 0,
        task: str = "",
        description: str = "",
    ) -> None:
        await self._events.emit(
            make_event(
                SubagentActivityPayload(
                    job_id=job_id or child_session or "",
                    subagent_type=adef.name,
                    activity=activity,
                    detail=detail,
                    child_session_id=child_session,
                    status=status,
                    elapsed_ms=elapsed_ms,
                    tool_count=tool_count,
                    task=task,
                    description=description,
                )
            )
        )


async def _run_with_safety_valves(
    engine: SubEngine, adef: AgentDefinition, signal: AbortSignal
) -> RunOutcome:
    """Wall-clock + max_turns valves; both degrade to a non-empty summary (§8.2/S10)."""
    timeout = adef.wall_clock or _DEFAULT_WALL_CLOCK
    try:
        run = await asyncio.wait_for(engine.run(), timeout=timeout)
    except asyncio.TimeoutError:
        return RunOutcome(
            summary=_degraded_summary(adef, "wall_clock", ""),
            degraded="wall_clock",
        )
    if run.degraded:
        # max_turns (or any engine-reported overrun): guarantee non-empty summary.
        if not run.summary.strip() or TAINT_PREFIX in run.summary:
            run.summary = _degraded_summary(adef, run.degraded, run.summary)
        elif not run.summary.startswith("[DEGRADED"):
            run.summary = _degraded_summary(adef, run.degraded, run.summary)
    return run


def _engine_pgids(engine: SubEngine) -> tuple[int, ...]:
    """Bound process groups a child engine registered (§7.4).

    The success path carries pgids in ``RunOutcome``; on timeout / exception the
    engine never returns one, so the runner falls back to this side channel so
    reaping still happens. Engines that do not track groups simply expose none.
    """
    pgids = getattr(engine, "pgids", ())
    return tuple(pgids)


def _filter_tools(registry: object, adef: AgentDefinition) -> Any:
    """Apply the capability filter when a ToolRegistry is present, else None.

    Fail-safe (docs/reference/subagents.md §5.2): if filtering raises (e.g. a corrupt registry),
    degrade to the most-restricted empty tool set rather than crashing the spawn.
    """
    from autoverse.core.tools.registry import ToolRegistry

    if not isinstance(registry, ToolRegistry):
        return None
    try:
        return filter_registry(registry, adef)
    except Exception:  # noqa: BLE001 — corrupt registry must not abort the spawn
        return ToolRegistry.build(include=set())


def _build_prompt(
    adef: AgentDefinition,
    req: SpawnRequest,
    iso: IsoHandle | None,
    *,
    plan_downgraded: bool = False,
) -> str:
    """Assemble the child system prompt: role prompt + task/context + scope note."""
    parts = [adef.system_prompt.rstrip()]
    # reviewer review_mode selects the compliance/quality lens + output contract.
    if adef.name == "reviewer" and req.review_mode:
        from autoverse.core.subagent.prompts import REVIEWER_MODE_VARIANTS

        variant = REVIEWER_MODE_VARIANTS.get(req.review_mode)
        if variant:
            parts.append(variant)
    if plan_downgraded:
        # The parent session is in plan mode; this agent's capability ceiling was
        # clamped to READ_ONLY so write/exec tools are not in its toolset. Tell it
        # so it researches instead of fruitlessly attempting blocked edits.
        parts.append(
            "\n## Plan Mode (parent)\n"
            "The parent session is in planning mode, so you have been restricted "
            "to READ-ONLY tools. Do NOT attempt to write files or run commands "
            "(those tools are not available to you). Investigate, read, and report "
            "findings or a proposed approach back to the caller."
        )
    if iso is not None:
        source_note = (
            f" Absolute paths under {iso.source_root} are remapped into this worktree."
            if iso.source_root
            else ""
        )
        parts.append(
            f"\n## Isolation\nYou are running inside an isolated git worktree at "
            f"{iso.cwd} (branch {iso.branch}).{source_note} Do not cd or write "
            "outside the worktree."
        )
    elif req.target_cwd:
        parts.append(f"\n## Working Directory\nRun this task from {req.target_cwd}.")
    if req.task:
        parts.append(f"\n## Task\n{req.task[:_MAX_TASK_CHARS]}")
    if req.context:
        parts.append(f"\n## Context\n{req.context}")
    return "\n".join(parts)


def _build_result(
    adef: AgentDefinition,
    run: RunOutcome,
    tainted: bool,
    note: str | None,
    commit: str | None,
    worktree_branch: str | None,
    worktree_path: str | None,
    trace_id: str | None,
) -> SpawnResult:
    summary = run.summary
    verdict = run.verdict
    if tainted:
        verdict = None  # tainted voids the verdict for the orchestrator (§7.3)
        if note and note not in summary:
            summary = f"{note}\n{summary}"
    return SpawnResult(
        subagent_type=adef.name,
        summary=summary or f"{adef.name} produced no output.",
        verdict=verdict,
        tainted=tainted,
        degraded=run.degraded,
        worktree_commit=commit,
        worktree_branch=worktree_branch,
        worktree_path=worktree_path,
        trace_id=trace_id,
        tool_count=run.tool_count,
    )


__all__ = [
    "SpawnRequest",
    "SpawnResult",
    "RunOutcome",
    "SubEngine",
    "EngineFactory",
    "SubAgentRunner",
]
