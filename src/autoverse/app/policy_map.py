"""Map the app Config policy trees onto the core policy config dataclasses.

The app config tree (pydantic, user-facing) and the core policy configs (frozen
dataclasses the policies actually read) have deliberately different shapes — the
app tree names things by stage/operator intent, the core configs by the
algorithm's tunables. These pure translators are the single place that bridges
them, so both the main :class:`EngineBuilder` and the recursive sub-agent
factory wire identical, user-honoured configs (P1 — no scattered mapping).

Lives in ``app/`` (may import ``core``); imports neither ``builder`` nor
``subagent`` so it is safe to import from both without a cycle.
"""

from __future__ import annotations

from autoverse.app.config import (
    CompactionConfig as AppCompactionConfig,
    RecoveryConfig as AppRecoveryConfig,
)
from autoverse.core.context.config import (
    CompactionConfig as CoreCompactionConfig,
    RecoveryConfig as CoreRecoveryConfig,
)


def build_core_compaction_config(app_cfg: AppCompactionConfig) -> CoreCompactionConfig:
    """Translate the app Config compaction tree into the core CompactionConfig.

    The app tree names thresholds by stage (compact/summary/emergency); the core
    policy uses ratios (micro/compact/truncate). Fields with no app mirror keep
    their core defaults (``protected_tail``, ``micro_keep_tail``,
    ``max_message_chars``, ``summary_retries``, ``summary_template``).

    ``context_window`` is passed through verbatim: ``0`` means "auto" and the
    builder/sub-agent factory wire a ``window_provider`` onto the CompactionPolicy
    that resolves the live model window at runtime (a positive value is an
    explicit override). The policy treats this value as a static fallback.
    """
    return CoreCompactionConfig(
        context_window=app_cfg.context_window,
        micro_ratio=app_cfg.compact_threshold,
        compact_ratio=app_cfg.summary_threshold,
        truncate_ratio=app_cfg.emergency_threshold,
        cooldown_effective=app_cfg.cooldown_effective,
        cooldown_low=app_cfg.cooldown_low,
        cooldown_stuck=app_cfg.cooldown_stuck,
        low_reduction_ratio=app_cfg.low_reduction_ratio,
        ema_alpha=app_cfg.ema_alpha,
        summary_budget=app_cfg.summary_output_budget,
    )


def build_core_recovery_config(app_cfg: AppRecoveryConfig) -> CoreRecoveryConfig:
    """Translate the app Config recovery tree into the core RecoveryConfig.

    ``max_retries`` is the only user-facing recovery knob; the core-only fields
    (``max_fallbacks``, ``max_resume_text``, ``resume_token_bonus``,
    ``enable_json_tool_recovery``) keep their defaults.
    """
    return CoreRecoveryConfig(max_retries=app_cfg.max_retries)


__all__ = ["build_core_compaction_config", "build_core_recovery_config"]
