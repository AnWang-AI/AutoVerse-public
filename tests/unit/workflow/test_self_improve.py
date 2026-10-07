"""Unit — /dream + /distill self-improvement (KDR-11: zero new mechanism).

These prove the loop is pure assembly over injected trace / memory / registry
interfaces: it only *reads* trace, *writes* memory through the sink, and
*registers* assets through the registry — inventing no storage of its own.
"""

from __future__ import annotations

from autoverse.app.workflow import (
    Advance,
    Orchestration,
    Termination,
    Trigger,
    distill,
    distill_workflow,
    dream,
    dream_workflow,
)


class FakeTrace:
    """Read-only trace source: a list of (tool_sequence) per turn."""

    def __init__(self, sequences: list[list[str]]) -> None:
        self._sequences = sequences
        self.writes = 0  # must remain 0: trace is the source of truth (read-only)

    def tool_sequences(self, session_id: str | None = None) -> list[list[str]]:
        return [list(s) for s in self._sequences]


class FakeMemory:
    def __init__(self) -> None:
        self.facts: list[str] = []

    def append_fact(self, fact: str) -> None:
        self.facts.append(fact)


class FakeAssets:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    def register(self, kind: str, name: str) -> None:
        self.registered.append((kind, name))


def test_dream_workflow_four_dimensions() -> None:
    d = dream_workflow()
    assert d.name == "dream"
    assert d.orchestration is Orchestration.MULTI_AGENT_DAG
    assert d.advance is Advance.ARTIFACT_CHECK
    assert Termination.FIXED_PHASES_DONE in d.termination


def test_distill_workflow_four_dimensions() -> None:
    d = distill_workflow()
    assert d.name == "distill"
    assert d.advance is Advance.SCORE_THRESHOLD
    assert Termination.FIXED_PHASES_DONE in d.termination


def test_dream_consolidates_repeated_facts_only_and_never_writes_trace() -> None:
    trace = FakeTrace([["grep", "read", "read"], ["grep", "read", "read"], ["bash"]])
    mem = FakeMemory()
    res = dream(trace, mem, session_id="s", min_occurrences=2)
    # The repeated sequence (seen twice) is consolidated; the one-off is not.
    assert res.facts_added == 1
    assert any("grep" in f for f in mem.facts)
    assert trace.writes == 0           # trace untouched (P1 source of truth)


def test_distill_packages_only_high_confidence_candidates() -> None:
    # "grep,read" repeats 3× (>= threshold → packaged); "ls,cat" repeats 2× (a
    # genuine sub-threshold candidate → listed, not registered).
    trace = FakeTrace(
        [["grep", "read"], ["grep", "read"], ["grep", "read"], ["ls", "cat"], ["ls", "cat"]]
    )
    assets = FakeAssets()
    res = distill(trace, assets, threshold=3)
    assert res.packaged == 1
    assert assets.registered and assets.registered[0][0] in {"skill", "command", "subagent"}
    # The repeated-but-below-threshold one is surfaced as a candidate.
    assert res.candidates >= 1


def test_distill_conservative_default_only_lists_candidates_below_threshold() -> None:
    trace = FakeTrace([["grep", "read"], ["bash"]])
    assets = FakeAssets()
    res = distill(trace, assets, threshold=5)
    assert res.packaged == 0           # nothing meets threshold → fail-closed
    assert assets.registered == []
