"""Tool & permission value types shared across modules (docs/architecture/contracts.md §4.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from autoverse.types.capability import CapabilityTier


@dataclass(frozen=True)
class ToolCall:
    """A single call as seen by the permission/dispatch layer (derived from ToolUseBlock)."""

    id: str
    name: str
    input: dict[str, object]
    tier: CapabilityTier = CapabilityTier.READ_ONLY


@dataclass
class ToolResult:
    """Sole definition. Documentation tables are summaries only."""

    output: str
    is_error: bool = False
    error_kind: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return not self.is_error

    @classmethod
    def ok(cls, output: str, **metadata: Any) -> "ToolResult":
        return cls(output=output, is_error=False, metadata=dict(metadata))

    @classmethod
    def error(cls, message: str, error_kind: str = "", **metadata: Any) -> "ToolResult":
        return cls(
            output=message,
            is_error=True,
            error_kind=error_kind,
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class Decision:
    """Permission verdict. `ask` is resolved internally by the Port; only allow/deny escape."""

    action: Literal["allow", "deny"]
    reason: str | None = None
    rule: str | None = None

    def __post_init__(self) -> None:
        if self.action == "deny" and self.reason is None:
            raise ValueError("Decision.deny must carry a reason (P5)")

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @classmethod
    def allow(cls, rule: str | None = None) -> "Decision":
        return cls(action="allow", reason=None, rule=rule)

    @classmethod
    def deny(cls, reason: str, rule: str | None = None) -> "Decision":
        return cls(action="deny", reason=reason, rule=rule)


__all__ = ["ToolCall", "ToolResult", "Decision", "CapabilityTier"]
