"""SessionRouter — platform conversation -> AutoVerse session (docs/reference/extensions.md).

``route()`` is a pure function mapping an :class:`InboundMessage` to a stable
``session_id`` under a configurable strategy (per_user/per_channel/single, plus
the hard-coded DM and thread rules). ``acquire()`` lazily builds an
:class:`~autoverse.ports.EngineHandle` via an injected factory and caches the
runtime handle behind a per-session busy lock; concurrent acquisitions of the
same key share one build (never two engines for one session).

The router depends only on :class:`~autoverse.ports.EngineHandle` — it never
reaches into engine internals (P3).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from autoverse.ports import EngineHandle

Scope = Literal["per_user", "per_channel", "single"]

# Factory: given a stable session_id, build (or rebuild) an EngineHandle.
EngineFactory = Callable[[str], Awaitable[EngineHandle]]


@dataclass
class SessionPolicy:
    """Routing + lifecycle policy for gateway session mapping."""

    scope: Scope = "per_user"
    max_sessions: int = 128
    lock_timeout_seconds: float = 30.0


@dataclass
class ActiveSession:
    """Runtime handle for a session. History truth lives in the Store (P1); this
    is only a derived runtime handle."""

    session_id: str
    engine: EngineHandle
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # A separate approval lock lets follow-up messages queue during approval
    # waits without contending the busy lock (docs/reference/extensions.md).
    approval_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def route_session_id(msg_platform: str, msg_bot_id: str, *, chat_id: str,
                     user_id: str, thread_id: str, is_dm: bool,
                     scope: Scope) -> str:
    """Pure routing function (broken out for direct unit testing)."""
    p, b = msg_platform, msg_bot_id
    if is_dm:  # DM is always per_user (hard platform semantics)
        return f"{p}:{b}:dm:{user_id}"
    if thread_id:  # thread is a focused sub-conversation, shared within (highest priority)
        return f"{p}:{b}:thread:{thread_id}"
    if scope == "single":  # one shared assistant for the whole bot
        return f"{p}:{b}:single"
    if scope == "per_channel":  # whole channel shares one session
        return f"{p}:{b}:chan:{chat_id}"
    # default per_user: each person in a channel gets an independent session
    return f"{p}:{b}:chan:{chat_id}:{user_id}"


class SessionRouter:
    def __init__(self, cfg: SessionPolicy, engine_factory: EngineFactory) -> None:
        self._cfg = cfg
        self._factory = engine_factory
        self._sessions: "OrderedDict[str, ActiveSession]" = OrderedDict()
        self._creating: dict[str, asyncio.Future[ActiveSession]] = {}

    @property
    def scope(self) -> Scope:
        return self._cfg.scope

    def route(
        self,
        *,
        platform: str,
        bot_id: str,
        chat_id: str,
        user_id: str,
        thread_id: str = "",
        is_dm: bool = False,
    ) -> str:
        """Map message coordinates to a stable session_id (pure)."""
        return route_session_id(
            platform,
            bot_id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
            is_dm=is_dm,
            scope=self._cfg.scope,
        )

    async def acquire(self, session_id: str) -> ActiveSession:
        """Return the cached session or lazily build its engine.

        Concurrent acquisitions of the same new key share one build Future so we
        never construct two engines for one session.
        """
        existing = self._sessions.get(session_id)
        if existing is not None:
            self._sessions.move_to_end(session_id)  # LRU touch
            return existing

        inflight = self._creating.get(session_id)
        if inflight is not None:
            return await inflight

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[ActiveSession] = loop.create_future()
        self._creating[session_id] = fut
        try:
            engine = await self._factory(session_id)
            session = ActiveSession(session_id=session_id, engine=engine)
            self._evict_if_needed()
            self._sessions[session_id] = session
            self._sessions.move_to_end(session_id)
            fut.set_result(session)
            return session
        except BaseException as exc:  # don't cache failed sessions
            if not fut.done():
                fut.set_exception(exc)
                # Avoid "exception never retrieved" when no concurrent waiter:
                # this caller re-raises below, so consume the future's result.
                fut.exception()
            raise
        finally:
            self._creating.pop(session_id, None)

    def get(self, session_id: str) -> ActiveSession | None:
        return self._sessions.get(session_id)

    def active_count(self) -> int:
        return len(self._sessions)

    def _evict_if_needed(self) -> None:
        while len(self._sessions) >= self._cfg.max_sessions and self._sessions:
            # Drop the least-recently-used runtime handle; history stays in Store.
            self._sessions.popitem(last=False)

    async def shutdown(self) -> None:
        """Release all runtime handles (history already persisted by core)."""
        self._sessions.clear()
        self._creating.clear()


__all__ = [
    "Scope",
    "SessionPolicy",
    "ActiveSession",
    "SessionRouter",
    "EngineFactory",
    "route_session_id",
]
