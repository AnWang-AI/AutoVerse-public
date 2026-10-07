"""Internal message & content-block types (docs/architecture/contracts.md §1).

Single source of truth for the harness's internal message format, independent
of any backend wire format. All content blocks are frozen (immutable) so they
can be passed and cached safely across the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Union

Role = Literal["user", "assistant", "system"]


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageBlock:
    media_type: str
    data: str
    source_type: Literal["base64", "url"] = "base64"


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: str | None = None


ContentBlock = Union[
    TextBlock, ToolUseBlock, ToolResultBlock, ImageBlock, ThinkingBlock
]


@dataclass(frozen=True)
class Message:
    role: Role
    content: tuple[ContentBlock, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Message.content must contain at least one block")
        # normalise content to a tuple so equality/hashing is stable even if a
        # list was passed in.
        if not isinstance(self.content, tuple):
            object.__setattr__(self, "content", tuple(self.content))


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


__all__ = [
    "Role",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ImageBlock",
    "ThinkingBlock",
    "ContentBlock",
    "Message",
    "Usage",
]
