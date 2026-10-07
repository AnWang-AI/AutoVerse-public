"""Checkpoint scaffolding — schema + pure budget helper (docs/architecture/runtime-flow.md).

This module lays down the section schema, a *pure* budgeting helper that decides
how much of each section to keep within a token budget, and the checkpoint
writer (:func:`write_checkpoint_via_subagent`) that drives an injected runner to
author the document. The writer is fail-open: when no runner / writer is wired
it returns an empty string so :class:`CompactionPolicy` falls back to
micro-compact rather than dead-locking.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckpointSection:
    title: str
    weight: float  # relative share of the budget (0..1, need not sum to 1)
    min_chars: int = 0


# Canonical ordered sections of a checkpoint document.
DEFAULT_CHECKPOINT_BACKEND = "micro"

CHECKPOINT_SECTIONS: tuple[CheckpointSection, ...] = (
    CheckpointSection("ORIGINAL GOAL", weight=0.10, min_chars=80),
    CheckpointSection("CURRENT STATE", weight=0.20, min_chars=120),
    CheckpointSection("KEY FACTS", weight=0.25, min_chars=120),
    CheckpointSection("DECISIONS", weight=0.15),
    CheckpointSection("OPEN THREADS", weight=0.20, min_chars=80),
    CheckpointSection("NEXT STEP", weight=0.10, min_chars=40),
)


def read_budgeted(
    sections: dict[str, str],
    budget_chars: int,
    schema: tuple[CheckpointSection, ...] = CHECKPOINT_SECTIONS,
) -> dict[str, str]:
    """Allocate ``budget_chars`` across sections by weight and trim each.

    Pure function: takes the raw per-section text and returns a trimmed copy
    that fits within the budget. Sections honour ``min_chars`` first, then the
    remaining budget is distributed by weight.
    """
    if budget_chars <= 0:
        return {s.title: "" for s in schema}

    total_weight = sum(s.weight for s in schema) or 1.0
    floor = sum(min(s.min_chars, len(sections.get(s.title, ""))) for s in schema)
    spare = max(0, budget_chars - floor)

    out: dict[str, str] = {}
    for s in schema:
        raw = sections.get(s.title, "")
        allowance = s.min_chars + int(spare * (s.weight / total_weight))
        allowance = min(allowance, budget_chars)
        out[s.title] = raw[:allowance]
    return out


def render_checkpoint(sections: dict[str, str]) -> str:
    """Render a budgeted section map into the canonical markdown document."""
    blocks: list[str] = []
    for s in CHECKPOINT_SECTIONS:
        body = sections.get(s.title, "").strip()
        if not body:
            continue
        blocks.append(f"## {s.title}\n{body}")
    return "\n\n".join(blocks)


async def write_checkpoint_via_subagent(
    runner: object,
    ctx: object,
    *,
    task: str = "",
    context: str = "",
) -> str:
    """Drive the checkpoint-writer sub-agent to author the checkpoint (§4.2a).

    Fail-open (methodology §3.4 — quality/enhancement failures degrade with a
    loud trace): if the writer sub-agent fails or is unavailable, return an empty
    string so :class:`CompactionPolicy` falls back to micro-compact rather than
    dead-locking the compaction path. The runner is injected (core stays free of
    a hard dependency on the concrete orchestrator).
    """
    write = getattr(runner, "write_checkpoint", None)
    if write is None:
        return ""
    try:
        result = await write(ctx, task=task, context=context)
    except Exception:  # noqa: BLE001 — fail-open: checkpoint must never dead-lock
        return ""
    return str(getattr(result, "summary", "") or "")


__all__ = [
    "CheckpointSection",
    "CHECKPOINT_SECTIONS",
    "DEFAULT_CHECKPOINT_BACKEND",
    "read_budgeted",
    "render_checkpoint",
    "write_checkpoint_via_subagent",
]
