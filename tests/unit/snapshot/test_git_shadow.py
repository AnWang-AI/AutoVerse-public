"""Unit: GitShadowSnapshot."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from autoverse.infra.snapshot.git_shadow import GitShadowSnapshot

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


async def test_empty_workspace_is_a_restorable_snapshot(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    snap = GitShadowSnapshot(work, tmp_path / "shadow.git")
    ref = await snap.track()
    assert ref is not None
    assert ref.files == ()

    created = work / "created-after-snapshot.txt"
    created.write_text("temporary\n")
    changed = await snap.restore(ref)

    assert "created-after-snapshot.txt" in changed
    assert not created.exists()


async def test_track_then_diff_then_restore(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    f = work / "a.txt"
    f.write_text("v1\n")
    snap = GitShadowSnapshot(work, tmp_path / "shadow.git")

    ref = await snap.track()
    assert ref is not None
    assert "a.txt" in ref.files

    f.write_text("v2\n")
    diff = await snap.diff(ref)
    assert "v2" in diff or "v1" in diff

    changed = await snap.restore(ref)
    assert "a.txt" in changed
    assert f.read_text() == "v1\n"


async def test_resolve_rehydrates_snapshot_after_restart(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("v1\n")
    git_dir = tmp_path / "shadow.git"
    first = GitShadowSnapshot(work, git_dir)
    ref = await first.track()
    assert ref is not None

    resumed = GitShadowSnapshot(work, git_dir)
    resolved = await resumed.resolve(ref.hash)

    assert resolved is not None
    assert resolved.hash == ref.hash


async def test_large_file_skipped(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "small.txt").write_text("ok\n")
    (work / "big.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    snap = GitShadowSnapshot(
        work, tmp_path / "shadow.git", large_file_limit=1024 * 1024
    )
    ref = await snap.track()
    assert ref is not None
    assert "big.bin" not in ref.files
    assert "small.txt" in ref.files
