"""Unit — Pipeline phase ordering, retry loop, early stop, coordinate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from autoverse.app.workflow import (
    PhaseResult,
    PhaseSpec,
    Pipeline,
)
from autoverse.app.workflow.pipelines import SubAgentPhaseRunner
from autoverse.core.loop.types import StopCause


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


async def test_phases_run_in_order_and_thread_prev_output() -> None:
    seen: list[tuple[str, str]] = []

    class Runner:
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            seen.append((spec.name, prev))
            return PhaseResult(spec.name, f"{spec.name}-out", True)

    sink = _Sink()
    pipe = Pipeline(runner=Runner(), events=sink)
    out = await pipe.run(
        [PhaseSpec("a", "architect", "x"), PhaseSpec("b", "builder", "use {prev}")]
    )
    assert out.cause is StopCause.END_TURN
    assert [n for n, _ in seen] == ["a", "b"]
    assert seen[1][1] == "a-out"          # prev output threaded forward
    # SubAgentRunner owns phase lifecycle. Pipeline must not create a second,
    # unterminated `phase:*` SUBAGENT_ACTIVITY card for the same work.
    assert sink.events == []


async def test_early_stop_when_phase_signals_nothing_to_do() -> None:
    class Runner:
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            # verification phase finds nothing to commit → stop, never run builder
            return PhaseResult(spec.name, "empty diff", True, halt=True)

    ran: list[str] = []

    class Tracking(Runner):
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            ran.append(spec.name)
            return await super().run_phase(spec, prev)

    pipe = Pipeline(runner=Tracking(), events=_Sink())
    out = await pipe.run(
        [PhaseSpec("verify", "validator", "x"), PhaseSpec("builder", "builder", "y")]
    )
    assert ran == ["verify"]               # builder never dispatched
    assert out.cause is StopCause.END_TURN


async def test_phase_runner_pins_subagent_to_exact_workflow_root(tmp_path: Path) -> None:
    captured: list[Any] = []

    class Runner:
        async def spawn(self, req: Any, ctx: Any) -> Any:
            captured.append((req, ctx))
            return SimpleNamespace(summary="done", verdict=None, tainted=False)

    app = SimpleNamespace(cwd=tmp_path / ".." / tmp_path.name, events=_Sink())
    runner = SubAgentPhaseRunner(
        runner=Runner(),  # type: ignore[arg-type]
        app=app,  # type: ignore[arg-type]
        permissions=object(),  # type: ignore[arg-type]
    )
    await runner.run_phase(
        PhaseSpec("write", "builder", "Write {cwd}/AGENTS.md from:\n{prev}"),
        "scan output",
    )

    req, ctx = captured[0]
    root = tmp_path.resolve()
    assert req.target_cwd == str(root)
    assert ctx.cwd == root
    assert f"Workflow root: {root}" in req.task
    assert f"Write {root}/AGENTS.md" in req.task
    assert "Do not switch into a nested repository" in req.task


async def test_coordinate_scoring_retry_loop_breaks_on_threshold() -> None:
    scores = iter([3, 5, 8])

    class Runner:
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            if spec.name == "evaluator":
                s = next(scores)
                return PhaseResult(spec.name, f"score={s}", s >= 7, score=s)
            return PhaseResult(spec.name, f"{spec.name}-out", True)

    pipe = Pipeline(runner=Runner(), events=_Sink())
    out = await pipe.coordinate("do X", threshold=7, max_retries=3)
    assert out.cause is StopCause.END_TURN
    assert out.results[-1].score == 8
