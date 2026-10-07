"""Unit tests — message serialize round-trip edge cases (docs/reference/events-and-storage.md §5.2)."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.persist.serialize import (
    FileBlobStore,
    INLINE_MAX,
    block_from_dict,
    block_to_dict,
    msg_from_dict,
    msg_to_dict,
)
from autoverse.types.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def test_each_block_type_round_trips() -> None:
    blocks = [
        TextBlock(text="hi"),
        ThinkingBlock(text="hmm", signature="s"),
        ThinkingBlock(text="no sig", signature=None),
        ToolUseBlock(id="t1", name="bash", input={"command": "ls", "n": 3}),
        ToolResultBlock(tool_use_id="t1", content="out", is_error=True, metadata={"k": 1}),
        ImageBlock(media_type="image/png", data="QUJD", source_type="base64"),
    ]
    for b in blocks:
        assert block_from_dict(block_to_dict(b)) == b


def test_message_round_trip_preserves_order() -> None:
    msg = Message(
        role="assistant",
        content=(
            ThinkingBlock(text="t"),
            TextBlock(text="a"),
            ToolUseBlock(id="x", name="n", input={}),
        ),
        metadata={"message_id": "m1"},
    )
    assert msg_from_dict(msg_to_dict(msg)) == msg


def test_large_tool_result_offloaded_to_blob(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path)
    big = "x" * (INLINE_MAX + 100)
    block = ToolResultBlock(tool_use_id="t", content=big)
    d = block_to_dict(block, blobs=blobs)
    # Content is a blob reference, not the inline string.
    assert isinstance(d["content"], dict) and "_blob" in d["content"]
    # Dereferences back to the exact original content.
    assert block_from_dict(d, blobs=blobs) == block


def test_small_tool_result_stays_inline(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path)
    block = ToolResultBlock(tool_use_id="t", content="small")
    d = block_to_dict(block, blobs=blobs)
    assert d["content"] == "small"


def test_large_image_offloaded(tmp_path: Path) -> None:
    blobs = FileBlobStore(tmp_path)
    big = "A" * (INLINE_MAX + 50)
    block = ImageBlock(media_type="image/png", data=big)
    d = block_to_dict(block, blobs=blobs)
    assert isinstance(d["data"], dict) and "_blob" in d["data"]
    assert block_from_dict(d, blobs=blobs) == block


def test_blobstore_get_verifies_sha256(tmp_path: Path) -> None:
    """Content-addressed get must verify integrity, not trust the filename."""
    import pytest

    from autoverse.infra.persist.serialize import BlobError

    blobs = FileBlobStore(tmp_path)
    big = b"y" * (INLINE_MAX + 10)
    sha = blobs.put(big, ext="txt")
    # Corrupt the on-disk blob while keeping its sha-named path.
    (tmp_path / "blobs" / f"{sha}.txt").write_bytes(b"tampered")
    with pytest.raises(BlobError):
        blobs.get(sha, ext="txt")


def test_blobstore_get_missing_raises(tmp_path: Path) -> None:
    import pytest

    from autoverse.infra.persist.serialize import BlobError

    blobs = FileBlobStore(tmp_path)
    with pytest.raises(BlobError):
        blobs.get("0" * 64, ext="txt")


def test_blob_ref_without_store_is_not_silently_empty(tmp_path: Path) -> None:
    """A tool_result/image dict that references a blob must NOT decode to "" when
    no blob store is available — that silently fabricates a lossy load."""
    import pytest

    from autoverse.infra.persist.serialize import BlobError

    blobs = FileBlobStore(tmp_path)
    big = "z" * (INLINE_MAX + 10)
    d = block_to_dict(ToolResultBlock(tool_use_id="t", content=big), blobs=blobs)
    with pytest.raises(BlobError):
        block_from_dict(d, blobs=None)


def test_missing_blob_detected_on_load(tmp_path: Path) -> None:
    """A deleted blob surfaces as a skipped line in LoadReport (P5: corruption
    is made explicit, never silently swallowed)."""
    import asyncio

    from autoverse.infra.persist.jsonl_store import JSONLStore

    store = JSONLStore(tmp_path)
    sid = "blobloss"
    big = "q" * (INLINE_MAX + 10)
    msg = Message(role="user", content=(ToolResultBlock(tool_use_id="t", content=big),))
    asyncio.run(store.append(sid, msg))
    # Delete the offloaded blob, simulating a GC bug / partial copy.
    for p in (tmp_path / "blobs").glob("*.txt"):
        p.unlink()
    _msgs, report = asyncio.run(store.load_with_report(sid))
    assert report.skipped >= 1
