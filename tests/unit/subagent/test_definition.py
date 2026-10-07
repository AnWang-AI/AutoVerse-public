"""AgentDefinition single-source-of-truth invariants (docs/reference/subagents.md §3.5, §4, §13)."""

from __future__ import annotations

from autoverse.core.subagent.definition import (
    AGENT_DEFINITIONS,
    IsolationProfile,
    get_definition,
)
from autoverse.core.subagent.runner import _config_tier_model, _resolve_model
from autoverse.types.capability import CapabilityTier
from autoverse.types.llm import ModelTier


def test_nine_selectable_roles_plus_checkpoint_writer() -> None:
    names = set(AGENT_DEFINITIONS)
    selectable = {
        "scout",
        "architect",
        "builder",
        "validator",
        "reviewer",
        "e2e-verifier",
        "debugger",
        "researcher",
        "generalist",
    }
    assert selectable <= names
    assert "checkpoint-writer" in names


def test_unknown_role_falls_back_to_generalist() -> None:
    assert get_definition("totally-unknown").name == "generalist"


def test_scout_architect_are_read_only() -> None:
    for name in ("scout", "architect"):
        d = AGENT_DEFINITIONS[name]
        assert d.max_tier == CapabilityTier.READ_ONLY
        assert d.can_network is False
        assert d.bash_check_mode is False


def test_validator_reviewer_are_process_with_check_mode() -> None:
    for name in ("validator", "reviewer"):
        d = AGENT_DEFINITIONS[name]
        assert d.max_tier == CapabilityTier.PROCESS
        assert d.bash_check_mode is True


def test_researcher_can_network_but_read_only_and_omit_memory() -> None:
    d = AGENT_DEFINITIONS["researcher"]
    assert d.max_tier == CapabilityTier.READ_ONLY
    assert d.can_network is True
    assert d.omit_memory is True


def test_only_e2e_verifier_has_isolation_profile() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        if name == "e2e-verifier":
            assert isinstance(d.isolation, IsolationProfile)
        else:
            assert d.isolation is None


def test_e2e_verifier_isolation_fields() -> None:
    d = AGENT_DEFINITIONS["e2e-verifier"]
    assert d.max_tier == CapabilityTier.PROCESS
    assert d.bash_check_mode is False
    assert d.isolation is not None
    assert d.isolation.force_isolation is True
    assert d.isolation.post_audit == "git_diff_must_be_empty"
    assert d.isolation.process_lifecycle == "bound"


def test_checkpoint_writer_is_controlled_write_meta_only() -> None:
    d = AGENT_DEFINITIONS["checkpoint-writer"]
    assert d.max_tier == CapabilityTier.CONTROLLED_WRITE
    assert d.can_network is False


def test_model_resolution_priority() -> None:
    assert _resolve_model("o", "d", "p") == "o"
    assert _resolve_model(None, "d", "p") == "d"
    assert _resolve_model(None, None, "p") == "p"


def test_role_tiers_match_intelligence_need() -> None:
    expected = {
        "scout": ModelTier.FAST,
        "validator": ModelTier.FAST,
        "checkpoint-writer": ModelTier.FAST,
        "architect": ModelTier.DEEP,
        "debugger": ModelTier.DEEP,
        "builder": ModelTier.BALANCED,
        "reviewer": ModelTier.BALANCED,
        "e2e-verifier": ModelTier.BALANCED,
        "researcher": ModelTier.BALANCED,
        "generalist": ModelTier.BALANCED,
    }
    for name, tier in expected.items():
        assert AGENT_DEFINITIONS[name].tier is tier


class _FakeConfig:
    def __init__(self, mapping: dict[ModelTier, str | None]) -> None:
        self._mapping = mapping

    def model_for_tier(self, tier: ModelTier) -> str | None:
        return self._mapping.get(tier)


class _FakeApp:
    def __init__(self, config: object) -> None:
        self.config = config


def test_config_tier_model_reads_live_config() -> None:
    app = _FakeApp(_FakeConfig({ModelTier.FAST: "small", ModelTier.DEEP: "big"}))
    assert _config_tier_model(app, AGENT_DEFINITIONS["scout"]) == "small"
    assert _config_tier_model(app, AGENT_DEFINITIONS["debugger"]) == "big"
    # balanced unset → None → caller falls back to the parent model.
    assert _config_tier_model(app, AGENT_DEFINITIONS["builder"]) is None


def test_config_tier_model_is_fail_safe() -> None:
    # No config / wrong shape must degrade to None, never raise (spawn must survive).
    assert _config_tier_model(_FakeApp(None), AGENT_DEFINITIONS["scout"]) is None
    assert _config_tier_model(object(), AGENT_DEFINITIONS["scout"]) is None


def test_full_resolution_order_with_tier() -> None:
    # per-call > role explicit model > config tier > parent (runner call-site logic).
    app = _FakeApp(_FakeConfig({ModelTier.FAST: "tier-fast"}))
    scout = AGENT_DEFINITIONS["scout"]
    tier_model = _config_tier_model(app, scout)
    # per-call wins
    assert _resolve_model("call", scout.model or tier_model, "parent") == "call"
    # tier wins when no per-call/explicit
    assert _resolve_model(None, scout.model or tier_model, "parent") == "tier-fast"
    # parent fallback when tier unset
    app_empty = _FakeApp(_FakeConfig({}))
    tm = _config_tier_model(app_empty, scout)
    assert _resolve_model(None, scout.model or tm, "parent") == "parent"
