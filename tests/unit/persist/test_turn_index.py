"""Tests for collision-free durable turn index allocation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from autoverse.infra.persist.turn_index import FileTurnIndexStore


async def test_file_turn_indices_are_unique_across_adapter_instances(
    tmp_path: Path,
) -> None:
    first = FileTurnIndexStore(tmp_path)
    second = FileTurnIndexStore(tmp_path)
    values = await asyncio.gather(
        *(first.reserve("s1") if i % 2 else second.reserve("s1") for i in range(20))
    )
    assert sorted(values) == list(range(20))


async def test_file_turn_index_honours_hydrated_minimum(tmp_path: Path) -> None:
    store = FileTurnIndexStore(tmp_path)
    assert await store.reserve("s1", minimum=8) == 8
    assert await store.reserve("s1", minimum=0) == 9


async def test_file_turn_index_rejects_session_path_traversal(tmp_path: Path) -> None:
    store = FileTurnIndexStore(tmp_path)
    with pytest.raises(ValueError):
        await store.reserve("../outside")
