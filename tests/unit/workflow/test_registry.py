"""Unit — WorkflowRegistry single-source-of-truth + @workflow decorator."""

from __future__ import annotations

import pytest

from autoverse.app.workflow import (
    Advance,
    Orchestration,
    Termination,
    Trigger,
    WorkflowDefinition,
    WorkflowRegistry,
    drain_pending,
    workflow,
)


def _defn(name: str, term: Termination = Termination.MODEL_SELF_CLAIM) -> WorkflowDefinition:
    return WorkflowDefinition(
        name=name,
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.SINGLE_TURN,
        advance=Advance.PROMPT_ONLY,
        termination=(term,),
        summary=f"{name} workflow",
    )


def test_register_get_all() -> None:
    reg = WorkflowRegistry()
    reg.register(_defn("plan"))
    reg.register(_defn("goal"))
    assert reg.get("plan").name == "plan"
    assert {d.name for d in reg.all()} == {"plan", "goal"}
    assert reg.get("missing") is None


def test_duplicate_registration_is_an_error_not_silent_overwrite() -> None:
    reg = WorkflowRegistry()
    reg.register(_defn("plan"))
    with pytest.raises(ValueError):
        reg.register(_defn("plan"))  # P8: name conflict reported, never overwritten


def test_active_workflow_is_per_session() -> None:
    reg = WorkflowRegistry()
    reg.register(_defn("goal"))
    assert reg.active("s1") is None
    reg.set_active("s1", "goal")
    assert reg.active("s1").name == "goal"
    assert reg.active("s2") is None        # isolation across sessions
    reg.clear_active("s1")
    assert reg.active("s1") is None


def test_set_active_unknown_workflow_raises() -> None:
    reg = WorkflowRegistry()
    with pytest.raises(KeyError):
        reg.set_active("s1", "nope")


def test_termination_accepts_single_or_composite() -> None:
    single = WorkflowDefinition(
        name="a", trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.SINGLE_TURN, advance=Advance.PROMPT_ONLY,
        termination=Termination.USER_APPROVAL, summary="x",
    )
    assert single.termination == (Termination.USER_APPROVAL,)
    composite = WorkflowDefinition(
        name="b", trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.FIXED_PHASES, advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.MACHINE_VERIFIABLE, Termination.SCORE_THRESHOLD),
        summary="y",
    )
    assert Termination.MACHINE_VERIFIABLE in composite.termination


def test_workflow_decorator_collects_pending() -> None:
    drain_pending()  # clear any leftovers

    @workflow(
        name="demo", trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG, advance=Advance.ARTIFACT_CHECK,
        termination=Termination.SCORE_THRESHOLD, summary="demo runner",
    )
    def _demo(task: str) -> str:
        return task

    defs = drain_pending()
    assert [d.name for d in defs] == ["demo"]
    assert defs[0].runner is _demo
    # drained → registry can absorb without re-collecting
    assert drain_pending() == []
