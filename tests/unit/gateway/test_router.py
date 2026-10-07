"""Unit tests for SessionRouter routing + per_user isolation (docs/reference/extensions.md)."""

from __future__ import annotations

import asyncio

import pytest

from autoverse.frontends.gateway.router import (
    SessionPolicy,
    SessionRouter,
    route_session_id,
)


def test_per_user_distinct_per_user() -> None:
    a = route_session_id(
        "discord", "default", chat_id="c1", user_id="u1", thread_id="",
        is_dm=False, scope="per_user",
    )
    b = route_session_id(
        "discord", "default", chat_id="c1", user_id="u2", thread_id="",
        is_dm=False, scope="per_user",
    )
    assert a != b
    assert a.endswith(":u1") and b.endswith(":u2")


def test_per_channel_shares_channel() -> None:
    a = route_session_id(
        "discord", "default", chat_id="c1", user_id="u1", thread_id="",
        is_dm=False, scope="per_channel",
    )
    b = route_session_id(
        "discord", "default", chat_id="c1", user_id="u2", thread_id="",
        is_dm=False, scope="per_channel",
    )
    assert a == b


def test_single_scope_one_session() -> None:
    a = route_session_id(
        "discord", "default", chat_id="c1", user_id="u1", thread_id="",
        is_dm=False, scope="single",
    )
    b = route_session_id(
        "discord", "default", chat_id="c2", user_id="u2", thread_id="",
        is_dm=False, scope="single",
    )
    assert a == b


def test_dm_is_per_user_regardless_of_scope() -> None:
    a = route_session_id(
        "discord", "default", chat_id="c1", user_id="u1", thread_id="",
        is_dm=True, scope="per_channel",
    )
    assert ":dm:u1" in a


def test_thread_takes_priority() -> None:
    sid = route_session_id(
        "discord", "default", chat_id="c1", user_id="u1", thread_id="t9",
        is_dm=False, scope="per_user",
    )
    assert ":thread:t9" in sid


async def test_acquire_dedupes_concurrent_builds() -> None:
    builds: list[str] = []

    async def factory(sid: str) -> object:
        builds.append(sid)
        await asyncio.sleep(0.01)
        return _StubEngine(sid)

    router = SessionRouter(SessionPolicy(), factory)
    s1, s2 = await asyncio.gather(router.acquire("k"), router.acquire("k"))
    assert s1 is s2
    assert builds == ["k"]  # only built once


async def test_acquire_does_not_cache_failed_build() -> None:
    calls = {"n": 0}

    async def factory(sid: str) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return _StubEngine(sid)

    router = SessionRouter(SessionPolicy(), factory)
    with pytest.raises(RuntimeError):
        await router.acquire("k")
    sess = await router.acquire("k")  # retry rebuilds
    assert sess.session_id == "k"
    assert calls["n"] == 2


async def test_lru_eviction_keeps_under_cap() -> None:
    async def factory(sid: str) -> object:
        return _StubEngine(sid)

    router = SessionRouter(SessionPolicy(max_sessions=2), factory)
    await router.acquire("a")
    await router.acquire("b")
    await router.acquire("c")  # evicts LRU
    assert router.active_count() <= 2


class _StubEngine:
    def __init__(self, sid: str) -> None:
        self._sid = sid

    def run(
        self, prompt: str, attachments: tuple[object, ...] = ()
    ) -> object:
        raise NotImplementedError

    def abort(self, reason: str = "cancel") -> None: ...

    async def clear_in_place(self) -> int:
        return 0

    async def compact(self) -> dict[str, int]:
        return {}

    def switch_model(self, model: str) -> None: ...

    @property
    def session_id(self) -> str:
        return self._sid
