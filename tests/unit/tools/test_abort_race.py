"""Unit: race_with_abort + abort_result."""

from __future__ import annotations

import asyncio

import pytest

from autoverse.core.tools.abort_race import abort_result, race_with_abort
from autoverse.types.abort import AbortController, AbortError


async def test_coro_completes_returns_value() -> None:
    ctrl = AbortController()

    async def work() -> int:
        await asyncio.sleep(0.01)
        return 42

    assert await race_with_abort(work(), signal=ctrl.signal, timeout=1.0) == 42


async def test_abort_raises_abort_error() -> None:
    ctrl = AbortController()

    async def work() -> int:
        await asyncio.sleep(5)
        return 1

    task = asyncio.ensure_future(
        race_with_abort(work(), signal=ctrl.signal, timeout=5.0)
    )
    await asyncio.sleep(0.01)
    ctrl.abort("cancel")
    with pytest.raises(AbortError) as ei:
        await task
    assert ei.value.reason == "cancel"


async def test_timeout_raises() -> None:
    ctrl = AbortController()

    async def work() -> int:
        await asyncio.sleep(5)
        return 1

    with pytest.raises(asyncio.TimeoutError):
        await race_with_abort(work(), signal=ctrl.signal, timeout=0.01)


async def test_pre_aborted_signal_wins_over_ready_coro() -> None:
    """If the signal is already aborted on entry, abort must take priority even
    when the coro is immediately ready (no await point before completion)."""
    ctrl = AbortController()
    ctrl.abort("interrupt")

    async def already_done() -> int:
        return 7  # completes without yielding control

    with pytest.raises(AbortError) as ei:
        await race_with_abort(already_done(), signal=ctrl.signal, timeout=1.0)
    assert ei.value.reason == "interrupt"


def test_abort_result_interrupt_vs_cancel() -> None:
    r1 = abort_result(AbortError("interrupt"))
    assert r1.error_kind == "aborted" and r1.metadata.get("interrupted") is True
    r2 = abort_result(AbortError("cancel"))
    assert r2.error_kind == "aborted" and r2.metadata.get("cancelled") is True
