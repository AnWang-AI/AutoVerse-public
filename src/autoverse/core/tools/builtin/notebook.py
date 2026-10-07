"""NotebookEditTool (docs/reference/tools.md §11): read/replace/insert/delete Jupyter cells.

CONTROLLED_WRITE. Parses the .ipynb JSON, mutates a single cell by 0-based index,
and writes atomically. Out-of-range index and invalid cell types are reported as
clear ToolResult errors (never raised).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

_ACTIONS = ("read", "replace", "insert", "delete")
_CELL_TYPES = ("code", "markdown", "raw")


class NotebookEditArgs(BaseModel):
    notebook_path: str = Field(..., description="Path to the .ipynb file.")
    action: str = Field("read", description="read/replace/insert/delete.")
    cell_index: int | None = Field(None, description="0-based cell index.")
    cell_type: str = Field("code", description="Cell type for insert (code/markdown/raw).")
    new_source: str | None = Field(None, description="New cell source for replace/insert.")


def _source_lines(text: str) -> list[str]:
    # nbformat stores source as a list of lines, each keeping its trailing \n.
    return text.splitlines(keepends=True) or [""]


@register_tool
class NotebookEditTool(BaseTool):
    name: ClassVar[str] = "notebook_edit"
    description: ClassVar[str] = "Read or edit Jupyter notebook (.ipynb) cells."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = NotebookEditArgs

    async def run(self, args: NotebookEditArgs, ctx: ToolContext) -> ToolResult:
        if args.action not in _ACTIONS:
            return ToolResult.error(
                f"Unknown action {args.action!r}; use one of {_ACTIONS}.",
                error_kind="invalid_args",
            )
        path = ctx.resolve_path(args.notebook_path)
        try:
            doc: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ToolResult.error(f"Notebook not found: {path}", error_kind="not_found")
        except (OSError, json.JSONDecodeError) as exc:
            return ToolResult.error(
                f"Cannot read notebook {path}: {exc}", error_kind="io_error"
            )
        cells = doc.get("cells")
        if not isinstance(cells, list):
            return ToolResult.error(
                "Notebook has no 'cells' array.", error_kind="invalid_notebook"
            )

        if args.action == "read":
            return self._render(cells)

        if args.action == "insert":
            return await self._insert(doc, cells, args, ctx, path)

        # replace / delete need a valid index.
        idx = args.cell_index
        if idx is None or not (0 <= idx < len(cells)):
            return ToolResult.error(
                f"cell_index {idx} out of range (0..{len(cells) - 1}).",
                error_kind="index_error",
            )
        if args.action == "replace":
            if args.new_source is None:
                return ToolResult.error(
                    "replace requires new_source.", error_kind="invalid_args"
                )
            cells[idx]["source"] = _source_lines(args.new_source)
            return await self._flush(doc, ctx, path, f"Replaced cell {idx}.")
        # delete
        cells.pop(idx)
        return await self._flush(doc, ctx, path, f"Deleted cell {idx}.")

    async def _insert(
        self,
        doc: dict[str, Any],
        cells: list[Any],
        args: NotebookEditArgs,
        ctx: ToolContext,
        path: Any,
    ) -> ToolResult:
        if args.cell_type not in _CELL_TYPES:
            return ToolResult.error(
                f"Invalid cell_type {args.cell_type!r}; use {_CELL_TYPES}.",
                error_kind="invalid_args",
            )
        at = args.cell_index if args.cell_index is not None else len(cells)
        if not (0 <= at <= len(cells)):
            return ToolResult.error(
                f"insert index {at} out of range (0..{len(cells)}).",
                error_kind="index_error",
            )
        cell: dict[str, Any] = {
            "cell_type": args.cell_type,
            "metadata": {},
            "source": _source_lines(args.new_source or ""),
        }
        if args.cell_type == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        cells.insert(at, cell)
        return await self._flush(doc, ctx, path, f"Inserted {args.cell_type} cell at {at}.")

    @staticmethod
    def _render(cells: list[Any]) -> ToolResult:
        lines: list[str] = []
        for i, cell in enumerate(cells):
            ctype = cell.get("cell_type", "?")
            src = cell.get("source", "")
            if isinstance(src, list):
                src = "".join(src)
            lines.append(f"[{i}] ({ctype})\n{src}")
        return ToolResult.ok("\n\n".join(lines) or "(empty notebook)", cell_count=len(cells))

    @staticmethod
    async def _flush(doc: dict[str, Any], ctx: ToolContext, path: Any, msg: str) -> ToolResult:
        p = Path(path)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".ipynb.tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
            os.replace(tmp, p)
        except OSError as exc:
            return ToolResult.error(f"Cannot write notebook {p}: {exc}", error_kind="io_error")
        return ToolResult.ok(msg)


__all__ = ["NotebookEditTool"]
