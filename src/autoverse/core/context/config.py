"""Compaction and recovery configuration (docs/reference/config.md).

All thresholds and tunables for the context-management subsystem live here as
frozen dataclasses so there are no scattered magic constants (P1/P4). The loop
and policies read everything through these objects; tests construct variants by
field overrides only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompactionConfig:
    """Thresholds for the CompactionPolicy.

    Ratios are fractions of ``context_window``. The cascade is:
      micro_compact (fold tool noise)  -> at ``micro_ratio``
      meta summarise (LLM summary)     -> at ``compact_ratio``
      emergency truncate               -> at ``truncate_ratio``
    """

    context_window: int = 200_000
    micro_ratio: float = 0.72
    compact_ratio: float = 0.82
    truncate_ratio: float = 0.90

    # number of trailing messages always kept verbatim (the "protected tail").
    protected_tail: int = 6
    # micro-compaction keeps at least this many trailing messages intact.
    micro_keep_tail: int = 4
    # graded cooldown by reduction effectiveness (docs/architecture/runtime-flow.md §4.7):
    # effective reduction (>= low_reduction_ratio) -> short cooldown;
    # low reduction -> longer; repeated low ("stuck") -> longest.
    cooldown_effective: int = 1
    cooldown_low: int = 2
    cooldown_stuck: int = 5
    # a compaction whose reduction ratio is below this is "low/ineffective".
    low_reduction_ratio: float = 0.30
    # EMA smoothing factor for the calibrated token estimate.
    ema_alpha: float = 0.3
    # max chars retained when truncating a single oversized message.
    max_message_chars: int = 20_000
    # token budget allotted to a generated summary.
    summary_budget: int = 2_000
    # retries when the summariser LLM call fails.
    summary_retries: int = 1
    # summary prompt template; empty -> runtime fallback to SUMMARY_TEMPLATE
    # (kept default-empty to avoid import-order coupling).
    summary_template: str = ""

    def micro_threshold(self) -> int:
        return int(self.context_window * self.micro_ratio)

    def compact_threshold(self) -> int:
        return int(self.context_window * self.compact_ratio)

    def truncate_threshold(self) -> int:
        return int(self.context_window * self.truncate_ratio)


@dataclass(frozen=True)
class RecoveryConfig:
    """Tunables for the RecoveryPolicy (docs/reference/models.md §9-§11)."""

    max_retries: int = 2
    max_fallbacks: int = 1
    # how many times a MAX_TOKENS-truncated turn may be auto-resumed.
    max_resume_text: int = 1
    # extra output-token budget granted to a resumed turn.
    resume_token_bonus: int = 4_000
    # whether to attempt inline-JSON tool recovery (gated by capabilities).
    enable_json_tool_recovery: bool = True


__all__ = ["CompactionConfig", "RecoveryConfig"]
