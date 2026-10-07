"""Unit: builtin file tools (T6)."""

from __future__ import annotations

from pathlib import Path

from autoverse.core.tools.builtin.files import (
    FileDeleteTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
)
from autoverse.types.tools import ToolResult

from tests.conftest import make_ctx


async def test_read_sets_mtime_and_returns(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("content here")
    ctx = make_ctx(tmp_path)
    res = await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    assert res.succeeded and "content here" in res.output
    assert ctx.tracker.get_last_read_mtime(f) is not None


async def test_write_requires_prior_read_for_existing(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("old")
    ctx = make_ctx(tmp_path)
    res = await FileWriteTool().run(
        FileWriteTool.Args(file_path=str(f), content="new"), ctx
    )
    assert res.is_error and res.error_kind == "stale_or_unread"


async def test_write_new_file_ok(tmp_path: Path) -> None:
    f = tmp_path / "new.txt"
    ctx = make_ctx(tmp_path)
    res = await FileWriteTool().run(
        FileWriteTool.Args(file_path=str(f), content="hi"), ctx
    )
    assert res.succeeded and f.read_text() == "hi"
    assert res.metadata["created"] is True


async def test_t6_edit_no_match_gives_guidance(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    ctx = make_ctx(tmp_path)
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(f), old_string="zzz", new_string="q"), ctx
    )
    assert res.is_error and res.error_kind == "no_match"
    assert "Searched for" in res.output and "Re-read" in res.output


async def test_t6_edit_success(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    ctx = make_ctx(tmp_path)
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(f), old_string="world", new_string="there"), ctx
    )
    assert res.succeeded and f.read_text() == "hello there"


async def test_edit_ambiguous_multiple_matches(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("foo\nfoo\nfoo\n")
    ctx = make_ctx(tmp_path)
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(f), old_string="foo", new_string="bar"), ctx
    )
    assert res.is_error and res.error_kind == "ambiguous_match"
    assert "3" in res.output
    assert f.read_text() == "foo\nfoo\nfoo\n"  # unchanged


async def test_edit_ambiguous_resolved_by_occurrence_all(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("foo\nfoo\n")
    ctx = make_ctx(tmp_path)
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(
            file_path=str(f), old_string="foo", new_string="bar", occurrence=-1
        ),
        ctx,
    )
    assert res.succeeded and f.read_text() == "bar\nbar\n"


async def test_edit_attaches_diagnostics_metadata(tmp_path: Path) -> None:
    """The edit tool surfaces the diagnostics seam's findings as JSON-safe
    metadata so the TUI can render a MiMo ``<Diagnostics>`` block under the diff."""
    from pathlib import Path as _Path

    from autoverse.ports.diagnostics import Diagnostic

    class _FakeDiag:
        async def check(self, path: _Path) -> tuple[Diagnostic, ...]:
            return (
                Diagnostic(str(path), 1, 5, "warning", "unused import", "ruff"),
            )

    f = tmp_path / "a.py"
    f.write_text("import os\nx = 1\n")
    ctx = make_ctx(tmp_path)
    ctx.diagnostics = _FakeDiag()  # type: ignore[assignment]
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(f), old_string="x = 1", new_string="x = 2"), ctx
    )
    assert res.succeeded
    diags = res.metadata["diagnostics"]
    assert isinstance(diags, list) and len(diags) == 1
    assert diags[0]["severity"] == "warning" and diags[0]["message"] == "unused import"
    assert diags[0]["line"] == 1 and diags[0]["column"] == 5


async def test_write_attaches_diagnostics_metadata(tmp_path: Path) -> None:
    """Write (new file) also runs the diagnostics seam (parity with edit)."""
    from pathlib import Path as _Path

    from autoverse.ports.diagnostics import Diagnostic

    class _FakeDiag:
        async def check(self, path: _Path) -> tuple[Diagnostic, ...]:
            return (Diagnostic(str(path), 2, 0, "error", "syntax error", "pyflakes"),)

    f = tmp_path / "n.py"
    ctx = make_ctx(tmp_path)
    ctx.diagnostics = _FakeDiag()  # type: ignore[assignment]
    res = await FileWriteTool().run(
        FileWriteTool.Args(file_path=str(f), content="def f(:\n"), ctx
    )
    assert res.succeeded
    diags = res.metadata["diagnostics"]
    assert len(diags) == 1 and diags[0]["severity"] == "error"


async def test_edit_diagnostics_adapter_error_is_swallowed(tmp_path: Path) -> None:
    """A misbehaving diagnostics adapter must not fail an otherwise-good edit."""
    from pathlib import Path as _Path

    class _BoomDiag:
        async def check(self, path: _Path) -> tuple:  # noqa: ANN401
            raise RuntimeError("lsp crashed")

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    ctx = make_ctx(tmp_path)
    ctx.diagnostics = _BoomDiag()  # type: ignore[assignment]
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(f), old_string="x = 1", new_string="x = 2"), ctx
    )
    assert res.succeeded and res.metadata["diagnostics"] == []


async def test_read_blocks_relative_traversal(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    res = await FileReadTool().run(
        FileReadTool.Args(file_path="../../etc/passwd"), ctx
    )
    assert res.is_error and res.error_kind == "workspace_escape"


async def test_write_blocks_relative_traversal(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    res = await FileWriteTool().run(
        FileWriteTool.Args(file_path="../escape.txt", content="x"), ctx
    )
    assert res.is_error and res.error_kind == "workspace_escape"
    assert not (tmp_path.parent / "escape.txt").exists()


async def test_isolated_absolute_source_path_maps_to_worktree(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    iso = tmp_path / "repo" / ".autoverse" / "worktrees" / "box"
    source.mkdir(parents=True)
    iso.mkdir(parents=True)
    src_file = source / "pkg" / "mod.py"
    iso_file = iso / "pkg" / "mod.py"
    src_file.parent.mkdir()
    iso_file.parent.mkdir()
    src_file.write_text("x = 1\n", encoding="utf-8")
    iso_file.write_text("x = 1\n", encoding="utf-8")
    ctx = make_ctx(iso, isolation_root=iso, isolation_source_root=source)

    await FileReadTool().run(FileReadTool.Args(file_path=str(src_file)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(src_file), old_string="x = 1", new_string="x = 2"),
        ctx,
    )

    assert res.succeeded
    assert src_file.read_text(encoding="utf-8") == "x = 1\n"
    assert iso_file.read_text(encoding="utf-8") == "x = 2\n"
    assert res.metadata["file_path"] == str(iso_file.resolve(strict=False))


async def test_isolated_absolute_outside_path_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    iso = tmp_path / "repo" / ".autoverse" / "worktrees" / "box"
    outside = tmp_path / "outside.txt"
    source.mkdir(parents=True)
    iso.mkdir(parents=True)
    outside.write_text("secret", encoding="utf-8")
    ctx = make_ctx(iso, isolation_root=iso, isolation_source_root=source)

    res = await FileReadTool().run(FileReadTool.Args(file_path=str(outside)), ctx)

    assert res.is_error and res.error_kind == "workspace_escape"


async def test_edit_blocks_relative_traversal(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    res = await FileEditTool().run(
        FileEditTool.Args(
            file_path="../../etc/passwd", old_string="root", new_string="x"
        ),
        ctx,
    )
    assert res.is_error and res.error_kind == "workspace_escape"


async def test_glob(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("")
    (tmp_path / "y.txt").write_text("")
    ctx = make_ctx(tmp_path)
    res = await GlobTool().run(GlobTool.Args(pattern="*.py"), ctx)
    assert "x.py" in res.output and "y.txt" not in res.output
    assert res.metadata["count"] == 1
    assert res.metadata["truncated"] is False


async def test_glob_caps_output(tmp_path: Path) -> None:
    """A huge match set is capped (avoids blowing the context); the note states
    the true total and the metadata flags truncation."""
    cap = GlobTool._MAX_MATCHES
    for i in range(cap + 25):
        (tmp_path / f"f{i:05d}.py").write_text("")
    ctx = make_ctx(tmp_path)
    res = await GlobTool().run(GlobTool.Args(pattern="*.py"), ctx)
    path_lines = [ln for ln in res.output.splitlines() if ln.endswith(".py")]
    assert len(path_lines) == cap
    assert res.metadata["count"] == cap + 25
    assert res.metadata["truncated"] is True
    assert "+25 more" in res.output and f"of {cap + 25}" in res.output


async def test_grep(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("alpha\nbeta\n")
    ctx = make_ctx(tmp_path)
    res = await GrepTool().run(GrepTool.Args(pattern="beta", path=str(tmp_path)), ctx)
    assert "beta" in res.output


async def test_delete_file_escape_guard(tmp_path: Path) -> None:
    """delete_file must block relative traversal the same as read/write/edit."""
    ctx = make_ctx(tmp_path)
    res = await FileDeleteTool().run(
        FileDeleteTool.Args(path="../../etc/passwd"), ctx
    )
    assert res.is_error, "delete_file should block traversal escape"
    assert res.error_kind == "workspace_escape"


async def test_delete_file_basic(tmp_path: Path) -> None:
    """delete_file should succeed for files inside the workspace."""
    f = tmp_path / "to_delete.txt"
    f.write_text("bye")
    ctx = make_ctx(tmp_path)
    res = await FileDeleteTool().run(
        FileDeleteTool.Args(path=str(f)), ctx
    )
    assert res.succeeded
    assert not f.exists()
