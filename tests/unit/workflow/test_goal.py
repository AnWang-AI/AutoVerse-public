"""Unit — GoalStore / GoalEvaluator circuit breaker (docs/reference/workflows.md §6.4)."""

from __future__ import annotations

from typing import Any

from autoverse.app.workflow import (
    CLEAR_ALIASES,
    GoalEvaluator,
    GoalStore,
    JudgeResult,
    MAX_CONSECUTIVE_JUDGE_ERRORS,
)


def test_goal_store_lifecycle() -> None:
    store = GoalStore()
    assert store.get("s") is None
    store.set("s", "done when green")
    e = store.get("s")
    assert e is not None and e.condition == "done when green" and not e.achieved
    store.mark_achieved("s", "all green")
    assert store.get("s") is None
    assert store.last_achieved("s") is not None
    assert store.last_achieved("s").achieved
    store.clear("s")
    assert store.get("s") is None


def test_at_most_one_active_goal_per_session() -> None:
    store = GoalStore()
    store.set("s", "first")
    store.set("s", "second")     # replaces, not stacks
    assert store.get("s").condition == "second"


def test_restore_replaces_existing_session_state() -> None:
    store = GoalStore()
    store.set("s", "discarded goal")
    store.mark_achieved("s", "discarded result")

    store.restore(
        "s",
        {
            "active": {
                "condition": "retained goal",
                "turns_evaluated": 2,
                "achieved": False,
            },
            "last_achieved": None,
        },
    )

    active = store.get("s")
    assert active is not None
    assert active.condition == "retained goal"
    assert active.turns_evaluated == 2
    assert store.last_achieved("s") is None

    store.restore("s", None)
    assert store.get("s") is None
    assert store.last_achieved("s") is None


def test_clear_aliases_cover_common_stop_words() -> None:
    assert {"clear", "stop", "off", "reset", "none", "cancel"} <= CLEAR_ALIASES


async def test_goal_state_persists_and_restores(tmp_path) -> None:
    from autoverse.infra.persist.jsonl_store import JSONLStore
    from autoverse.ports import SessionMeta

    store = JSONLStore(tmp_path)
    await store.create("s", SessionMeta(session_id="s"))
    first = GoalStore(store=store)
    first.set("s", "all tests pass")
    first.get("s").turns_evaluated = 2
    await first.persist("s")

    meta = await store.load_meta("s")
    restored = GoalStore(store=store)
    restored.restore("s", meta.extra.get("goal_state"))
    entry = restored.get("s")
    assert entry is not None
    assert entry.condition == "all tests pass"
    assert entry.turns_evaluated == 2

    restored.mark_achieved("s", "green")
    await restored.persist("s")
    cold = GoalStore(store=store)
    meta = await store.load_meta("s")
    cold.restore("s", meta.extra.get("goal_state"))
    assert cold.get("s") is None
    assert cold.last_achieved("s") is not None


async def test_evaluator_wraps_exception_as_judge_error() -> None:
    async def boom(condition: str, transcript: Any) -> JudgeResult:
        raise TimeoutError("judge timed out")

    ev = GoalEvaluator(judge=boom)
    res = await ev.evaluate("cond", [])
    assert res.is_judge_error and not res.ok


async def test_evaluator_passes_through_normal_verdicts() -> None:
    async def ok(condition: str, transcript: Any) -> JudgeResult:
        return JudgeResult(ok=True, reason="")

    res = await GoalEvaluator(judge=ok).evaluate("cond", [])
    assert res.ok and not res.is_judge_error


async def test_llm_judge_counts_usage_and_rejects_malformed_json() -> None:
    from autoverse.app.workflow.judge import make_llm_judge
    from autoverse.types.events import (
        TextDeltaPayload,
        UsagePayload,
        make_event,
    )
    from autoverse.types.messages import Usage
    from tests._doubles import ScriptedProvider

    good = ScriptedProvider([[
        make_event(TextDeltaPayload('{"done": false, "reason": "tests failing"}')),
        make_event(UsagePayload(Usage(input_tokens=12, output_tokens=3), "echo")),
    ]])
    result = await make_llm_judge(good, model="echo", max_retries=0)("green", [])
    assert not result.ok
    assert not result.is_judge_error
    assert result.tokens_used == 15

    malformed = ScriptedProvider([[
        make_event(TextDeltaPayload("probably done")),
    ]])
    result = await make_llm_judge(
        malformed, model="echo", max_retries=0
    )("green", [])
    assert result.is_judge_error


def test_judge_transcript_includes_tool_evidence() -> None:
    from autoverse.app.workflow.judge import _transcript_text
    from autoverse.types.messages import Message, ToolResultBlock, ToolUseBlock

    transcript = [
        Message(
            role="assistant",
            content=(ToolUseBlock(id="t1", name="bash", input={"command": "pytest"}),),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_use_id="t1",
                    content="42 passed in 1.2s",
                    is_error=False,
                ),
            ),
        ),
    ]
    rendered = _transcript_text(transcript)
    assert "pytest" in rendered
    assert "42 passed" in rendered


def test_breaker_resets_on_normal_records_and_trips_on_faults() -> None:
    store = GoalStore()
    store.set("s", "cond")
    # two faults, then a normal verdict resets the counter
    assert not store.record_judgement("s", is_judge_error=True)
    assert store.get("s").consecutive_judge_errors == 1
    assert not store.record_judgement("s", is_judge_error=True)
    assert store.get("s").consecutive_judge_errors == 2
    assert not store.record_judgement("s", is_judge_error=False)  # normal → reset
    assert store.get("s").consecutive_judge_errors == 0
    # now trip cleanly
    tripped = False
    for _ in range(MAX_CONSECUTIVE_JUDGE_ERRORS):
        tripped = store.record_judgement("s", is_judge_error=True)
    assert tripped is True
    assert store.get("s") is None    # tripped breaker clears the goal
