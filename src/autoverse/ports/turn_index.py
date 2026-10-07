"""Port for collision-free session turn index allocation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TurnIndexPort(Protocol):
    async def reserve(self, session_id: str, *, minimum: int = 0) -> int: ...


__all__ = ["TurnIndexPort"]
