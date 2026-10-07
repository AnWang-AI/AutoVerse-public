"""Storage-only message <-> dict codec (docs/reference/events-and-storage.md §5.2).

A pure structural mapping between internal :class:`Message`/:class:`ContentBlock`
values and JSON-serialisable dicts. Each block carries a ``type`` discriminator
so the round-trip is lossless and order-preserving (D1). This is *not* the LLM
wire codec (that lives in ``infra/llm/codec``); here there is no backend dialect.

Large ``tool_result`` text and large images may be offloaded to a content-
addressed blob store (docs/reference/events-and-storage.md §5.2/§5.2a); the helpers accept an optional
``BlobStore`` to externalise oversized payloads while keeping the rebuilt block
identical to the contract type.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from autoverse.types.messages import (
    ContentBlock,
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

#: Inline ceiling for text/image payloads; larger blobs are offloaded (§5.2a).
INLINE_MAX = 16 * 1024


class BlobError(ValueError):
    """A content-addressed blob is missing, unreadable, or fails integrity check.

    Subclasses ``ValueError`` so that ``JSONLStore._load_inner`` (which catches
    ``(KeyError, TypeError, ValueError)``) records the affected line in its
    ``LoadReport`` rather than silently fabricating empty content (P5: storage
    corruption is made explicit, never swallowed — docs/reference/events-and-storage.md §5.2a/§10.1).
    """


class BlobStore(Protocol):
    """Content-addressed blob sink used to offload oversized payloads."""

    def put(self, data: bytes, *, ext: str) -> str:
        """Persist *data* if absent; return its sha256 hex digest."""

    def get(self, sha: str, *, ext: str) -> bytes:
        """Read the blob bytes back by digest; verify content addressing."""


class FileBlobStore:
    """Default on-disk blob store: ``<base>/blobs/<sha256>.<ext>`` (§5.2a)."""

    def __init__(self, base_dir: Path) -> None:
        self._dir = Path(base_dir) / "blobs"

    def put(self, data: bytes, *, ext: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self._dir / f"{sha}.{ext}"
        if not target.exists():
            tmp = self._dir / f".{sha}.{ext}.tmp"
            tmp.write_bytes(data)
            tmp.replace(target)
        target.chmod(0o600)
        return sha

    def get(self, sha: str, *, ext: str) -> bytes:
        target = self._dir / f"{sha}.{ext}"
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise BlobError(f"blob {sha}.{ext} missing or unreadable") from exc
        # Content addressing: the bytes must hash back to the requested digest.
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha:
            raise BlobError(
                f"blob {sha}.{ext} integrity check failed (got {actual})"
            )
        return data


def block_to_dict(b: ContentBlock, *, blobs: BlobStore | None = None) -> dict[str, object]:
    """Map a single content block to a JSON-serialisable dict (lossless)."""
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ThinkingBlock):
        return {"type": "thinking", "text": b.text, "signature": b.signature}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": b.tool_use_id,
            "content": _content_to_dict(b.content, blobs),
            "is_error": b.is_error,
            "metadata": b.metadata,
        }
    if isinstance(b, ImageBlock):
        return _image_to_dict(b, blobs)
    raise TypeError(f"Unknown content block: {type(b).__name__}")


def block_from_dict(d: dict[str, object], *, blobs: BlobStore | None = None) -> ContentBlock:
    """Reconstruct a content block from its dict form (inverse of block_to_dict)."""
    kind = d.get("type")
    if kind == "text":
        return TextBlock(text=str(d["text"]))
    if kind == "thinking":
        sig = d.get("signature")
        return ThinkingBlock(
            text=str(d["text"]),
            signature=None if sig is None else str(sig),
        )
    if kind == "tool_use":
        raw_input = d.get("input", {})
        assert isinstance(raw_input, dict)
        return ToolUseBlock(id=str(d["id"]), name=str(d["name"]), input=dict(raw_input))
    if kind == "tool_result":
        meta = d.get("metadata", {})
        assert isinstance(meta, dict)
        return ToolResultBlock(
            tool_use_id=str(d["tool_use_id"]),
            content=_content_from_dict(d.get("content", ""), blobs),
            is_error=bool(d.get("is_error", False)),
            metadata=dict(meta),
        )
    if kind == "image":
        return _image_from_dict(d, blobs)
    raise TypeError(f"Unknown content block dict: {kind!r}")


def msg_to_dict(m: Message, *, blobs: BlobStore | None = None) -> dict[str, object]:
    """Serialise a Message to a ``_kind=='msg'`` dict (docs/reference/events-and-storage.md §5.2)."""
    return {
        "_kind": "msg",
        "role": m.role,
        "content": [block_to_dict(b, blobs=blobs) for b in m.content],
        "metadata": dict(m.metadata),
    }


def msg_from_dict(d: dict[str, object], *, blobs: BlobStore | None = None) -> Message:
    """Reconstruct a Message from its dict form (lossless, order-preserving)."""
    raw_content = d.get("content", [])
    assert isinstance(raw_content, list)
    blocks = tuple(block_from_dict(b, blobs=blobs) for b in raw_content)
    meta = d.get("metadata", {})
    assert isinstance(meta, dict)
    role: Role = _coerce_role(d.get("role", "user"))
    return Message(role=role, content=blocks, metadata=dict(meta))


# ── internals ────────────────────────────────────────────────────────────────


def _coerce_role(value: object) -> Role:
    if value == "user" or value == "assistant" or value == "system":
        return value
    return "user"


def _content_to_dict(content: str, blobs: BlobStore | None) -> object:
    """Offload oversized tool_result text to a blob; otherwise keep inline."""
    data = content.encode("utf-8")
    if blobs is None or len(data) <= INLINE_MAX:
        return content
    sha = blobs.put(data, ext="txt")
    return {"_blob": sha, "bytes": len(data)}


def _content_from_dict(value: object, blobs: BlobStore | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "_blob" in value:
        sha = str(value["_blob"])
        if blobs is None:
            raise BlobError(f"blob {sha} referenced but no blob store provided")
        return blobs.get(sha, ext="txt").decode("utf-8")
    return str(value)


def _image_to_dict(b: ImageBlock, blobs: BlobStore | None) -> dict[str, object]:
    raw = b.data.encode("utf-8")
    if blobs is not None and len(raw) > INLINE_MAX:
        sha = blobs.put(raw, ext="b64")
        return {
            "type": "image",
            "media_type": b.media_type,
            "source_type": b.source_type,
            "data": {"_blob": sha, "bytes": len(raw)},
        }
    return {
        "type": "image",
        "media_type": b.media_type,
        "source_type": b.source_type,
        "data": b.data,
    }


def _image_from_dict(d: dict[str, object], blobs: BlobStore | None) -> ImageBlock:
    raw = d.get("data", "")
    if isinstance(raw, dict) and "_blob" in raw:
        sha = str(raw["_blob"])
        if blobs is None:
            raise BlobError(f"blob {sha} referenced but no blob store provided")
        data = blobs.get(sha, ext="b64").decode("utf-8")
    else:
        data = str(raw)
    source = d.get("source_type", "base64")
    source_type = source if source in ("base64", "url") else "base64"
    return ImageBlock(
        media_type=str(d["media_type"]),
        data=data,
        source_type=source_type,  # type: ignore[arg-type]
    )


__all__ = [
    "INLINE_MAX",
    "BlobError",
    "BlobStore",
    "FileBlobStore",
    "block_to_dict",
    "block_from_dict",
    "msg_to_dict",
    "msg_from_dict",
]
