"""Unit tests for checkpoint schema and budget helper."""

from __future__ import annotations

from autoverse.core.context.checkpoint import (
    CHECKPOINT_SECTIONS,
    CheckpointSection,
    read_budgeted,
    render_checkpoint,
    write_checkpoint_via_subagent,
)


def test_sections_default_backend_micro_schema_present() -> None:
    titles = [s.title for s in CHECKPOINT_SECTIONS]
    assert "ORIGINAL GOAL" in titles
    assert "NEXT STEP" in titles
    assert all(isinstance(s, CheckpointSection) for s in CHECKPOINT_SECTIONS)


def test_L17_read_budgeted_fits_within_budget() -> None:
    raw = {s.title: "z" * 1000 for s in CHECKPOINT_SECTIONS}
    out = read_budgeted(raw, budget_chars=600)
    total = sum(len(v) for v in out.values())
    assert total <= 600 + sum(s.min_chars for s in CHECKPOINT_SECTIONS)
    # weighted: KEY FACTS (0.25) gets more than NEXT STEP (0.10)
    assert len(out["KEY FACTS"]) >= len(out["NEXT STEP"])


def test_read_budgeted_zero_budget() -> None:
    raw = {s.title: "x" * 100 for s in CHECKPOINT_SECTIONS}
    out = read_budgeted(raw, budget_chars=0)
    assert all(v == "" for v in out.values())


def test_render_checkpoint_skips_empty() -> None:
    rendered = render_checkpoint({"ORIGINAL GOAL": "do the thing", "KEY FACTS": ""})
    assert "## ORIGINAL GOAL" in rendered
    assert "## KEY FACTS" not in rendered


async def test_subagent_writer_fail_open_without_runner() -> None:
    # Phase 4: forwards to the injected runner; fail-open when unavailable.
    assert await write_checkpoint_via_subagent(None, None) == ""


async def test_subagent_writer_forwards_to_runner() -> None:
    class _Result:
        summary = "checkpoint written"

    class _Runner:
        async def write_checkpoint(self, ctx: object, *, task: str = "", context: str = "") -> object:
            return _Result()

    out = await write_checkpoint_via_subagent(_Runner(), object(), task="t")
    assert out == "checkpoint written"


async def test_subagent_writer_fail_open_on_runner_error() -> None:
    class _Runner:
        async def write_checkpoint(self, ctx: object, *, task: str = "", context: str = "") -> object:
            raise RuntimeError("boom")

    assert await write_checkpoint_via_subagent(_Runner(), object()) == ""
