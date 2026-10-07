"""Acceptance — config / secrets / memory (docs/reference/testing.md §13, F1–F8).

Each test maps to one acceptance row with a Given/When/Then docstring.
F4/F7 skip gracefully when ``cryptography`` is absent (pytest.importorskip).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autoverse.app.config import (
    CompactionConfig,
    Config,
    MCPServerConfig,
    ModelTiersConfig,
    PluginsConfig,
    RecoveryConfig,
)
from autoverse.infra.hooks.engine import HookEvent
from autoverse.infra.hooks.shell import parse_hooks_config
from autoverse.infra.config import ConfigError, load_config
from autoverse.infra.memory import MemoryLoader, should_use_retrieval
from autoverse.infra.observ.redact import Redactor
from autoverse.types.llm import ModelTier


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# F1 — layered override precedence
# --------------------------------------------------------------------------- #
def test_f1_layered_override_final_value_deterministic(tmp_path: Path) -> None:
    """Given default<global<project<env<cli with the same key at different
    values, When loaded, Then the highest layer wins and the value is fixed."""
    global_dir = tmp_path / "global"
    _write_json(global_dir / "config.json", {"max_turns": 10})
    project = tmp_path / "proj"
    _write_json(project / ".autoverse" / "config.json", {"max_turns": 20})

    loaded = load_config(
        project,
        env={"AUTOVERSE_MAX_TURNS": "30"},
        cli={"max_turns": 40},
        global_dir=global_dir,
    )
    assert loaded.config.max_turns == 40
    assert loaded.sources["max_turns"] == "cli"

    # And lowering the highest layer makes the next layer win — deterministic.
    loaded2 = load_config(
        project, env={"AUTOVERSE_MAX_TURNS": "30"}, cli={}, global_dir=global_dir
    )
    assert loaded2.config.max_turns == 30
    assert loaded2.sources["max_turns"] == "env"


# --------------------------------------------------------------------------- #
# F2 — thresholds centralised in config sub-models (P1)
# --------------------------------------------------------------------------- #
def test_f2_thresholds_come_from_config_submodels(tmp_path: Path) -> None:
    """Given compaction/recovery thresholds, When read, Then they all live on
    CompactionConfig/RecoveryConfig — not scattered. (Per-sub-agent turn ceilings
    are role-specific and live on AgentDefinition, not a global config knob.)"""
    cfg = Config()
    assert isinstance(cfg.compaction, CompactionConfig)
    assert isinstance(cfg.recovery, RecoveryConfig)
    # The tunables are addressable on the config tree.
    assert cfg.compaction.compact_threshold == pytest.approx(0.72)
    assert cfg.compaction.summary_threshold == pytest.approx(0.82)
    assert cfg.compaction.emergency_threshold == pytest.approx(0.90)
    assert cfg.recovery.max_retries == 3
    # context_window defaults to 0 = "auto-derive from the active model".
    assert cfg.compaction.context_window == 0

    # And the loader holds no scattered threshold constants of its own.
    loader_src = (
        Path(__file__).resolve().parents[2]
        / "src/autoverse/infra/config/loader.py"
    ).read_text(encoding="utf-8")
    assert "0.72" not in loader_src
    assert "compact_threshold = 0" not in loader_src


def test_config_snapshot_rejects_in_place_mutation(tmp_path: Path) -> None:
    """Given a loaded config snapshot, When callers try to mutate it in place,
    Then field and nested-container mutation are rejected; hot-swap must build a
    new tree instead."""
    cfg = Config(
        mcp_servers={
            "local": MCPServerConfig(
                command="python -m server",
                args=["--root", str(tmp_path)],
                env={"TOKEN_ALIAS": "TOKEN"},
                headers={"X-Test": "1"},
            )
        },
        plugins=PluginsConfig(
            extra_paths=[str(tmp_path / "plugins")],
            disabled=["off"],
        ),
        hooks={"PreToolUse": [{"command": "echo {}", "matcher": "write_*"}]},
    )

    with pytest.raises(ValidationError):
        cfg.max_turns = 200  # type: ignore[misc]
    with pytest.raises(TypeError):
        cfg.mcp_servers["other"] = MCPServerConfig(  # type: ignore[index]
            command="python"
        )
    with pytest.raises(AttributeError):
        cfg.mcp_servers["local"].args.append("--debug")
    with pytest.raises(TypeError):
        cfg.mcp_servers["local"].env["TOKEN_ALIAS"] = "OTHER"  # type: ignore[index]
    with pytest.raises(TypeError):
        cfg.mcp_servers["local"].headers["X-Test"] = "2"  # type: ignore[index]
    with pytest.raises(AttributeError):
        cfg.plugins.disabled.append("another")
    assert cfg.hooks is not None
    with pytest.raises(TypeError):
        cfg.hooks["PreToolUse"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        cfg.hooks["PreToolUse"][0]["command"] = "echo changed"  # type: ignore[index]

    dumped = cfg.model_dump()
    assert dumped["mcp_servers"]["local"]["args"] == ["--root", str(tmp_path)]
    assert dumped["plugins"]["disabled"] == ["off"]
    assert dumped["hooks"] == {
        "PreToolUse": [{"command": "echo {}", "matcher": "write_*"}]
    }

    parsed = parse_hooks_config(cfg.hooks, tmp_path)
    assert parsed[HookEvent.PRE_TOOL_USE][0].matcher == "write_*"


# --------------------------------------------------------------------------- #
# model_tiers — per-tier sub-agent model mapping (docs/reference/subagents.md §3.4)
# --------------------------------------------------------------------------- #
def test_model_tiers_default_unset_falls_back() -> None:
    """Given no model_tiers config, When resolved, Then every tier is None so
    sub-agents fall back to the parent/main model (zero-config = no behaviour change)."""
    cfg = Config()
    assert isinstance(cfg.model_tiers, ModelTiersConfig)
    for tier in ModelTier:
        assert cfg.model_for_tier(tier) is None


def test_model_tiers_loaded_and_resolved(tmp_path: Path) -> None:
    """Given model_tiers in config, When loaded, Then each tier maps to its id
    and an empty string is treated as unset (→ None)."""
    global_dir = tmp_path / "global"
    _write_json(
        global_dir / "config.json",
        {"model_tiers": {"deep": "big-model", "fast": "small-model"}},
    )
    loaded = load_config(tmp_path, env={}, cli={}, global_dir=global_dir)
    cfg = loaded.config
    assert cfg.model_for_tier(ModelTier.DEEP) == "big-model"
    assert cfg.model_for_tier(ModelTier.FAST) == "small-model"
    assert cfg.model_for_tier(ModelTier.BALANCED) is None  # "" default → unset


def test_model_tiers_rejects_unknown_key(tmp_path: Path) -> None:
    """Given a misspelled tier key, When loaded, Then a rich error names it
    (extra=forbid guards typos like 'depe')."""
    global_dir = tmp_path / "global"
    _write_json(global_dir / "config.json", {"model_tiers": {"depe": "x"}})
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path, env={}, cli={}, global_dir=global_dir)
    assert "model_tiers.depe" in str(exc.value)


# --------------------------------------------------------------------------- #
# F3 — rich validation error names the field
# --------------------------------------------------------------------------- #
def test_f3_invalid_config_raises_rich_error_naming_field(tmp_path: Path) -> None:
    """Given an illegal value (threshold>1), When loaded, Then a rich error
    names the offending field/value/source — not silently accepted."""
    global_dir = tmp_path / "global"
    _write_json(global_dir / "config.json", {"compaction": {"compact_threshold": 1.5}})
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path, env={}, cli={}, global_dir=global_dir)
    msg = str(exc.value)
    assert "compaction.compact_threshold" in msg
    assert "1.5" in msg
    assert "global" in msg  # source layer is reported


# --------------------------------------------------------------------------- #
# F4 — AES-256-GCM machine-bound vault; plaintext never on disk
# --------------------------------------------------------------------------- #
def test_f4_vault_roundtrip_plaintext_never_on_disk(tmp_path: Path) -> None:
    """Given an AES-256-GCM machine-bound vault, When set then get, Then it
    roundtrips correctly and no plaintext is written to disk."""
    pytest.importorskip("cryptography")
    from autoverse.infra.secrets import SecretVault

    path = tmp_path / "secrets.enc"
    vault = SecretVault(path, machine_secret="test-machine")
    vault.set("ANTHROPIC_API_KEY", "sk-ant-PLAINTEXT")
    assert vault.get("ANTHROPIC_API_KEY") == "sk-ant-PLAINTEXT"

    raw = path.read_bytes()
    assert b"sk-ant-PLAINTEXT" not in raw
    assert (path.stat().st_mode & 0o777) == 0o600

    # A fresh instance with the same machine binding decrypts identically.
    assert SecretVault(path, machine_secret="test-machine").get(
        "ANTHROPIC_API_KEY"
    ) == "sk-ant-PLAINTEXT"


# --------------------------------------------------------------------------- #
# F5 — env wins over vault on the same key
# --------------------------------------------------------------------------- #
def test_f5_env_wins_over_vault(tmp_path: Path) -> None:
    """Given env and vault hold the same key, When get, Then env wins."""
    pytest.importorskip("cryptography")
    from autoverse.infra.secrets import EnvFirstSecretProvider, SecretVault

    vault = SecretVault(tmp_path / "secrets.enc", machine_secret="m")
    vault.set("K", "from-vault")
    provider = EnvFirstSecretProvider(vault, env={"K": "from-env"})
    assert provider.get("K") == "from-env"
    assert provider.require("K") == "from-env"


# --------------------------------------------------------------------------- #
# F6 — secrets never leak into trace/prompt/log (O7)
# --------------------------------------------------------------------------- #
def test_f6_secret_value_redacted_from_trace_and_prompt() -> None:
    """Given a run carrying a secret value, When it passes through redact(),
    Then no plaintext secret appears in the trace/prompt string."""
    secret = "sk-ant-super-secret-value-123"
    redactor = Redactor([secret])

    sample_trace = {
        "request": {"headers": {"authorization": f"Bearer {secret}"}},
        "prompt": f"system prompt mentioning {secret} accidentally",
        "log_line": f"calling api with {secret}",
    }
    masked = redactor.value(sample_trace)
    assert secret not in json.dumps(masked)


# --------------------------------------------------------------------------- #
# F7 — machine migration: friendly degrade, not a crash
# --------------------------------------------------------------------------- #
def test_f7_machine_migration_friendly_degrade(tmp_path: Path) -> None:
    """Given a vault written on machine A, When read on machine B (wrong key),
    Then a friendly DecryptError with recovery guidance is raised — not a raw
    crash — and the env-first provider degrades to env-only."""
    pytest.importorskip("cryptography")
    from autoverse.infra.secrets import (
        DecryptError,
        EnvFirstSecretProvider,
        SecretVault,
    )

    path = tmp_path / "secrets.enc"
    SecretVault(path, machine_secret="machine-A").set("K", "v")

    migrated = SecretVault(path, machine_secret="machine-B")
    with pytest.raises(DecryptError) as exc:
        migrated.get("K")
    guidance = str(exc.value).lower()
    assert "machine" in guidance
    assert "delete" in guidance or "recover" in guidance

    # Env-first provider does not crash; it falls back to env-only.
    provider = EnvFirstSecretProvider(migrated, env={"K": "env-v"})
    assert provider.get("K") == "env-v"


# --------------------------------------------------------------------------- #
# F8 — cwd-only memory injection
# --------------------------------------------------------------------------- #
def test_f8_memory_loader_reads_only_cwd(tmp_path: Path) -> None:
    """Given AGENTS.md across home/project/subdir, when assembled, then only
    the cwd file is injected and role trimming does not drop that cwd block."""
    home = tmp_path / "home"
    project = home / "work" / "proj"
    subdir = project / "src"
    subdir.mkdir(parents=True)

    (home / "AGENTS.md").write_text("GLOBAL conventions", encoding="utf-8")
    (project / "AGENTS.md").write_text("PROJECT conventions", encoding="utf-8")
    (subdir / "AGENTS.md").write_text("SUBDIR conventions", encoding="utf-8")

    cfg = Config().memory
    loader = MemoryLoader(cfg)

    full = loader.assemble(subdir, home=home)
    assert [b.scope for b in full.blocks] == ["subdir"]
    assert [b.content for b in full.blocks] == ["SUBDIR conventions"]
    rendered = full.render()
    assert "GLOBAL conventions" not in rendered
    assert "PROJECT conventions" not in rendered
    assert "SUBDIR conventions" in rendered

    # Researcher omits global/session scopes, but cwd-only memory stays intact.
    researcher = loader.assemble(subdir, home=home, role="researcher")
    assert [b.scope for b in researcher.blocks] == ["subdir"]
    assert researcher.render() == rendered


# --------------------------------------------------------------------------- #
# KDR-10 — retrieval is OFF by default (full memory unless over budget)
# --------------------------------------------------------------------------- #
def test_kdr10_retrieval_off_by_default() -> None:
    """Given default memory config, When deciding the injection path, Then
    retrieval stays off unless explicitly enabled or over the char budget."""
    assert should_use_retrieval(
        total_chars=100, max_total_chars=60_000, retrieval_enabled=False
    ) is False
    # Over budget → engage.
    assert should_use_retrieval(
        total_chars=70_000, max_total_chars=60_000, retrieval_enabled=False
    ) is True
    # Explicit opt-in always engages.
    assert should_use_retrieval(
        total_chars=100, max_total_chars=60_000, retrieval_enabled=True
    ) is True


# --------------------------------------------------------------------------- #
# Compaction config wiring — app tree maps onto the core policy config
# --------------------------------------------------------------------------- #
def test_compaction_thresholds_must_be_ascending() -> None:
    """Given an out-of-order threshold set, When the model validates, Then it
    is rejected (compact <= summary <= emergency)."""
    with pytest.raises(ValueError, match="ascending"):
        CompactionConfig(compact_threshold=0.9, summary_threshold=0.8)


def test_build_compaction_config_maps_app_tree_to_core() -> None:
    """Given the app compaction tree, When the builder maps it, Then the core
    CompactionConfig the policy consumes reflects the overrides (not defaults)."""
    from autoverse.app.builder import _build_compaction_config

    app_cfg = CompactionConfig(
        context_window=123_456,
        compact_threshold=0.6,
        summary_threshold=0.7,
        emergency_threshold=0.85,
        cooldown_effective=2,
        cooldown_low=3,
        cooldown_stuck=7,
        low_reduction_ratio=0.25,
        ema_alpha=0.4,
        summary_output_budget=3_000,
    )
    core = _build_compaction_config(app_cfg)
    assert core.context_window == 123_456
    assert core.micro_ratio == pytest.approx(0.6)
    assert core.compact_ratio == pytest.approx(0.7)
    assert core.truncate_ratio == pytest.approx(0.85)
    assert core.cooldown_effective == 2
    assert core.cooldown_low == 3
    assert core.cooldown_stuck == 7
    assert core.low_reduction_ratio == pytest.approx(0.25)
    assert core.ema_alpha == pytest.approx(0.4)
    assert core.summary_budget == 3_000
    # the meta-summary threshold is now distinct from the micro threshold.
    assert core.compact_threshold() == int(123_456 * 0.7)
