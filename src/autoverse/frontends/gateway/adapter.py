"""PlatformAdapter Protocol + inbound/outbound translation (docs/reference/extensions.md).

This module defines:

* :class:`PlatformAdapter` — the structural contract a platform implements
  (one adapter == one bot on one platform). It only does protocol translation
  and connection; it knows nothing about routing or the engine (P3).
* :class:`EngineInput` — the normalized engine input produced by
  :func:`normalize_inbound` (text prompt + image attachments, G1).
* :class:`OutboundRenderer` — turns a stream of :class:`StreamEvent` into
  :class:`OutboundMessage` objects, applying throttle/merge for non-streaming
  platforms (G2) and tool-card downgrade to a text summary for text-only
  platforms without losing information (G3).
* a tiny :class:`PlatformRegistry` so new platforms register without the runner
  changing (G7/P8).

``OutboundMessage``/``InboundMessage``/``PlatformCapabilities`` live in
``types.py`` and are re-exported here for convenience.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import (
    AsyncContextManager,
    Awaitable,
    Callable,
    Protocol,
    runtime_checkable,
)

from autoverse.types import ImageBlock, StreamEvent
from autoverse.types.events import (
    ApprovalPayload,
    ErrorPayload,
    EventType,
    TextDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
)

from autoverse.frontends.gateway.throttle import DeltaThrottle
from autoverse.frontends.gateway.types import (
    InboundAttachment,
    InboundMessage,
    OutboundMessage,
    PlatformCapabilities,
)

# Runner-injected inbound callback; the adapter reports messages through it and
# never references the Runner directly (decoupling, P3).
OnInbound = Callable[[InboundMessage], Awaitable[None]]


@dataclass(frozen=True)
class EngineInput:
    """Normalized engine input — the product of :func:`normalize_inbound` (G1).

    ``prompt`` is the (possibly note-augmented) text; ``attachments`` are the
    image blocks the engine accepts via ``engine.run(prompt, attachments=...)``.
    """

    prompt: str
    attachments: tuple[ImageBlock, ...] = ()


@runtime_checkable
class PlatformAdapter(Protocol):
    """Structural contract for a platform bot.

    Implemented by each platform (Discord/Slack/...); tests use a fake. Uses a
    Protocol (not ABC) so fakes need not inherit and the adapter never depends
    back on the gateway base (purest P3).
    """

    name: str  # platform id, e.g. "discord"
    bot_id: str  # distinguishes multiple bots on one platform
    capabilities: PlatformCapabilities  # explicit capability declaration (P4)

    def bind(self, on_inbound: OnInbound) -> None:
        """Runner injects the inbound callback before ``connect()``."""

    async def connect(self) -> None:
        """Connect and start listening (may block until disconnected)."""

    async def disconnect(self) -> None:
        """Disconnect and release resources; must be idempotent."""

    async def send(self, msg: OutboundMessage) -> str:
        """Send one outbound message; return the platform message_id."""

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """Edit an existing message in place (streaming_edit platforms only)."""

    def typing(self, chat_id: str) -> AsyncContextManager[None]:
        """Typing-indicator context; no-op for platforms without it."""


# ── Inbound normalization (G1) ────────────────────────────────────────────────


def normalize_inbound(
    msg: InboundMessage, *, vision: bool = True
) -> EngineInput:
    """Map a platform :class:`InboundMessage` to :class:`EngineInput` (G1).

    Image attachments become :class:`ImageBlock` when the model supports vision;
    otherwise they are degraded to a text note so information is never silently
    dropped (P5). Inbound rich text defaults to plain text (least ambiguous for
    the model).
    """
    images: list[ImageBlock] = []
    notes: list[str] = []
    for att in msg.attachments:
        if att.kind != "image":
            notes.append(
                f"[user sent a {att.kind} attachment '{att.filename or att.url}'"
                " which is not yet supported]"
            )
            continue
        if not vision:
            notes.append(
                f"[user sent an image, but the current model has no vision:"
                f" {att.filename or att.url}]"
            )
            continue
        data = _attachment_b64(att)
        if data is None:
            notes.append(
                f"[user sent an image that could not be loaded:"
                f" {att.filename or att.url}]"
            )
            continue
        images.append(
            ImageBlock(
                media_type=att.mime or "image/png",
                data=data,
                source_type="base64",
            )
        )
    prompt = msg.text
    if notes:
        prompt = (prompt + "\n" + "\n".join(notes)) if prompt else "\n".join(notes)
    return EngineInput(prompt=prompt, attachments=tuple(images))


def _attachment_b64(att: InboundAttachment) -> str | None:
    """Best-effort base64 for an inbound attachment without network I/O.

    Real adapters download ``url`` and supply ``data``; here we accept either an
    inline ``data`` payload or a ``data:`` / ``base64,`` URL. Unknown remote
    URLs return ``None`` (caller degrades to a text note).
    """
    if att.data:
        return att.data
    url = att.url
    if url.startswith("data:") and "base64," in url:
        return url.split("base64,", 1)[1]
    if url.startswith("base64,"):
        return url.split("base64,", 1)[1]
    # A bare local path or remote URL: adapters supply ``data`` for those;
    # without it (and without network here) we cannot inline.
    if _looks_base64(url):
        return url
    return None


def _looks_base64(s: str) -> bool:
    if not s or len(s) % 4 != 0 or "/" in s or "." in s:
        return False
    try:
        base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


# ── Outbound rendering (G2 throttle/merge, G3 tool-card downgrade) ─────────────


@dataclass
class OutboundRenderer:
    """Render a :class:`StreamEvent` stream to :class:`OutboundMessage`s.

    Applies throttle/merge of text deltas (G2) and downgrades rich tool cards to
    a text summary on text-only platforms (G3). The renderer is pure: it returns
    messages to send and never touches an adapter directly, so the Runner stays
    in control of transport (P2/P4).

    Usage::

        r = OutboundRenderer(caps)
        for ev in events:
            for out in r.render(ev):
                await adapter.send(out)  # or .edit if out.edit_message_id set
    """

    capabilities: PlatformCapabilities
    chat_id: str = ""
    reply_to: str = ""
    _throttle: DeltaThrottle = field(init=False, repr=False)
    _images: list[str] = field(default_factory=list, init=False, repr=False)
    _edit_msg_id: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self._throttle = DeltaThrottle(
            merge_all=not self.capabilities.streaming_edit
        )

    def attach_edit_target(self, message_id: str) -> None:
        """Record the message id the renderer edits in place (streaming)."""
        self._edit_msg_id = message_id

    def render(self, ev: StreamEvent) -> list[OutboundMessage]:
        """Translate one event into zero or more outbound messages."""
        t = ev.type
        if t is EventType.TEXT_DELTA:
            return self._render_text_delta(ev.payload)  # type: ignore[arg-type]
        if t is EventType.TOOL_START:
            return self._render_tool_start(ev.payload)  # type: ignore[arg-type]
        if t is EventType.TOOL_END:
            return self._render_tool_end(ev.payload)  # type: ignore[arg-type]
        if t is EventType.ERROR:
            return self._render_error(ev.payload)  # type: ignore[arg-type]
        if t is EventType.TURN_END:
            return self._final_flush()
        # APPROVAL is rendered by the approval/runner path; THINKING_DELTA,
        # COMPACTING, USAGE, etc. are not surfaced to IM (avoid noise).
        return []

    def render_approval(self, payload: ApprovalPayload) -> OutboundMessage:
        """Render an approval request: button card or reply-prompt text (§6)."""
        prompt = (
            f"Approve `{payload.tool_name}`? {payload.summary}".rstrip()
        )
        if self.capabilities.interactive_buttons:
            block = {
                "type": "approval",
                "call_id": payload.call_id,
                "tool_name": payload.tool_name,
                "summary": payload.summary,
                "actions": ["allow", "deny"],
            }
            return OutboundMessage(
                chat_id=self.chat_id,
                text=prompt,
                reply_to_id=self.reply_to,
                blocks=(block,),
            )
        text = f"\u26a0\ufe0f {prompt}\nReply `yes`/`no` to decide."
        return OutboundMessage(
            chat_id=self.chat_id, text=text, reply_to_id=self.reply_to
        )

    # ── per-event helpers ─────────────────────────────────────────────────────
    def _render_text_delta(self, p: TextDeltaPayload) -> list[OutboundMessage]:
        chunk = self._throttle.add(p.text)
        if chunk is None:
            return []
        return [self._text_msg(chunk, streaming=True)]

    def _render_tool_start(self, p: ToolStartPayload) -> list[OutboundMessage]:
        out = self._flush_before_card()
        if self.capabilities.rich_blocks:
            block = {
                "type": "tool_start",
                "tool_use_id": p.tool_use_id,
                "tool_name": p.tool_name,
                "input": p.tool_input,
            }
            out.append(
                OutboundMessage(
                    chat_id=self.chat_id, reply_to_id=self.reply_to, blocks=(block,)
                )
            )
        else:
            out.append(self._text_msg(f"\U0001f527 running {p.tool_name}\u2026"))
        return out

    def _render_tool_end(self, p: ToolEndPayload) -> list[OutboundMessage]:
        out = self._flush_before_card()
        # Tool-produced images (generate_image, docs/reference/tools.md §11.7) are queued for the
        # final flush so platforms with image support render them inline; those
        # without degrade to a text path note (P5, see _final_flush).
        if not p.is_error:
            path = p.metadata.get("path")
            if p.tool_name == "generate_image" and isinstance(path, str) and path:
                self.add_image(path)
        if self.capabilities.rich_blocks:
            block = {
                "type": "tool_end",
                "tool_use_id": p.tool_use_id,
                "tool_name": p.tool_name,
                "output": p.output,
                "is_error": p.is_error,
                "error_kind": p.error_kind,
                "error_message": p.error_message,
                "duration_ms": p.duration_ms,
                "metadata": p.metadata,
            }
            out.append(
                OutboundMessage(
                    chat_id=self.chat_id, reply_to_id=self.reply_to, blocks=(block,)
                )
            )
        else:
            out.append(self._text_msg(downgrade_tool_end(p)))
        return out

    def _render_error(self, p: ErrorPayload) -> list[OutboundMessage]:
        out = self._final_flush()
        out.append(self._text_msg(_redact(p.message)))
        return out

    def _final_flush(self) -> list[OutboundMessage]:
        out: list[OutboundMessage] = []
        chunk = self._throttle.flush_final()
        if chunk:
            out.append(self._text_msg(chunk, streaming=self.capabilities.streaming_edit))
        if self._images:
            paths = tuple(self._images)
            self._images.clear()
            if self.capabilities.images:
                out.append(
                    OutboundMessage(
                        chat_id=self.chat_id,
                        reply_to_id=self.reply_to,
                        image_paths=paths,
                    )
                )
            else:
                # Never silently drop: degrade to a text note (P5).
                names = ", ".join(paths)
                out.append(self._text_msg(f"[image output: {names}]"))
        return out

    def _flush_before_card(self) -> list[OutboundMessage]:
        """Flush buffered streaming text before emitting a tool card so text and
        cards keep their relative order on streaming platforms."""
        chunk = self._throttle.flush_final()
        if chunk:
            return [self._text_msg(chunk, streaming=True)]
        return []

    def _text_msg(self, text: str, *, streaming: bool = False) -> OutboundMessage:
        edit_id = self._edit_msg_id if (streaming and self._edit_msg_id) else ""
        return OutboundMessage(
            chat_id=self.chat_id,
            text=text,
            reply_to_id=self.reply_to,
            edit_message_id=edit_id,
        )

    def add_image(self, path: str) -> None:
        """Collect a tool-produced image path for the final flush."""
        self._images.append(path)


def downgrade_tool_end(p: ToolEndPayload) -> str:
    """Downgrade a rich TOOL_END payload to a one-line text summary (G3).

    Preserves key information (name, status, duration, error, and a compact view
    of metadata) so text-only platforms lose no signal (P5).
    """
    status = "\u274c" if p.is_error else "\u2705"
    parts = [f"{status} {p.tool_name}"]
    if p.duration_ms:
        parts.append(f"({p.duration_ms:.0f}ms)")
    if p.is_error:
        detail = p.error_message or p.error_kind or "error"
        parts.append(f"\u2014 {detail}")
    elif p.output:
        parts.append(f"\u2014 {_truncate(p.output, 200)}")
    if p.metadata:
        parts.append(f"[{_compact_meta(p.metadata)}]")
    return " ".join(parts)


def _compact_meta(meta: dict[str, object]) -> str:
    items = []
    for k, v in meta.items():
        try:
            sval = v if isinstance(v, str) else json.dumps(v, default=str)
        except (TypeError, ValueError):
            sval = str(v)
        items.append(f"{k}={_truncate(sval, 60)}")
    return ", ".join(items)


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _redact(message: str) -> str:
    """Strip internal absolute paths / stack-trace noise from user-facing errors."""
    first = message.strip().splitlines()[0] if message.strip() else message
    return _truncate(first, 300)


# ── Platform registry (G7 / P8) ───────────────────────────────────────────────

PlatformFactory = Callable[..., PlatformAdapter]


class PlatformRegistry:
    """Maps a platform name to an adapter factory. New platform = register here;
    the Runner never changes (G7/P8)."""

    def __init__(self) -> None:
        self._factories: dict[str, PlatformFactory] = {}

    def register(self, name: str, factory: PlatformFactory) -> None:
        if name in self._factories:
            raise ValueError(f"platform '{name}' already registered")
        self._factories[name] = factory

    def platform(self, name: str) -> Callable[[PlatformFactory], PlatformFactory]:
        """Decorator form: ``@registry.platform("slack")``."""

        def deco(factory: PlatformFactory) -> PlatformFactory:
            self.register(name, factory)
            return factory

        return deco

    def create(self, name: str, *args: object, **kwargs: object) -> PlatformAdapter:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise KeyError(f"unknown platform '{name}'") from exc
        return factory(*args, **kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(self._factories)


__all__ = [
    "PlatformAdapter",
    "OnInbound",
    "EngineInput",
    "OutboundMessage",
    "OutboundRenderer",
    "PlatformCapabilities",
    "PlatformRegistry",
    "PlatformFactory",
    "normalize_inbound",
    "downgrade_tool_end",
]
