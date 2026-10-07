"""/dream + /distill — the self-improvement closure (docs/reference/workflows.md §11.5, KDR-11).

These two workflows are the highest expression of the four-dimension derivation:
they model "the system learning from its own run history" as just another pair of
workflows. The decisive property is **zero new mechanism** (KDR-11): they invent
no storage and add no engine path — they are *pure assembly* over three things
that already exist, injected as narrow interfaces:

* a **trace source** (read-only — the trace DB is the single source of truth, P1);
* a **memory sink** (controlled write to project memory);
* an **asset registry** (skill / sub-agent / command registration, P8).

``/dream`` (memory consolidation): promote facts that recur in the trace into
project memory. It **only reads** the trace — never writes it (trace is an audit
side-channel; mutable = untrustworthy).

``/distill`` (workflow distillation): detect repeated manual tool sequences and
package high-confidence ones into reusable assets. It is **fail-closed** below the
confidence threshold (auto-changing one's own behaviour is high hidden-cost):
sub-threshold candidates are listed, never auto-registered.
"""

from __future__ import annotations

import inspect
from collections import Counter
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from autoverse.app.workflow.pipeline import PhaseSpec
from autoverse.app.workflow.registry import (
    Advance,
    Orchestration,
    Termination,
    Trigger,
    WorkflowDefinition,
)


# ── injected interfaces (no concrete storage owned here, KDR-11) ──────────────
@runtime_checkable
class TraceSource(Protocol):
    """Read-only view over the trace: ordered tool sequences per turn."""

    def tool_sequences(self, session_id: str | None = None) -> list[list[str]]: ...


@runtime_checkable
class MemorySink(Protocol):
    """Controlled-write sink for consolidated facts (e.g. MEMORY.md)."""

    def append_fact(self, fact: str) -> None: ...


@runtime_checkable
class AssetRegistry(Protocol):
    """Register a distilled asset; ``kind`` ∈ {skill, command, subagent}."""

    def register(self, kind: str, name: str) -> None: ...


@dataclass(frozen=True)
class DreamResult:
    facts_added: int
    pruned: int = 0


@dataclass(frozen=True)
class DistillResult:
    packaged: int
    candidates: int


def _sequence_key(seq: list[str]) -> str:
    return ",".join(seq)


def dream(
    trace: TraceSource,
    memory: MemorySink,
    *,
    session_id: str | None = None,
    min_occurrences: int = 2,
) -> DreamResult:
    """Consolidate tool sequences that recur ``>= min_occurrences`` into memory.

    Pure assembly: read trace → count recurrences → write the validated ones to
    the memory sink. The trace is never mutated (P1).
    """
    counts = Counter(_sequence_key(seq) for seq in trace.tool_sequences(session_id) if seq)
    added = 0
    for key, n in counts.items():
        if n >= min_occurrences:
            memory.append_fact(f"Recurring workflow ({n}×): {key}")
            added += 1
    return DreamResult(facts_added=added)


def distill(
    trace: TraceSource,
    assets: AssetRegistry,
    *,
    threshold: int = 3,
    session_id: str | None = None,
) -> DistillResult:
    """Package repeated tool sequences whose count ``>= threshold`` into assets.

    Sub-threshold sequences are counted as ``candidates`` but never registered —
    fail-closed (§11.5): auto-changing one's own configuration is high hidden-cost.
    """
    counts = Counter(_sequence_key(seq) for seq in trace.tool_sequences(session_id) if seq)
    packaged = 0
    candidates = 0
    for key, n in counts.items():
        if n >= threshold:
            assets.register("skill", f"distilled-{key.replace(',', '-')}")
            packaged += 1
        elif n >= 2:
            candidates += 1
    return DistillResult(packaged=packaged, candidates=candidates)


# ── four-dimension definitions (registerable via WorkflowRegistry) ────────────
def dream_workflow() -> WorkflowDefinition:
    """``/dream`` filled into the four-dimension table (§11.5)."""
    return WorkflowDefinition(
        name="dream",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.FIXED_PHASES_DONE,),
        summary="Consolidate recurring trace facts into project memory (read-only trace).",
        runner=dream,
    )


def distill_workflow() -> WorkflowDefinition:
    """``/distill`` filled into the four-dimension table (§11.5)."""
    return WorkflowDefinition(
        name="distill",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.SCORE_THRESHOLD,
        termination=(Termination.FIXED_PHASES_DONE,),
        summary="Package high-confidence repeated workflows into reusable assets.",
        runner=distill,
    )


def fix_workflow() -> WorkflowDefinition:
    """``/fix`` derived by filling the four-dimension table (§11.2).

    A bug-fix closure: reproduce → locate → fix → verify, looping until a
    machine-verifiable assertion holds (the reproduction test goes green AND no
    new failures). This is strictly higher-determinism than a bare ``/goal``'s
    ``EXTERNAL_JUDGE`` (methodology first law) — hence MACHINE_VERIFIABLE.
    Registering it requires no engine edit (P8 / W7).
    """
    phases = (
        PhaseSpec("reproduce", "builder", "Write a failing test that reproduces: {prev}"),
        PhaseSpec("locate", "scout", "Locate the root cause given {prev}", readonly=True),
        PhaseSpec("fix", "builder", "Apply the minimal fix for {prev}"),
        PhaseSpec("verify", "validator", "Run the test suite; confirm green and no new failures"),
    )
    return WorkflowDefinition(
        name="fix",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.DYNAMIC_LOOP,
        advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.MACHINE_VERIFIABLE,),
        summary="Reproduce → locate → fix → verify until the repro test is green (no new failures).",
        phases=phases,
    )


# ── adapters: bridge app infrastructure → injected interfaces (KDR-11) ────────
# Lightweight duck-typed wrappers that bridge concrete app-layer objects
# (TraceCollector, SkillRegistry, a file path) into the narrow Protocol
# interfaces /dream and /distill declare.  No new storage, no new engine path
# (KDR-11): the runners stay pure assembly over injected interfaces.


class _TraceCollectorAdapter:
    """Adapt :class:`TraceCollector` → :class:`TraceSource`.

    Reads ``TraceModel.turns[*].tools[*].name`` to reconstruct per-turn tool
    sequences. The trace is never mutated (P1).
    """

    def __init__(self, collector: object) -> None:
        self._collector = collector

    def tool_sequences(self, session_id: str | None = None) -> list[list[str]]:
        model = getattr(self._collector, "model", None)
        if model is None:
            return []
        turns = getattr(model, "turns", [])
        return [
            [getattr(t, "name", "") for t in getattr(turn, "tools", [])]
            for turn in turns
        ]


class _FileMemorySink:
    """Append consolidated facts to project memory (``MEMORY.md``)."""

    def __init__(self, cwd: object) -> None:
        from pathlib import Path

        self._path = Path(str(cwd)) / "MEMORY.md"

    def append_fact(self, fact: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(f"\n- {fact}\n")


class _SkillAssetRegistry:
    """Wrap :class:`SkillRegistry` as an :class:`AssetRegistry` for /distill."""

    def __init__(self, registry: object) -> None:
        self._registry = registry

    def register(self, kind: str, name: str) -> None:
        if kind != "skill":
            return
        register_fn = getattr(self._registry, "register", None)
        if not callable(register_fn):
            return
        from autoverse.infra.skills.registry import Skill

        register_fn(
            Skill(
                name=name,
                description=f"Distilled skill: {name}",
                source="user",
            ),
            overwrite=True,
        )


def _find_skill_registry(app: object) -> object | None:
    """Locate the SkillRegistry among the app's extension sources."""
    for src in getattr(app, "extension_sources", ()):
        if hasattr(src, "register") and hasattr(src, "all_skills"):
            return src  # type: ignore[no-any-return]
    return None


def invoke_runner(defn: WorkflowDefinition, app: object, session_id: str) -> str:
    """Dispatch a runner-based workflow (e.g. /dream, /distill).

    KDR-11: zero new mechanism — the runner is the same plain callable registered
    in the four-dimension table; we only inject the narrow interfaces it already
    declares. Degrades gracefully (never raises) when a dependency is missing,
    matching the ``run_workflow`` contract.
    """
    if defn.runner is None:
        return f"workflow {defn.name!r} has no runner"
    sig = inspect.signature(defn.runner)
    kwargs: dict[str, object] = {}
    for pname in sig.parameters:
        if pname == "trace":
            trace = getattr(app, "trace", None)
            if trace is None:
                return f"workflow {defn.name!r} unavailable: no trace collector wired"
            kwargs[pname] = _TraceCollectorAdapter(trace)
        elif pname == "memory":
            kwargs[pname] = _FileMemorySink(getattr(app, "cwd"))
        elif pname == "assets":
            registry = _find_skill_registry(app)
            if registry is None:
                return f"workflow {defn.name!r} unavailable: no skill registry wired"
            kwargs[pname] = _SkillAssetRegistry(registry)
        elif pname == "session_id":
            kwargs[pname] = session_id

    try:
        result = defn.runner(**kwargs)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully per contract
        return f"[{defn.name}] failed: {exc}"

    if isinstance(result, DreamResult):
        return f"[{defn.name}] consolidated {result.facts_added} fact(s) into memory"
    if isinstance(result, DistillResult):
        return (
            f"[{defn.name}] packaged {result.packaged} asset(s), "
            f"{result.candidates} candidate(s) below threshold"
        )
    return f"[{defn.name}] done"


__all__ = [
    "TraceSource",
    "MemorySink",
    "AssetRegistry",
    "DreamResult",
    "DistillResult",
    "dream",
    "distill",
    "dream_workflow",
    "distill_workflow",
    "fix_workflow",
    "invoke_runner",
]
