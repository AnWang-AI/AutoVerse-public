"""Unit tests — JSONLStore append/rewrite cross-process flock (docs/reference/events-and-storage.md §9.2).

``rewrite`` swaps the inode under an exclusive ``<sid>.lock`` flock; ``append``
must take the same lock so it cannot write into the old (already-replaced) inode
mid-rewrite. We assert the lock protocol directly via ``fcntl`` semantics.
"""

from __future__ import annotations

import asyncio
import fcntl
import threading
import time
from pathlib import Path

from autoverse.infra.persist.jsonl_store import JSONLStore
from autoverse.types.messages import Message, TextBlock


def _msg(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))


def test_append_blocks_while_rewrite_lock_held(tmp_path: Path) -> None:
    """A foreign holder of the exclusive ``<sid>.lock`` must stall ``append``.

    This proves ``append`` acquires the cross-process lock (otherwise it would
    finish immediately and race the rewriter's ``os.replace``)."""
    sid = "concurrent"
    store = JSONLStore(tmp_path)
    asyncio.run(store.create(sid, _meta(sid)))

    lock_path = tmp_path / f"{sid}.lock"
    holder = lock_path.open("w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    done = threading.Event()

    def _do_append() -> None:
        asyncio.run(store.append(sid, _msg("during-rewrite")))
        done.set()

    t = threading.Thread(target=_do_append, daemon=True)
    t.start()

    # While the exclusive lock is held, append must not complete.
    assert not done.wait(timeout=0.5)

    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()

    # Once released, append proceeds and persists the message.
    assert done.wait(timeout=5.0)
    t.join(timeout=5.0)
    msgs = asyncio.run(store.load(sid))
    assert any(
        isinstance(b, TextBlock) and b.text == "during-rewrite"
        for m in msgs
        for b in m.content
    )


def test_append_then_rewrite_no_data_loss(tmp_path: Path) -> None:
    """Sequential append + rewrite keep all surviving messages."""
    sid = "seq"
    store = JSONLStore(tmp_path)
    asyncio.run(store.create(sid, _meta(sid)))
    asyncio.run(store.append(sid, _msg("a"), _msg("b")))
    asyncio.run(store.rewrite(sid, [_msg("a"), _msg("b"), _msg("c")], reason="compact"))
    asyncio.run(store.append(sid, _msg("d")))
    msgs = asyncio.run(store.load(sid))
    texts = [b.text for m in msgs for b in m.content if isinstance(b, TextBlock)]
    assert texts == ["a", "b", "c", "d"]


def _meta(sid: str):  # type: ignore[no-untyped-def]
    from autoverse.ports import SessionMeta

    return SessionMeta(session_id=sid)


# silence unused import warning when run standalone
_ = time


def test_update_meta_no_data_loss_under_concurrent_append(tmp_path: Path) -> None:
    """``update_meta`` must not lose a concurrent ``append``'s message.

    The read (meta+msgs) and rewrite happen inside the same ``LOCK_EX`` flock,
    so an append from another thread/process cannot land between our read and
    our rewrite — which would be silently lost by ``os.replace``."""
    sid = "race"
    store = JSONLStore(tmp_path)
    asyncio.run(store.create(sid, _meta(sid)))
    asyncio.run(store.append(sid, _msg("original")))

    append_done = threading.Event()

    def _do_append() -> None:
        time.sleep(0.1)
        asyncio.run(store.append(sid, _msg("concurrent")))
        append_done.set()

    t = threading.Thread(target=_do_append, daemon=True)
    t.start()

    asyncio.run(store.update_meta(sid, title="updated"))

    assert append_done.wait(timeout=5.0)
    t.join(timeout=5.0)

    msgs = asyncio.run(store.load(sid))
    texts = [b.text for m in msgs for b in m.content if isinstance(b, TextBlock)]
    assert "original" in texts
    assert "concurrent" in texts

    meta = asyncio.run(store.load_meta(sid))
    assert meta.title == "updated"
