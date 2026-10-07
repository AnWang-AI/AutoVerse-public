"""Built-in pipelines: commit / review / pr / init / coordinate (docs/reference/workflows.md §7.4).

Two things live here:

* The **four-dimension definitions** for the built-in multi-agent workflows, so
  they register into the single-source :class:`WorkflowRegistry` (P1) and are
  discoverable by ``/help`` / ``/workflows`` without a parallel hand-list.
* :class:`SubAgentPhaseRunner` — the production :class:`PhaseRunner` that turns a
  :class:`PhaseSpec` into a real sub-agent dispatch via ``SubAgentRunner.spawn``
  (orchestration ≠ isolation, P2: this only *dispatches*; capability tiers and
  isolation are the sub-agent subsystem's job).

The ``commit`` pipeline early-stops on a clean tree (§7.4): the inspect phase
emits ``NOTHING TO COMMIT`` and the runner maps that to ``PhaseResult.halt``.
"""

from __future__ import annotations

import re

from autoverse.app.context import AppContext
from autoverse.app.workflow.pipeline import PhaseResult, PhaseSpec
from autoverse.app.workflow.registry import (
    Advance,
    Orchestration,
    Termination,
    Trigger,
    WorkflowDefinition,
)
from autoverse.app.workflow.self_improve import (
    distill_workflow,
    dream_workflow,
    fix_workflow,
)
from autoverse.core.subagent.runner import SpawnRequest, SubAgentRunner
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.ports import PermissionPort
from autoverse.types.abort import AbortController

_SCORE_RE = re.compile(r"SCORE:\s*(\d+)", re.IGNORECASE)
_HALT_MARKERS = ("NOTHING TO COMMIT", "NO CHANGES TO COMMIT", "CLEAN WORKING TREE")


def _parse_score(text: str) -> int | None:
    m = _SCORE_RE.search(text or "")
    return int(m.group(1)) if m else None


def _is_halt(text: str) -> bool:
    up = (text or "").upper()
    return any(marker in up for marker in _HALT_MARKERS)


class SubAgentPhaseRunner:
    """Dispatch each pipeline phase to a sub-agent (docs/reference/subagents.md §3.3, P2).

    Builds a :class:`SpawnRequest` from the phase spec (``readonly`` phases run
    with no isolation since they only read), constructs a :class:`ToolContext`,
    and maps the :class:`SpawnResult` back onto a :class:`PhaseResult`:

    * ``score`` — parsed from a trailing ``SCORE: N`` when the phase has a
      ``score_threshold`` (reviewer/evaluator phases).
    * ``passed`` — verdict != FAIL and not tainted, overridden by the score gate.
    * ``halt`` — the inspect phase's "nothing to commit" early-stop (§7.4).
    """

    def __init__(
        self,
        *,
        runner: SubAgentRunner,
        app: AppContext,
        permissions: PermissionPort,
    ) -> None:
        self._runner = runner
        self._app = app
        self._permissions = permissions

    async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
        root = self._app.cwd.expanduser().resolve()
        task = (
            spec.task_template
            .replace("{prev}", prev)
            .replace("{cwd}", str(root))
        )
        task = (
            f"Workflow root: {root}\n"
            "Run every command and file operation relative to this exact directory. "
            "Do not switch into a nested repository or choose a different project "
            "root unless the task explicitly asks you to.\n\n"
            f"{task}"
        )
        req = SpawnRequest(
            subagent_type=spec.agent_type,
            task=task,
            isolation="none",  # built-ins act on the real work-tree (commit/init/pr)
            run_in_background=False,
            target_cwd=str(root),
        )
        ctx = ToolContext(
            cwd=root,
            permissions=self._permissions,
            tracker=FileTracker(),
            signal=AbortController().signal,
            events=self._app.events,
            app=self._app,
        )
        result = await self._runner.spawn(req, ctx)
        output = result.summary or ""
        score = _parse_score(output) if spec.score_threshold is not None else None
        passed = result.verdict != "FAIL" and not result.tainted
        if spec.score_threshold is not None:
            passed = score is not None and score >= spec.score_threshold
        return PhaseResult(
            phase=spec.name,
            output=output,
            passed=passed,
            score=score,
            halt=_is_halt(output),
        )


# ── four-dimension definitions (registerable via WorkflowRegistry, §8.1) ──────
def commit_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="commit",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.FIXED_PHASES_DONE,),
        summary="Inspect the tree → write a Conventional Commit message → commit "
        "(early-stops on a clean tree).",
        phases=(
            PhaseSpec(
                "inspect",
                "scout",
                "Inspect the git working tree and summarize the staged/unstaged "
                "changes. If there is nothing to commit, reply with exactly "
                "'NOTHING TO COMMIT'. Context: {prev}",
                readonly=True,
            ),
            PhaseSpec(
                "message",
                "builder",
                "Write a concise Conventional Commits message for these "
                "changes:\n{prev}",
            ),
            PhaseSpec(
                "commit",
                "builder",
                "Stage all changes and create the git commit with this "
                "message:\n{prev}",
            ),
        ),
    )


def review_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="review",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.SCORE_THRESHOLD,
        termination=(Termination.SCORE_THRESHOLD,),
        summary="Independent code review of the current changes (quality lens).",
        phases=(
            PhaseSpec(
                "review",
                "reviewer",
                "Review the current changes for correctness, quality, and risks. "
                "End with a line 'SCORE: N/10'. Focus: {prev}",
                readonly=True,
            ),
        ),
    )


def pr_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="pr",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.FIXED_PHASES_DONE,),
        summary="Summarize the branch vs base → draft a PR title/body → open it.",
        phases=(
            PhaseSpec(
                "summarize",
                "scout",
                "Summarize this branch's changes versus the base branch. "
                "Context: {prev}",
                readonly=True,
            ),
            PhaseSpec(
                "open",
                "builder",
                "Draft a clear pull-request title and body for the following "
                "and open the PR:\n{prev}",
            ),
        ),
    )


def init_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="init",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.FIXED_PHASES_DONE,),
        summary="Scan the repo → write an AGENTS.md capturing conventions.",
        phases=(
            PhaseSpec(
                "scan",
                "scout",
                "Scan the repository rooted exactly at {cwd}: languages, layout, "
                "and the build/test/lint commands. Context: {prev}",
                readonly=True,
            ),
            PhaseSpec(
                "write",
                "builder",
                "Write {cwd}/AGENTS.md (at the workflow root, not in a nested "
                "project) capturing the root project's conventions based on:\n{prev}",
            ),
        ),
    )


def coordinate_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="coordinate",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.MULTI_AGENT_DAG,
        advance=Advance.SCORE_THRESHOLD,
        termination=(Termination.SCORE_THRESHOLD,),
        summary="Architect → builder → reviewer scoring-retry loop until the "
        "review score clears the threshold.",
    )


def goal_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="goal",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.DYNAMIC_LOOP,
        advance=Advance.ARTIFACT_CHECK,
        termination=(Termination.EXTERNAL_JUDGE,),
        summary="Loop until an independent judge confirms a declarative goal.",
    )


def builtin_workflow_definitions() -> tuple[WorkflowDefinition, ...]:
    """All built-in workflow definitions for the WorkflowRegistry (single source)."""
    return (
        goal_workflow(),
        fix_workflow(),
        dream_workflow(),
        distill_workflow(),
        commit_workflow(),
        review_workflow(),
        pr_workflow(),
        init_workflow(),
        coordinate_workflow(),
    )


__all__ = [
    "SubAgentPhaseRunner",
    "commit_workflow",
    "review_workflow",
    "pr_workflow",
    "init_workflow",
    "coordinate_workflow",
    "goal_workflow",
    "builtin_workflow_definitions",
]
