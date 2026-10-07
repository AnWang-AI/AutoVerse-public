"""Configuration tree (docs/reference/config.md). Single authoritative tree."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from autoverse.types.llm import ModelTier

# frozen → hot-swap = new tree (P4); extra=forbid → reject unknown keys with a
# nearest-neighbour spelling hint at validation time (P5, F3).
_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _freeze_raw(value: Any) -> Any:
    """Recursively freeze raw config containers while preserving scalar values."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_raw(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_raw(v) for v in value)
    return value


def _thaw_raw(value: Any) -> Any:
    """Return JSON-serializable containers for frozen raw config values."""
    if isinstance(value, Mapping):
        return {k: _thaw_raw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw_raw(v) for v in value]
    return value


class CompactionConfig(BaseModel):
    # 0 = auto-derive from the *active model's* context window (the registry's
    # per-model value); a positive value is an explicit override/cap. The
    # thresholds below are fractions of whichever window is resolved at runtime,
    # so switching to a 1M-window model widens the budget without re-config.
    context_window: int = Field(
        0,
        ge=0,
        description="0 → auto-derive from the active model; >0 → explicit override/cap.",
    )
    # cascade thresholds (fractions of context_window), ascending:
    #   compact_threshold   -> micro-fold tool noise
    #   summary_threshold   -> escalate to an LLM meta-summary
    #   emergency_threshold -> hard truncate
    compact_threshold: float = Field(0.72, gt=0, le=1)
    summary_threshold: float = Field(0.82, gt=0, le=1)
    emergency_threshold: float = Field(0.90, gt=0, le=1)
    summary_output_budget: int = 4_096
    cooldown_effective: int = 1
    cooldown_low: int = 2
    cooldown_stuck: int = 5
    low_reduction_ratio: float = Field(0.30, ge=0, le=1)
    ema_alpha: float = Field(0.20, ge=0, le=1)
    model_config = _FROZEN

    @model_validator(mode="after")
    def _require_ascending_thresholds(self) -> "CompactionConfig":
        if not (
            self.compact_threshold
            <= self.summary_threshold
            <= self.emergency_threshold
        ):
            raise ValueError(
                "compaction thresholds must be ascending: "
                "compact_threshold <= summary_threshold <= emergency_threshold"
            )
        return self


class RecoveryConfig(BaseModel):
    # Only ``max_retries`` is consumed by the core RecoveryPolicy; the former
    # max_tokens_ceiling/step/base_delay knobs were never wired and were removed
    # to keep ``/config`` honest (every shown key is actually consumed, P1/P5).
    max_retries: int = 3
    model_config = _FROZEN


class ModelConfig(BaseModel):
    model_id: str = ""
    max_tokens: int = 8192
    temperature: float = 1.0
    api_key_alias: str | None = Field(
        None,
        description="Alias of the credential resolved by SecretProvider; the "
        "config tree never holds plaintext secrets (docs/reference/config.md §1).",
    )
    model_config = _FROZEN


class ModelTiersConfig(BaseModel):
    """Per-tier model ids for sub-agents (docs/reference/subagents.md §3.4).

    Each role declares an abstract ``ModelTier`` (definition.py); this maps a tier
    → concrete model id. Empty string ("") means *unset* — the role then falls
    back to the parent/main model, so the zero-config default is unchanged.
    The main agent's model is configured separately via ``model.model_id``.
    """

    deep: str = ""
    balanced: str = ""
    fast: str = ""
    model_config = _FROZEN

    def for_tier(self, tier: ModelTier) -> str | None:
        """Configured model id for a tier, or None when unset (""→None)."""
        value = {
            ModelTier.DEEP: self.deep,
            ModelTier.BALANCED: self.balanced,
            ModelTier.FAST: self.fast,
        }.get(tier, "")
        return value or None


class MemoryConfig(BaseModel):
    """Memory assembly thresholds — the single authority for memory limits (P1/F2).

    All memory budgets live here so the loader holds zero scattered constants.
    ``retrieval_*`` fields describe the optional FTS5 retrieval helper; the
    default EngineBuilder prompt path still uses cwd-only full memory assembly.
    """

    enabled: bool = True
    files: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
    max_file_chars: int = Field(
        25_000, ge=0, description="Per-file injection cap; truncated past this."
    )
    max_total_chars: int = Field(
        60_000, ge=0, description="Combined injection cap across all memory files."
    )
    scan_max_depth: int = Field(
        15, ge=0, description="DEPRECATED: no longer used (cwd-only scan)."
    )
    retrieval_enabled: bool = Field(
        False,
        description="Opt-in: always use FTS5 retrieval. Default False — full "
        "memory unless over max_total_chars (KDR-10).",
    )
    reconcile_on_search: bool = Field(
        True, description="Lazily rebuild the FTS index before each search."
    )
    model_config = _FROZEN


class BrowserConfig(BaseModel):
    """Browser automation settings for the session-scoped backend (docs/reference/tools.md §12)."""

    backend: Literal["auto", "playwright", "none"] = Field(
        "auto",
        description="'auto' uses Playwright when installed; 'none' disables browser tools.",
    )
    headless: bool = True
    viewport_width: int = Field(1280, ge=320)
    viewport_height: int = Field(900, ge=240)
    timeout_ms: float = Field(10_000, gt=0)
    model_config = _FROZEN


class MCPServerConfig(BaseModel):
    """One MCP server entry (docs/reference/config.md, docs/reference/mcp.md).

    Either ``command`` (stdio) or ``url`` (HTTP) must be set. ``args`` are appended
    to the stdio command; ``capability`` (if set) narrows the fail-closed PROCESS
    default for that server's tools (docs/reference/mcp.md).
    """

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    capability: (
        Literal["read_only", "controlled_write", "arbitrary_write", "process"] | None
    ) = None
    needs_network: bool = True
    rpc_timeout: float = Field(30.0, gt=0)
    model_config = _FROZEN

    @model_validator(mode="after")
    def _require_transport_and_freeze(self) -> "MCPServerConfig":
        if not self.command and not self.url:
            raise ValueError(
                "MCP server requires either 'command' (stdio) or 'url' (HTTP)"
            )
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        return self

    @field_serializer("args")
    def _serialize_args(self, value: tuple[str, ...] | list[str]) -> list[str]:
        return list(value)

    @field_serializer("env", "headers")
    def _serialize_mapping(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class PluginsConfig(BaseModel):
    """Extension/plugin loading config — mirrors ``config.plugins.*``.

    Disabling plugins and adding scan paths both go through this single key; see
    docs/reference/config.md.
    """

    extra_paths: list[str] = Field(
        default_factory=list,
        description="Extra extension-source directories scanned beyond the builtins.",
    )
    disabled: list[str] = Field(
        default_factory=list,
        description="Extensions/plugins disabled by name (the single authoritative key).",
    )
    model_config = _FROZEN

    @model_validator(mode="after")
    def _freeze_containers(self) -> "PluginsConfig":
        object.__setattr__(self, "extra_paths", tuple(self.extra_paths))
        object.__setattr__(self, "disabled", tuple(self.disabled))
        return self

    @field_serializer("extra_paths", "disabled")
    def _serialize_list(self, value: tuple[str, ...] | list[str]) -> list[str]:
        return list(value)


class Config(BaseModel):
    """The single authoritative config tree. frozen → hot-swap = new tree (P4)."""

    model: ModelConfig = Field(default_factory=lambda: ModelConfig())
    model_tiers: ModelTiersConfig = Field(default_factory=lambda: ModelTiersConfig())
    compaction: CompactionConfig = Field(default_factory=lambda: CompactionConfig())
    recovery: RecoveryConfig = Field(default_factory=lambda: RecoveryConfig())
    # NOTE: per-sub-agent turn ceilings (and tier/tools) are role-specific and
    # live on ``core.subagent.definition.AgentDefinition`` (scout=30, builder=400,
    # …), which the runner consumes directly. There is deliberately no global
    # ``subagent.max_turns`` here — a single number cannot be right for every role.
    memory: MemoryConfig = Field(default_factory=lambda: MemoryConfig())
    browser: BrowserConfig = Field(default_factory=lambda: BrowserConfig())

    default_permission_mode: Literal["allow_all", "deny_all", "ask"] = "ask"
    session_dir: str = "~/.autoverse/sessions"
    max_turns: int = Field(100, ge=1)
    system_prompt_append: str = ""

    # Observability + snapshot wiring (gap10). Trace folding is cheap (in-memory
    # + per-turn JSONL append) so it is on by default; the git-shadow snapshot
    # spawns `git` per turn over the whole work-tree, so it is OFF by default
    # (determinism/perf guard) and opted in for the `/rewind` workflow.
    trace_enabled: bool = True
    snapshot_enabled: bool = False
    log_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: str = ""
    log_json: bool = True
    log_max_bytes: int = Field(5_000_000, ge=1)
    log_backup_count: int = Field(3, ge=0)

    # Extension sources (docs/reference/extensions.md; wired by EngineBuilder).
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    plugins: PluginsConfig = Field(default_factory=lambda: PluginsConfig())
    # Raw hook config (event-name → list of hook entries); parsed into a HookEngine
    # by the app layer. Kept as a plain mapping here to avoid the config tree
    # importing infra hook types (P3); shape is validated at parse time.
    hooks: dict[str, Any] | None = None

    model_config = _FROZEN

    @model_validator(mode="after")
    def _freeze_containers(self) -> "Config":
        object.__setattr__(self, "mcp_servers", MappingProxyType(dict(self.mcp_servers)))
        if self.hooks is not None:
            object.__setattr__(self, "hooks", _freeze_raw(self.hooks))
        return self

    @field_serializer("mcp_servers")
    def _serialize_mcp_servers(
        self, value: Mapping[str, MCPServerConfig]
    ) -> dict[str, MCPServerConfig]:
        return dict(value)

    @field_serializer("hooks")
    def _serialize_hooks(self, value: Any) -> Any:
        return None if value is None else _thaw_raw(value)

    def model_for_tier(self, tier: ModelTier) -> str | None:
        """Single-source tier→model lookup (None when that tier is unset)."""
        return self.model_tiers.for_tier(tier)


__all__ = [
    "Config",
    "CompactionConfig",
    "RecoveryConfig",
    "ModelConfig",
    "ModelTiersConfig",
    "MemoryConfig",
    "BrowserConfig",
    "MCPServerConfig",
    "PluginsConfig",
]
