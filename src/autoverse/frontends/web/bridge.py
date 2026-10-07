"""SessionBridge — per-session block log + SSE pub/sub (docs/guides/using-webui.md §3.1 / §5).

The bridge is the seam between the (synchronous) ``WebLog`` surface calls the
sink makes and the (async) SSE subscribers. It keeps an ordered, addressable
block log so a late subscriber can be sent a full ``snapshot`` and then live
deltas. Block commands are append/update/upsert/remove keyed on a stable id;
sidebar ``state`` and ``status`` are published as their own envelope types.

Envelope types pushed to subscribers (one JSON object per SSE ``data:`` line):

* ``block``    — ``{op, id, block}``: a conversation block delta.
* ``state``    — ``{state}``: the sidebar dashboard snapshot (tokens/todos/…).
* ``status``   — ``{status}``: the live run state (running/elapsed/interaction).
* ``snapshot`` — full ``{blocks, state, status, meta}`` sent once on connect.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from typing import Any
from urllib.parse import quote


_SNAPSHOT_MAX_BLOCKS = 200
_SNAPSHOT_MAX_BYTES = 128 * 1024


class SessionBridge:
    def __init__(self, session_id: str = "", range_namespace: str = "") -> None:
        self.session_id = session_id
        self.range_namespace = range_namespace
        self.blocks: list[dict[str, Any]] = []
        self._index: dict[str, int] = {}
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self.state: dict[str, Any] = {}
        self.status: dict[str, Any] = {"running": False}
        self.meta: dict[str, Any] = {}
        self._pinned_tail_id: str | None = None
        self._external_before_cursor: str | None = None

    # ── subscriptions ─────────────────────────────────────────────────────────
    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        self._subs.discard(q)

    def has_subscribers(self) -> bool:
        """Whether a browser is currently watching this session's SSE stream."""
        return bool(self._subs)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer protection
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

    def bind_session(self, session_id: str) -> None:
        """Bind the runtime id used by resource-reference URLs."""
        self.session_id = session_id

    def set_external_before_cursor(self, cursor: str | None) -> None:
        """Attach an older Store window cursor to the next SSE snapshot."""
        self._external_before_cursor = cursor

    def _resource_url(self, block_id: str, index: int) -> str:
        sid = quote(self.session_id, safe="")
        bid = quote(block_id, safe="")
        return f"/api/sessions/{sid}/resources/{bid}/{index}"

    def _block_url(self, block_id: str) -> str:
        sid = quote(self.session_id, safe="")
        bid = quote(block_id, safe="")
        return f"/api/sessions/{sid}/blocks/{bid}"

    def _range_url(self, start: int, end: int) -> str:
        sid = quote(self.session_id, safe="")
        path = f"/api/sessions/{sid}/ranges/{start}/{end}"
        return (
            f"{path}?page={quote(self.range_namespace, safe='')}"
            if self.range_namespace
            else path
        )

    def _public_block(self, block_id: str, block: Any) -> Any:
        """Replace inline base64 images with lazy same-session resource URLs."""
        if not self.session_id or not isinstance(block, dict):
            return block
        kind = block.get("kind")
        field = (
            "attachments"
            if kind == "user"
            else "images"
            if kind == "tool_images"
            else ""
        )
        raw_items = block.get(field) if field else None
        if not isinstance(raw_items, list):
            return block
        changed = False
        items: list[Any] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict) or raw.get("source_type") == "url":
                items.append(raw)
                continue
            data = raw.get("data")
            if not isinstance(data, str) or not data:
                items.append(raw)
                continue
            item = dict(raw)
            item.pop("data", None)
            item["resource_url"] = self._resource_url(block_id, index)
            items.append(item)
            changed = True
        if not changed:
            return block
        public = dict(block)
        public[field] = items
        return public

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        block_id = str(item.get("id", ""))
        return {
            "id": block_id,
            "block": self._public_block(block_id, item.get("block")),
        }

    def _bounded_item(
        self, item: dict[str, Any], *, max_bytes: int
    ) -> tuple[dict[str, Any], int]:
        """Return a public item that cannot monopolize a bounded history page."""
        public = self._public_item(item)
        encoded = json.dumps(
            public, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) <= max_bytes:
            return public, len(encoded)
        block_id = str(public.get("id", ""))
        raw_block = public.get("block")
        kind = str(raw_block.get("kind", "block")) if isinstance(raw_block, dict) else "block"
        summary_source = ""
        if isinstance(raw_block, dict):
            for field in ("text", "md", "body_html", "html"):
                value = raw_block.get(field)
                if isinstance(value, str) and value:
                    summary_source = value
                    break
        summary = " ".join(summary_source.split())[:320]
        bounded = {
            "id": block_id,
            "block": {
                "kind": "oversize",
                "original_kind": kind,
                "summary": summary or f"Large {kind} block",
                "byte_size": len(encoded),
                "detail_url": self._block_url(block_id),
            },
        }
        size = len(
            json.dumps(
                bounded, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        return bounded, size

    @staticmethod
    def _encoded_size(item: dict[str, Any]) -> int:
        return len(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )

    @staticmethod
    def _is_terminal_response(block: Any) -> bool:
        if not isinstance(block, dict):
            return False
        kind = block.get("kind")
        if kind in {"markdown", "plan", "approval", "ask_question"}:
            return True
        return kind == "line" and "error" in str(block.get("cls", "")).split()

    def _deferred_turn_items(
        self, start: int, end: int, *, max_bytes: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Keep conversation anchors visible and defer only execution detail."""
        if start >= end:
            return [], 0

        response_index: int | None = None
        footer_index: int | None = None
        for index in range(end - 1, start, -1):
            block = self.blocks[index].get("block")
            if footer_index is None and isinstance(block, dict):
                if block.get("kind") == "turn_meta":
                    footer_index = index
            if response_index is None and self._is_terminal_response(block):
                response_index = index
            if response_index is not None and footer_index is not None:
                break

        visible_indices = {start}
        if response_index is not None:
            visible_indices.add(response_index)
        if footer_index is not None:
            visible_indices.add(footer_index)
        has_deferred_detail = any(
            index not in visible_indices for index in range(start + 1, end)
        )

        items: list[dict[str, Any]] = []
        first, _ = self._bounded_item(self.blocks[start], max_bytes=max_bytes)
        items.append(first)
        if has_deferred_detail:
            items.append(
                {
                    "id": f"execution:{start}:{end}",
                    "block": {
                        "kind": "deferred_execution",
                        "detail_url": self._range_url(start + 1, end),
                    },
                }
            )
        for index in sorted(visible_indices - {start}):
            item, _ = self._bounded_item(self.blocks[index], max_bytes=max_bytes)
            items.append(item)
        return items, sum(self._encoded_size(item) for item in items)

    def _shrink_visible_items(
        self, selected: list[dict[str, Any]], *, max_bytes: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Bound pathological visible anchors without hiding an entire turn."""
        total = sum(self._encoded_size(item) for item in selected)
        while total > max_bytes:
            candidates: list[tuple[bool, int, int]] = []
            for index, item in enumerate(selected):
                block = item.get("block")
                if not isinstance(block, dict) or block.get("kind") in {
                    "oversize",
                    "deferred_execution",
                }:
                    continue
                source_index = self._index.get(str(item.get("id", "")))
                if source_index is None:
                    continue
                candidates.append(
                    (
                        block.get("kind") == "user",
                        -self._encoded_size(item),
                        index,
                    )
                )
            if not candidates:
                break
            _, _, selected_index = min(candidates)
            source_index = self._index[str(selected[selected_index]["id"])]
            selected[selected_index], _ = self._bounded_item(
                self.blocks[source_index], max_bytes=0
            )
            total = sum(self._encoded_size(item) for item in selected)
        return selected, total

    # ── block commands (called by WebLog.emit + the session) ──────────────────
    def apply_block(self, cmd: dict[str, Any]) -> None:
        op = cmd.get("op", "append")
        bid = str(cmd.get("id", ""))
        block = cmd.get("block")
        if op == "append":
            tail_idx = self._index.get(self._pinned_tail_id or "")
            if tail_idx is None or bid == self._pinned_tail_id:
                self._index[bid] = len(self.blocks)
                self.blocks.append({"id": bid, "block": block})
            else:
                self.blocks.insert(tail_idx, {"id": bid, "block": block})
                self._index = {b["id"]: i for i, b in enumerate(self.blocks)}
        elif op in ("update", "upsert"):
            idx = self._index.get(bid)
            if idx is None:
                tail_idx = self._index.get(self._pinned_tail_id or "")
                if tail_idx is None or bid == self._pinned_tail_id:
                    self._index[bid] = len(self.blocks)
                    self.blocks.append({"id": bid, "block": block})
                else:
                    self.blocks.insert(tail_idx, {"id": bid, "block": block})
                    self._index = {b["id"]: i for i, b in enumerate(self.blocks)}
                op = "append"
            else:
                self.blocks[idx] = {"id": bid, "block": block}
                op = "update"
        elif op == "remove":
            idx = self._index.pop(bid, None)
            if idx is not None:
                self.blocks.pop(idx)
                # Rebuild the index after a removal (positions shift).
                self._index = {b["id"]: i for i, b in enumerate(self.blocks)}
        self._broadcast(
            {
                "type": "block",
                "op": op,
                "id": bid,
                "block": self._public_block(bid, block),
            }
        )

    def emit(self, cmd: dict[str, Any]) -> None:
        """The callback handed to :class:`WebLog` (a plain block command)."""
        self.apply_block(cmd)

    def append_block(self, block: dict[str, Any], block_id: str) -> None:
        self.apply_block({"op": "append", "id": block_id, "block": block})

    def remove_block(self, block_id: str) -> None:
        self.apply_block({"op": "remove", "id": block_id, "block": None})

    def pin_tail(self, block_id: str) -> None:
        """Keep subsequent appended blocks immediately before *block_id*."""
        self._pinned_tail_id = block_id

    def release_tail(self, block_id: str) -> None:
        if self._pinned_tail_id == block_id:
            self._pinned_tail_id = None

    # ── dashboard / status ─────────────────────────────────────────────────────
    def publish_state(self, state: dict[str, Any]) -> None:
        self.state = state
        self._broadcast({"type": "state", "state": state})

    def publish_status(self, status: dict[str, Any]) -> None:
        self.status = status
        self._broadcast({"type": "status", "status": status})

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def reset_blocks(self) -> None:
        self.blocks = []
        self._index = {}
        self._pinned_tail_id = None
        self._broadcast({"type": "reset"})

    def _page(
        self,
        end: int,
        *,
        max_blocks: int = _SNAPSHOT_MAX_BLOCKS,
        max_bytes: int = _SNAPSHOT_MAX_BYTES,
        minimum_turns: int = 1,
    ) -> tuple[list[dict[str, Any]], int]:
        page_end = max(0, min(end, len(self.blocks)))
        user_starts = [
            i
            for i in range(page_end)
            if isinstance(self.blocks[i].get("block"), dict)
            and self.blocks[i]["block"].get("kind") == "user"
        ]
        if user_starts:
            boundaries = [*user_starts, page_end]
            group_count = len(user_starts)
            first_group = max(0, group_count - minimum_turns)
            selected: list[dict[str, Any]] = []
            total = 0

            def group_items(group: int) -> tuple[list[dict[str, Any]], int]:
                items: list[dict[str, Any]] = []
                size_total = 0
                for index in range(boundaries[group], boundaries[group + 1]):
                    item, item_size = self._bounded_item(
                        self.blocks[index], max_bytes=max_bytes
                    )
                    items.append(item)
                    size_total += item_size
                return items, size_total

            # Keep each mandatory turn's question and terminal response visible.
            # Only its intermediate execution detail becomes an expansion handle.
            for group in range(first_group, group_count):
                items, group_size = group_items(group)
                if (
                    len(selected) + len(items) <= max_blocks
                    and total + group_size <= max_bytes
                ):
                    selected.extend(items)
                    total += group_size
                    continue
                start_index, end_index = boundaries[group], boundaries[group + 1]
                deferred, deferred_size = self._deferred_turn_items(
                    start_index, end_index, max_bytes=max_bytes
                )
                selected.extend(deferred)
                total += deferred_size

            if len(selected) > max_blocks or total > max_bytes:
                selected, total = self._shrink_visible_items(
                    selected, max_bytes=max_bytes
                )

            # Fill older whole turns only while the complete group fits.
            for group in range(first_group - 1, -1, -1):
                items, group_size = group_items(group)
                if (
                    len(selected) + len(items) > max_blocks
                    or total + group_size > max_bytes
                ):
                    break
                selected = items + selected
                total += group_size
                first_group = group
            return selected, boundaries[first_group]

        # Logs without user boundaries (startup/status output) retain the simple
        # bounded block paging behavior.
        public: list[dict[str, Any]] = []
        total = 0
        start = page_end
        for index in range(page_end - 1, -1, -1):
            item, size = self._bounded_item(self.blocks[index], max_bytes=max_bytes)
            if public and (len(public) >= max_blocks or total + size > max_bytes):
                break
            public.append(item)
            total += size
            start = index
        public.reverse()
        return public, start

    def history_page(self, before: str | None) -> dict[str, Any]:
        """Return the bounded page immediately before an opaque snapshot cursor."""
        try:
            end = int(before or "0")
        except ValueError:
            end = 0
        blocks, start = self._page(end, minimum_turns=1)
        return {
            "blocks": blocks,
            "has_more": start > 0,
            "before_cursor": str(start) if start > 0 else None,
        }

    def resource(self, block_id: str, index: int) -> tuple[bytes, str]:
        """Resolve one lazy image resource from the authoritative private block."""
        position = self._index.get(block_id)
        if position is None:
            raise KeyError(block_id)
        block = self.blocks[position].get("block")
        if not isinstance(block, dict):
            raise KeyError(block_id)
        kind = block.get("kind")
        field = (
            "attachments"
            if kind == "user"
            else "images"
            if kind == "tool_images"
            else ""
        )
        items = block.get(field) if field else None
        if not isinstance(items, list) or index < 0 or index >= len(items):
            raise KeyError(block_id)
        item = items[index]
        if not isinstance(item, dict) or item.get("source_type") == "url":
            raise KeyError(block_id)
        raw = item.get("data")
        if not isinstance(raw, str):
            raise KeyError(block_id)
        try:
            payload = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyError(block_id) from exc
        media_type = str(item.get("media_type") or "image/png")
        if not media_type.startswith("image/"):
            media_type = "image/png"
        return payload, media_type

    def block_detail(self, block_id: str) -> dict[str, Any]:
        """Return one explicitly requested full public block by stable id."""
        position = self._index.get(block_id)
        if position is None:
            raise KeyError(block_id)
        return self._public_item(self.blocks[position])

    def range_detail(self, start: int, end: int) -> dict[str, Any]:
        """Return an explicitly expanded block range from one oversized turn."""
        safe_start = max(0, min(start, len(self.blocks)))
        safe_end = max(safe_start, min(end, len(self.blocks)))
        return {
            "blocks": [
                self._public_item(item) for item in self.blocks[safe_start:safe_end]
            ]
        }

    def snapshot(self) -> dict[str, Any]:
        blocks, start = self._page(len(self.blocks), minimum_turns=2)
        cursor = (
            self._external_before_cursor
            if self._external_before_cursor is not None
            else str(start) if start > 0 else None
        )
        return {
            "type": "snapshot",
            "server_now_ms": time.time() * 1000.0,
            "blocks": blocks,
            "has_more": cursor is not None,
            "before_cursor": cursor,
            "state": self.state,
            "status": self.status,
            "meta": self.meta,
        }


__all__ = ["SessionBridge"]
