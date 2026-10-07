"""LLM value types (docs/architecture/contracts.md §5)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from autoverse.types.messages import Message


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool = True
    thinking: bool = False
    cache: bool = False
    vision: bool = False
    degradation_prone: bool = False


class ModelTier(str, Enum):
    """Abstract intelligence tier a role needs (docs/reference/subagents.md §3.4).

    Roles declare a *tier* (their intrinsic reasoning need), not a concrete
    model id; the config tree maps each tier → model id (config.model_tiers).
    Unmapped tier → fall back to the parent/main model (zero-config default).
    """

    DEEP = "deep"          # hardest reasoning: orchestration, design, root-cause
    BALANCED = "balanced"  # default: execute a given plan competently
    FAST = "fast"          # mechanical: search, CI-run, templated writes


class WireKind(Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class AuthKind(Enum):
    BEARER = "bearer"
    HMAC = "hmac"
    APPKEY = "appkey"
    NONE = "none"


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    json_schema: dict[str, object]
    tier: int = 0


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[Message, ...]
    system: str
    tools: tuple[ToolSchema, ...] = ()
    model: str = ""
    max_tokens: int = 8192
    temperature: float = 1.0
    thinking_budget: int = 0


class ErrorClass(Enum):
    RETRYABLE = "retryable"
    FALLBACK_TRIGGER = "fallback"
    PERMANENT = "permanent"


__all__ = [
    "ProviderCapabilities",
    "ModelTier",
    "WireKind",
    "AuthKind",
    "ToolSchema",
    "LLMRequest",
    "ErrorClass",
]
