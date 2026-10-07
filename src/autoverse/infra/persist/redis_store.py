"""RedisStore — Redis-backed ``Store`` adapter, equivalent to JSONLStore (D9).

The ``redis`` dependency is optional: the import is guarded so this module
imports cleanly even when redis is not installed. A clear error is raised only
on *instantiation* if the dependency (or a server) is missing
(docs/reference/events-and-storage.md).

Layout per session:
  key ``sess:<sid>:meta``  → JSON SessionMeta
  key ``sess:<sid>:msgs``  → RPUSH list of serialised Message JSON lines
"""

from __future__ import annotations

import json
from typing import Any

from autoverse.infra.persist.serialize import msg_from_dict, msg_to_dict
from autoverse.ports import SessionMeta
from autoverse.types.messages import Message

try:  # pragma: no cover - import guard, exercised by absence-of-redis path
    import redis.asyncio as _redis_asyncio

    _REDIS_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover
    _redis_asyncio = None  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR = exc


class RedisStore:
    """Store Protocol adapter backed by Redis (same contract as JSONLStore)."""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        if _redis_asyncio is None:
            raise RuntimeError(
                "RedisStore requires the 'redis' package. Install it with "
                "`pip install redis` to use the redis backend."
            ) from _REDIS_IMPORT_ERROR
        # redis-py async methods are dynamically typed (Awaitable[T] | T);
        # treat the client as Any so mypy --strict does not choke on the union.
        self._client: Any = _redis_asyncio.from_url(url, decode_responses=True)

    @staticmethod
    def _meta_key(sid: str) -> str:
        return f"sess:{sid}:meta"

    @staticmethod
    def _msgs_key(sid: str) -> str:
        return f"sess:{sid}:msgs"

    async def create(self, session_id: str, meta: SessionMeta) -> None:
        key = self._meta_key(session_id)
        if await self._client.exists(key):
            return  # idempotent
        await self._client.set(key, json.dumps(_meta_to_dict(meta)))

    async def append(self, session_id: str, *msgs: Message) -> None:
        if not msgs:
            return
        exists = await self._client.exists(self._meta_key(session_id))
        if not exists:
            await self.create(session_id, SessionMeta(session_id=session_id))
        lines = [json.dumps(msg_to_dict(m), ensure_ascii=False) for m in msgs]
        await self._client.rpush(self._msgs_key(session_id), *lines)

    async def load(self, session_id: str, *, since: int = 0) -> list[Message]:
        raw = await self._client.lrange(self._msgs_key(session_id), since, -1)
        out: list[Message] = []
        for line in raw:
            try:
                out.append(msg_from_dict(json.loads(line)))
            except (ValueError, TypeError, KeyError):
                continue
        return out

    async def rewrite(self, session_id: str, msgs: list[Message], reason: str) -> None:
        meta = await self.load_meta(session_id)
        extra = dict(meta.extra)
        extra["last_rewrite_reason"] = reason
        meta.extra = extra
        pipe = self._client.pipeline(transaction=True)
        pipe.delete(self._msgs_key(session_id))
        if msgs:
            lines = [json.dumps(msg_to_dict(m), ensure_ascii=False) for m in msgs]
            pipe.rpush(self._msgs_key(session_id), *lines)
        pipe.set(self._meta_key(session_id), json.dumps(_meta_to_dict(meta)))
        await pipe.execute()

    async def load_meta(self, session_id: str) -> SessionMeta:
        raw = await self._client.get(self._meta_key(session_id))
        if raw is None:
            return SessionMeta(session_id=session_id)
        return _meta_from_dict(json.loads(raw), session_id)

    async def update_meta(self, session_id: str, **fields: object) -> None:
        """Atomically update meta via WATCH/MULTI/EXEC optimistic locking.

        If another process modifies the meta key between our read and write,
        the transaction is retried (up to 10 times) so the update always lands
        on the latest state — no lost updates (cross-process safety fix)."""
        key = self._meta_key(session_id)
        for _ in range(10):
            await self._client.watch(key)
            raw = await self._client.get(key)
            meta = (
                _meta_from_dict(json.loads(raw), session_id)
                if raw is not None
                else SessionMeta(session_id=session_id)
            )
            for k, v in fields.items():
                if hasattr(meta, k) and k != "extra":
                    setattr(meta, k, v)
                else:
                    meta.extra[k] = v
            pipe = self._client.pipeline(transaction=True)
            pipe.set(key, json.dumps(_meta_to_dict(meta)))
            try:
                await pipe.execute()
                return  # success
            except Exception:  # WatchError or similar — retry
                continue
        # Fallback: last-write-wins (rare, only after 10 failed retries)
        await self._client.set(key, json.dumps(_meta_to_dict(meta)))

    async def list_sessions(self) -> list[str]:
        keys = await self._client.keys("sess:*:meta")
        return sorted(k.split(":")[1] for k in keys)

    async def delete(self, session_id: str) -> None:
        await self._client.delete(self._meta_key(session_id), self._msgs_key(session_id))


def _meta_to_dict(meta: SessionMeta) -> dict[str, object]:
    return {
        "session_id": meta.session_id,
        "model": meta.model,
        "cwd": meta.cwd,
        "title": meta.title,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "extra": dict(meta.extra),
    }


def _meta_from_dict(d: dict[str, object], session_id: str) -> SessionMeta:
    extra = d.get("extra", {})
    return SessionMeta(
        session_id=str(d.get("session_id", session_id)),
        model=str(d.get("model", "")),
        cwd=str(d.get("cwd", "")),
        title=str(d.get("title", "")),
        created_at=float(d.get("created_at", 0.0)),  # type: ignore[arg-type]
        updated_at=float(d.get("updated_at", 0.0)),  # type: ignore[arg-type]
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


__all__ = ["RedisStore"]
