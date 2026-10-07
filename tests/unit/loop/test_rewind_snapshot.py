"""gap5 — /rewind joint file+history revert (docs/architecture/runtime-flow.md §3.3a, docs/reference/tools.md §9.4).

Drives the TurnLoop rewind helpers against a real GitShadowSnapshot: track binds
a snapshot hash to a turn message; resolve_rewind_target finds it; rewind restores
files AND truncates history at the bound message.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from autoverse.core.loop.turn_loop import _with_snapshot_hash
from autoverse.infra.snapshot.git_shadow import GitShadowSnapshot
from autoverse.types.messages import Message, TextBlock

from tests._doubles import ScriptedProvider
from tests.acceptance.conftest import make_loop

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _scripted() -> ScriptedProvider:
    # the loop's LLM is never called in these rewind-helper tests
    return ScriptedProvider([])


async def test_rewind_restores_files_and_truncates_history(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    f = work / "a.txt"
    f.write_text("v1\n")
    snap = GitShadowSnapshot(work, tmp_path / "shadow.git")

    loop, conv, sink, app = make_loop(_scripted(), cwd=work)
    app.snapshot = snap  # type: ignore[attr-defined]

    # turn 1: capture a snapshot of v1, bind to a tool-result message
    ref = await loop._track_snapshot()
    assert ref  # non-empty hash
    await conv.append(_with_snapshot_hash(
        Message(role="user", content=(TextBlock(text="result"),)), ref
    ))
    await conv.append(Message(role="assistant", content=(TextBlock(text="after"),)))

    # mutate the file (simulate the turn's write)
    f.write_text("v2-mutated\n")

    # resolve + rewind
    target = await loop.resolve_rewind_target(ref)
    assert target is not None
    changed = await loop.rewind(target)
    assert "a.txt" in changed
    assert f.read_text() == "v1\n"  # file restored

    # history truncated at the bound message (the bound msg + later dropped)
    texts = [
        b.text for m in conv.view().messages for b in m.content if isinstance(b, TextBlock)
    ]
    assert "after" not in texts  # post-snapshot history gone


async def test_resolve_rewind_none_when_no_snapshots(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    loop, _conv, _sink, _app = make_loop(_scripted(), cwd=work)
    assert await loop.resolve_rewind_target("anything") is None
