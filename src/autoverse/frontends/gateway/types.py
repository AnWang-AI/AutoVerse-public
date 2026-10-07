"""Gateway shared data types (docs/reference/extensions.md).

Platform-agnostic value types used across the gateway. ``InboundMessage`` is the
unified product of every :class:`~autoverse.frontends.gateway.adapter.PlatformAdapter`;
``OutboundMessage`` is what the renderer produces and an adapter translates to a
native send. These types depend on nothing internal beyond ``types/`` so the
gateway stays a pure frontend adapter (P3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class InboundAttachment:
    """Normalized representation of an inbound attachment."""

    kind: Literal["image", "file", "audio"]
    url: str  # platform-side downloadable URL (or local cache path)
    mime: str = ""
    filename: str = ""
    data: str = ""  # optional inline base64 payload (test/echo platforms)


@dataclass(frozen=True)
class InboundMessage:
    """Platform-agnostic inbound message — every Adapter's unified product.

    Runner/Router never see which platform a message came from; they only read
    these fields (P2/P3).
    """

    platform: str
    bot_id: str
    chat_id: str
    user_id: str
    text: str
    thread_id: str = ""
    is_dm: bool = False
    attachments: tuple[InboundAttachment, ...] = ()
    raw: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class PlatformCapabilities:
    """Adapter declares its own capabilities so the renderer can degrade (P5/P8)."""

    streaming_edit: bool = False  # can edit a message in place to fake "streaming"
    rich_blocks: bool = False  # can send structured cards (tool cards stay rich)
    interactive_buttons: bool = False  # can send approval buttons
    images: bool = False  # can attach images
    max_text_len: int = 2000  # single-message text cap
    rate_limit_per_sec: float = 1.0  # soft outbound rate cap


@dataclass(frozen=True)
class OutboundMessage:
    """Normalized outbound message — renderer produces, Adapter sends natively."""

    chat_id: str
    text: str = ""
    reply_to_id: str = ""  # thread / quoted reply
    image_paths: tuple[str, ...] = ()  # local image paths
    blocks: tuple[dict[str, Any], ...] = ()  # structured cards (rich_blocks)
    edit_message_id: str = ""  # non-empty = edit an existing message in place


__all__ = [
    "InboundAttachment",
    "InboundMessage",
    "PlatformCapabilities",
    "OutboundMessage",
]
