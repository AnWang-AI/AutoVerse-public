"""Cancellation race helper (docs/reference/tools.md §2, docs/architecture/contracts.md §2).

`race_with_abort` runs a tool coroutine against the abort signal and an optional
timeout via a three-way `asyncio.wait`. Whichever finishes first wins:
  - coro completes -> its result is returned;
  - signal fires   -> AbortError(signal.reason) is raised;
  - timeout elapses -> asyncio.TimeoutError is raised.
Pending tasks are always cancelled. `abort_result` renders an AbortError as a
ToolResult for the dispatcher.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar, cast

from autoverse.types.abort import AbortError, AbortSignal
from autoverse.types.tools import ToolResult

T = TypeVar("T")


async def race_with_abort(
    coro: Awaitable[T], *, signal: AbortSignal, timeout: float | None
) -> T:
    """Race `coro` against `signal` and `timeout`. Raises on abort/timeout."""
    # Pre-abort short-circuit: if the signal is already aborted, abort wins
    # unconditionally — never run/return the coro. Otherwise a coro that is ready
    # in the same tick could be reported as success despite a standing abort.
    if signal.aborted:
        coro_task_pre: asyncio.Task[T] = asyncio.ensure_future(coro)
        coro_task_pre.cancel()
        await asyncio.gather(coro_task_pre, return_exceptions=True)
        raise AbortError(signal.reason)

    coro_task: asyncio.Task[T] = asyncio.ensure_future(coro)
    signal_task: asyncio.Task[object] = asyncio.ensure_future(signal.wait())

    waiters: set[asyncio.Task[object]] = {
        cast("asyncio.Task[object]", coro_task),
        signal_task,
    }
    done, pending = await asyncio.wait(
        waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )

    # Timeout: nothing finished.
    if not done:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise asyncio.TimeoutError()

    # Abort fired first (or alongside): prefer the abort signal.
    if signal_task in done and coro_task not in done:
        coro_task.cancel()
        await asyncio.gather(coro_task, return_exceptions=True)
        raise AbortError(signal.reason)

    # Coro finished: tidy the signal waiter and surface coro's outcome/exception.
    if not signal_task.done():
        signal_task.cancel()
        await asyncio.gather(signal_task, return_exceptions=True)
    return coro_task.result()


def abort_result(err: AbortError) -> ToolResult:
    """Render an AbortError as a ToolResult (interrupt vs cancel distinction)."""
    if err.reason == "interrupt":
        return ToolResult.error(
            "Tool interrupted by user.",
            error_kind="aborted",
            interrupted=True,
        )
    return ToolResult.error(
        "Tool cancelled.",
        error_kind="aborted",
        cancelled=True,
    )


__all__ = ["race_with_abort", "abort_result"]
