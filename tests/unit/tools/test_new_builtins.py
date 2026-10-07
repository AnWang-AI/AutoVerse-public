"""gap1 — coverage for newly added builtin tools and the file-tool rename.

Verifies registration under the spec-canonical names, the new utility/notebook
tools, and read_file line-range / grep modes. (asyncio_mode=auto — no marker.)
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import autoverse.core.tools.builtin  # noqa: F401 — triggers registration
from autoverse.core.tools.builtin.files import FileReadArgs, FileReadTool, GrepArgs, GrepTool
from autoverse.core.tools.builtin.notebook import NotebookEditArgs, NotebookEditTool
from autoverse.core.tools.builtin.utility import (
    GenerateUuidArgs,
    GenerateUuidTool,
    HashTextArgs,
    HashTextTool,
    SleepArgs,
    SleepTool,
    ToolSearchArgs,
    ToolSearchTool,
)
from autoverse.core.tools.registry import ToolRegistry

from tests.conftest import make_ctx


# ── registration / naming ────────────────────────────────────────────────────

def test_spec_canonical_tool_names_registered() -> None:
    names = set(ToolRegistry.build().names())
    assert {"read_file", "write_file", "edit_file", "delete_file"} <= names
    assert not ({"file_read", "file_write", "file_edit"} & names)


def test_all_spec_builtins_present() -> None:
    names = set(ToolRegistry.build().names())
    expected = {
        "read_file", "write_file", "edit_file", "delete_file", "glob", "grep",
        "bash", "web_fetch", "web_search", "think", "todo_write",
        "notebook_edit", "tool_search", "generate_uuid", "hash_text", "sleep",
        "generate_image",
        "browser_navigate", "browser_screenshot", "browser_click", "browser_type",
        "browser_get_content", "browser_scroll", "browser_wait", "browser_select",
        "browser_keyboard", "browser_evaluate", "browser_close",
        "browser_list_pages", "browser_new_page", "browser_switch_page",
        "browser_close_page",
    }
    missing = expected - names
    assert not missing, f"missing builtin tools: {sorted(missing)}"


# ── utility tools ─────────────────────────────────────────────────────────────

async def test_generate_uuid_versions(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    out = await GenerateUuidTool().run(GenerateUuidArgs(count=3, version=4), ctx)
    assert not out.is_error
    assert len([ln for ln in out.output.splitlines() if "-" in ln]) == 3
    bad = await GenerateUuidTool().run(GenerateUuidArgs(count=1, version=2), ctx)
    assert bad.is_error and bad.error_kind == "invalid_args"


async def test_hash_text_known_vector(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    out = await HashTextTool().run(
        HashTextArgs(text="abc", algorithm="sha256", encoding="hex"), ctx
    )
    assert "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" in out.output
    bad = await HashTextTool().run(HashTextArgs(text="x", algorithm="crc32"), ctx)
    assert bad.is_error and bad.error_kind == "invalid_args"


async def test_sleep_clamps(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    out = await SleepTool().run(SleepArgs(seconds=0.0), ctx)
    assert not out.is_error


async def test_tool_search_finds_by_keyword(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    # attach a live registry so tool_search has something to search.
    ctx = dataclasses.replace(ctx, app=dataclasses.replace(ctx.app, tools=ToolRegistry.build()))
    out = await ToolSearchTool().run(ToolSearchArgs(query="uuid"), ctx)
    assert not out.is_error
    assert "generate_uuid" in out.output


# ── notebook_edit ─────────────────────────────────────────────────────────────

async def test_notebook_edit_replace_and_read(tmp_path: Path) -> None:
    nb = tmp_path / "n.ipynb"
    nb.write_text(json.dumps({
        "cells": [{"cell_type": "code", "source": ["print(1)\n"], "metadata": {}, "outputs": []}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }), encoding="utf-8")
    ctx = make_ctx(tmp_path)
    rep = await NotebookEditTool().run(
        NotebookEditArgs(notebook_path=str(nb), action="replace", cell_index=0, new_source="print(2)\n"),
        ctx,
    )
    assert not rep.is_error
    doc = json.loads(nb.read_text())
    assert "".join(doc["cells"][0]["source"]) == "print(2)\n"
    rd = await NotebookEditTool().run(NotebookEditArgs(notebook_path=str(nb), action="read"), ctx)
    assert "print(2)" in rd.output


async def test_notebook_edit_index_out_of_range(tmp_path: Path) -> None:
    nb = tmp_path / "n.ipynb"
    nb.write_text(
        json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}),
        encoding="utf-8",
    )
    ctx = make_ctx(tmp_path)
    out = await NotebookEditTool().run(
        NotebookEditArgs(notebook_path=str(nb), action="delete", cell_index=5), ctx
    )
    assert out.is_error and out.error_kind == "index_error"


# ── read_file line range / grep modes ────────────────────────────────────────

async def test_read_file_line_range(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("a\nb\nc\nd\n", encoding="utf-8")
    ctx = make_ctx(tmp_path)
    out = await FileReadTool().run(FileReadArgs(file_path=str(f), start_line=2, end_line=3), ctx)
    assert out.output == "b\nc\n"
    bad = await FileReadTool().run(FileReadArgs(file_path=str(f), start_line=3, end_line=1), ctx)
    assert bad.is_error and bad.error_kind == "invalid_args"


async def test_grep_count_mode(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("foo\nfoo\nbar\n", encoding="utf-8")
    ctx = make_ctx(tmp_path)
    out = await GrepTool().run(
        GrepArgs(pattern="foo", path=str(tmp_path), output_mode="count"), ctx
    )
    assert not out.is_error
    assert "2" in out.output


# ── generate_image ────────────────────────────────────────────────────────────

async def test_generate_image_surfaces_resolved_model(tmp_path: Path, monkeypatch) -> None:
    """The tool must expose the model the backend actually used in metadata so the
    TUI can render it (parity with read_image's ``· {model}``)."""
    from autoverse.core.tools.builtin.imagegen import GenerateImageArgs, GenerateImageTool

    class _Result:
        path = str(tmp_path / "out.png")
        media_type = "image/png"
        model = "bytedance-seed/seedream-4.5"
        base64_data = "aGVsbG8="

    class _Backend:
        async def generate(self, **_: object) -> _Result:
            return _Result()

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    ctx = make_ctx(tmp_path)
    ctx.app.image_backend = _Backend()
    # Caller omits the model → the backend resolves a default we must surface.
    res = await GenerateImageTool().run(GenerateImageArgs(prompt="a cat"), ctx)
    assert not res.is_error
    assert res.metadata.get("model") == "bytedance-seed/seedream-4.5"


async def test_todo_write_attaches_todos_metadata(tmp_path: Path) -> None:
    """Regression: todo_write must attach the list as ToolResult metadata so the
    TUI sidebar can render it (the panel was empty because the metadata was
    missing AND the consumer looked for the wrong tool name)."""
    from autoverse.core.tools.builtin.think import TodoItem, TodoWriteArgs, TodoWriteTool

    ctx = make_ctx(cwd=tmp_path)
    args = TodoWriteArgs(todos=[
        TodoItem(id="1", content="design", status="completed"),
        TodoItem(id="2", content="build", status="in_progress"),
        TodoItem(id="3", content="verify", status="pending"),
    ])
    res = await TodoWriteTool().run(args, ctx)
    assert not res.is_error
    todos = res.metadata.get("todos")
    assert todos == [
        {"status": "completed", "content": "design"},
        {"status": "in_progress", "content": "build"},
        {"status": "pending", "content": "verify"},
    ]
