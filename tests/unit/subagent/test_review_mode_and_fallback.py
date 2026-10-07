"""gap6 — reviewer review_mode variants, unknown-type DEBUG fallback, filter fail-safe."""

from __future__ import annotations

from autoverse.core.subagent.definition import get_definition, is_known_definition
from autoverse.core.subagent.prompts import REVIEWER_MODE_VARIANTS
from autoverse.core.subagent.runner import SpawnRequest, _build_prompt, _filter_tools


def test_is_known_definition() -> None:
    assert is_known_definition("reviewer") is True
    assert is_known_definition("not-a-role") is False


def test_reviewer_compliance_variant_in_prompt() -> None:
    adef = get_definition("reviewer")
    req = SpawnRequest(subagent_type="reviewer", task="check", review_mode="compliance")
    prompt = _build_prompt(adef, req, None)
    assert "Review Mode: COMPLIANCE" in prompt
    assert "PASS <item>" in prompt
    assert "FAIL <item> - <why>" in prompt
    assert "VERDICT: PASS | VERDICT: FAIL" in prompt
    assert "✅" not in prompt
    assert "❌" not in prompt


def test_reviewer_quality_variant_in_prompt() -> None:
    adef = get_definition("reviewer")
    req = SpawnRequest(subagent_type="reviewer", task="check", review_mode="quality")
    prompt = _build_prompt(adef, req, None)
    assert "Review Mode: QUALITY" in prompt
    assert "SCORE: N/10" in prompt


def test_reviewer_without_review_mode_has_no_variant() -> None:
    adef = get_definition("reviewer")
    req = SpawnRequest(subagent_type="reviewer", task="check")
    prompt = _build_prompt(adef, req, None)
    assert "Review Mode:" not in prompt


def test_non_reviewer_ignores_review_mode() -> None:
    adef = get_definition("builder")
    req = SpawnRequest(subagent_type="builder", task="do", review_mode="quality")
    prompt = _build_prompt(adef, req, None)
    assert "Review Mode:" not in prompt


def test_both_variants_registered() -> None:
    assert set(REVIEWER_MODE_VARIANTS) == {"compliance", "quality"}


def test_filter_tools_fail_safe_on_corrupt_registry() -> None:
    class _BoomRegistry:
        # Looks like a ToolRegistry to isinstance? No — _filter_tools returns None
        # for non-ToolRegistry. So we make a real one and break it.
        pass

    from autoverse.core.tools.registry import ToolRegistry

    reg = ToolRegistry.build()
    adef = get_definition("scout")

    # monkeypatch names() to raise, simulating corruption
    def _boom() -> list[str]:
        raise RuntimeError("corrupt registry")

    reg.names = _boom  # type: ignore[method-assign]
    out = _filter_tools(reg, adef)
    # fail-safe: returns an (empty) ToolRegistry, does not raise
    assert isinstance(out, ToolRegistry)
    assert out.names() == []


def test_task_under_cap_not_truncated() -> None:
    """A long task (>800 chars, the old cap) is injected verbatim now."""
    from autoverse.core.subagent.runner import _MAX_TASK_CHARS

    adef = get_definition("builder")
    body = "x" * 5000  # well over the old 800 cap, well under the 32k cap
    req = SpawnRequest(subagent_type="builder", task=body)
    prompt = _build_prompt(adef, req, None)
    assert body in prompt  # full body present, no truncation
    assert _MAX_TASK_CHARS == 32_000


def test_task_truncated_at_cap() -> None:
    """A task exceeding the 32k cap is truncated to exactly the cap."""
    from autoverse.core.subagent.runner import _MAX_TASK_CHARS

    adef = get_definition("builder")
    body = "y" * (_MAX_TASK_CHARS + 1000)
    req = SpawnRequest(subagent_type="builder", task=body)
    prompt = _build_prompt(adef, req, None)
    assert "y" * _MAX_TASK_CHARS in prompt
    assert "y" * (_MAX_TASK_CHARS + 1) not in prompt
