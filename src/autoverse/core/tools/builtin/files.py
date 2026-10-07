"""Builtin file tools (docs/reference/tools.md §8.1/§8.2, T6).

FileReadTool / FileWriteTool / FileEditTool / GlobTool / GrepTool / FileDeleteTool.
Write/edit tools enforce the FileTracker invariants and write atomically. None of
these call permissions (P2) — the dispatcher gates them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.edit_match import disproportionate_guard, find_match_unique
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _count_lines(text: str) -> int:
    """Non-empty line count — used for grep ``matches`` metadata (the inline
    label shows ``(N matches)`` MiMo-style)."""
    return sum(1 for ln in text.splitlines() if ln.strip())


def _unified_diff(old: str, new: str) -> tuple[str, int, int]:
    """Return ``(diff_text, added, removed)`` for a mutation (Edit/Write).

    The diff feeds the block renderer (MiMo ``<diff>`` parity); ``added`` /
    ``removed`` are line counts. Render-side concern only — full content stays
    in the file; this is metadata for display."""
    import difflib

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(old_lines, new_lines, lineterm="")
    rows: list[str] = []
    added = removed = 0
    for ln in diff_iter:
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        rows.append(ln.rstrip("\n"))
        if ln.startswith("+"):
            added += 1
        elif ln.startswith("-"):
            removed += 1
    return "\n".join(rows), added, removed


async def _diagnostics_meta(ctx: ToolContext, path: Path) -> list[dict[str, object]]:
    """Run the optional diagnostics seam and return JSON-safe finding dicts.

    The TUI renders these under the diff card (MiMo ``<Diagnostics>`` parity).
    Defensive: a missing seam or a misbehaving adapter yields ``[]`` — code
    quality feedback must never fail an otherwise-successful edit/write."""
    if ctx.diagnostics is None:
        return []
    try:
        diags = await ctx.diagnostics.check(path)
    except Exception:  # noqa: BLE001 - never break a write on a lint adapter error
        return []
    return [
        {
            "line": d.line,
            "column": d.column,
            "severity": d.severity,
            "message": d.message,
            "source": d.source,
        }
        for d in diags
    ]


def _escape_error(ctx: ToolContext, file_path: str) -> ToolResult | None:
    """Reject relative paths that traverse outside the workspace (T11).

    Absolute paths are allowed (gated by capability/permissions per §9.2);
    only relative inputs that normalise outside ``ctx.cwd`` are blocked here.
    Returns a ToolResult error to short-circuit, or None when the path is safe.
    """
    _, escaped = ctx.resolve_path_in_workspace(file_path)
    if escaped:
        return ToolResult.error(
            f"Path {file_path!r} escapes the workspace ({ctx.cwd}). "
            "Use a path inside the workspace, or an absolute path if you "
            "intend to address a file outside it.",
            error_kind="workspace_escape",
        )
    return None


# ── FileReadTool ─────────────────────────────────────────────────────────────
class FileReadArgs(BaseModel):
    file_path: str = Field(..., description="Path to the file to read.")
    start_line: int | None = Field(
        None, description="First line to read (1-based, inclusive)."
    )
    end_line: int | None = Field(
        None, description="Last line to read (1-based, inclusive)."
    )


@register_tool
class FileReadTool(BaseTool):
    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = "Read a file's contents (optionally a line range)."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = FileReadArgs

    async def run(self, args: FileReadArgs, ctx: ToolContext) -> ToolResult:
        escape = _escape_error(ctx, args.file_path)
        if escape is not None:
            return escape
        path = ctx.resolve_path(args.file_path)
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return ToolResult.error(
                f"File not found: {path}", error_kind="not_found"
            )
        except OSError as exc:
            return ToolResult.error(f"Cannot read {path}: {exc}", error_kind="io_error")
        mtime = _mtime_or_none(path)
        if mtime is not None:
            ctx.tracker.set_read_mtime(path, mtime)
        if args.start_line is not None or args.end_line is not None:
            lines = text.splitlines(keepends=True)
            start = max(1, args.start_line or 1)
            end = args.end_line if args.end_line is not None else len(lines)
            if end < start:
                return ToolResult.error(
                    f"end_line ({end}) < start_line ({start}).", error_kind="invalid_args"
                )
            text = "".join(lines[start - 1 : end])
            return ToolResult.ok(
                text,
                file_path=str(path),
                start_line=start,
                end_line=end,
                lines=f"{start}-{end}",
            )
        return ToolResult.ok(
            text, file_path=str(path), lines=text.count("\n") + 1 if text else 0
        )


# ── FileWriteTool ────────────────────────────────────────────────────────────
class FileWriteArgs(BaseModel):
    file_path: str = Field(..., description="Path to write.")
    content: str = Field(..., description="Full file content.")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:  # noqa: BLE001 — cleanup temp file before propagating
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@register_tool
class FileWriteTool(BaseTool):
    name: ClassVar[str] = "write_file"
    description: ClassVar[str] = "Write (overwrite) a file atomically."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = FileWriteArgs

    async def run(self, args: FileWriteArgs, ctx: ToolContext) -> ToolResult:
        escape = _escape_error(ctx, args.file_path)
        if escape is not None:
            return escape
        path = ctx.resolve_path(args.file_path)
        existed = path.exists()
        old_content = ""
        async with ctx.tracker.file_lock(path):
            if existed:
                ok, reason = ctx.tracker.was_read_before_write(path, _mtime_or_none(path))
                if not ok:
                    return ToolResult.error(reason, error_kind="stale_or_unread")
                try:
                    old_content = path.read_text(encoding="utf-8")
                except OSError:
                    old_content = ""
            try:
                _atomic_write(path, args.content)
            except OSError as exc:
                return ToolResult.error(
                    f"Cannot write {path}: {exc}", error_kind="io_error"
                )
            mtime = _mtime_or_none(path)
            if mtime is not None:
                ctx.tracker.set_read_mtime(path, mtime)
        diagnostics = await _diagnostics_meta(ctx, path)
        diff, added, removed = _unified_diff(old_content, args.content)
        return ToolResult.ok(
            f"Wrote {len(args.content)} bytes to {path}.",
            file_path=str(path),
            created=not existed,
            diff=diff,
            added=added,
            removed=removed,
            diagnostics=diagnostics,
        )


# ── FileEditTool ─────────────────────────────────────────────────────────────
class FileEditArgs(BaseModel):
    file_path: str = Field(..., description="Path to edit.")
    old_string: str = Field(..., description="String to replace.")
    new_string: str = Field(..., description="Replacement string.")
    occurrence: int = Field(1, description="Which occurrence: 1=first, -1=all.")


@register_tool
class FileEditTool(BaseTool):
    name: ClassVar[str] = "edit_file"
    description: ClassVar[str] = "Replace a string in a file."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = FileEditArgs

    async def run(self, args: FileEditArgs, ctx: ToolContext) -> ToolResult:
        escape = _escape_error(ctx, args.file_path)
        if escape is not None:
            return escape
        path = ctx.resolve_path(args.file_path)
        async with ctx.tracker.file_lock(path):
            try:
                content = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ToolResult.error(
                    f"File not found: {path}", error_kind="not_found"
                )
            except OSError as exc:
                return ToolResult.error(
                    f"Cannot read {path}: {exc}", error_kind="io_error"
                )

            ok, reason = ctx.tracker.was_read_before_write(path, _mtime_or_none(path))
            if not ok:
                return ToolResult.error(reason, error_kind="stale_or_unread")

            idx, strategy, count = find_match_unique(content, args.old_string)
            if idx is None:
                preview = args.old_string[:80]
                return ToolResult.error(
                    f"Could not find the search string in {path}.\n"
                    f"Searched for: {preview!r}\n"
                    "Tried verbatim, smart-quote normalization, and whitespace "
                    "normalization. Re-read the file and copy the exact text.",
                    error_kind="no_match",
                )

            # Uniqueness first (docs/reference/tools.md §8.2(b)): if old_string matches multiple
            # locations and the caller did not ask to replace all (occurrence=-1),
            # refuse rather than silently editing the first match.
            if count > 1 and args.occurrence != -1:
                return ToolResult.error(
                    f"old_string matches {count} locations in {path}, "
                    f"but occurrence={args.occurrence} would only replace one.\n"
                    "Add more surrounding context to make old_string unique, "
                    "or pass occurrence=-1 to replace all matches.",
                    error_kind="ambiguous_match",
                    matches=count,
                )

            if disproportionate_guard(args.old_string, args.new_string):
                return ToolResult.error(
                    "Replacement is disproportionately larger than the matched "
                    "region; refusing the edit. Make a smaller, targeted change.",
                    error_kind="disproportionate",
                )

            count = -1 if args.occurrence == -1 else 1
            new_content = content.replace(args.old_string, args.new_string, count)
            if new_content == content:
                # Matched only after normalization; fall back to single replace at idx.
                end = idx + len(args.old_string)
                new_content = content[:idx] + args.new_string + content[end:]

            try:
                _atomic_write(path, new_content)
            except OSError as exc:
                return ToolResult.error(
                    f"Cannot write {path}: {exc}", error_kind="io_error"
                )
            mtime = _mtime_or_none(path)
            if mtime is not None:
                ctx.tracker.set_read_mtime(path, mtime)

        diagnostics = await _diagnostics_meta(ctx, path)
        diff, added, removed = _unified_diff(content, new_content)
        return ToolResult.ok(
            f"Edited {path} (strategy: {strategy}).",
            file_path=str(path),
            strategy=strategy,
            diff=diff,
            added=added,
            removed=removed,
            diagnostics=diagnostics,
        )


# ── GlobTool ─────────────────────────────────────────────────────────────────
class GlobArgs(BaseModel):
    pattern: str = Field(..., description="Glob pattern, e.g. '**/*.py'.")
    path: str | None = Field(None, description="Root directory (default: cwd).")


@register_tool
class GlobTool(BaseTool):
    name: ClassVar[str] = "glob"
    description: ClassVar[str] = "Find files matching a glob pattern."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = GlobArgs

    # Hard cap on returned paths. An unbounded dump (e.g. ``**/*.py`` over a big
    # tree → tens of thousands of paths) is useless to the model and can blow the
    # context window in a single tool result, tripping emergency truncation. Cap
    # at a sane page (matches the documented "up to 1,000 paths" contract); the
    # model narrows the pattern or adds a subdir to see more.
    _MAX_MATCHES: ClassVar[int] = 1000

    async def run(self, args: GlobArgs, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve_path(args.path) if args.path else ctx.cwd

        def _glob_sync() -> list[str]:
            return sorted(str(p) for p in root.glob(args.pattern) if p.is_file())

        matches = await asyncio.to_thread(_glob_sync)
        total = len(matches)
        truncated = total > self._MAX_MATCHES
        shown = matches[: self._MAX_MATCHES]
        out = "\n".join(shown)
        if truncated:
            out += (
                f"\n… +{total - self._MAX_MATCHES} more "
                f"(showing first {self._MAX_MATCHES} of {total}; "
                f"narrow the pattern or pass a subdirectory path)"
            )
        return ToolResult.ok(
            out,
            pattern=args.pattern,
            path=str(root) if args.path else "",
            count=total,
            truncated=truncated,
        )


# ── GrepTool ─────────────────────────────────────────────────────────────────
class GrepArgs(BaseModel):
    pattern: str = Field(..., description="Regex pattern to search for.")
    path: str | None = Field(None, description="File or directory to search.")
    glob: str | None = Field(None, description="Glob filter for files.")
    type: str | None = Field(
        None, description="File-type shorthand (e.g. py, js, ts) — ripgrep --type."
    )
    output_mode: str = Field(
        "content", description="content / files_with_matches / count."
    )
    context_lines: int = Field(0, ge=0, description="Lines of context before/after each match.")


@register_tool
class GrepTool(BaseTool):
    name: ClassVar[str] = "grep"
    description: ClassVar[str] = "Search file contents with a regex."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = GrepArgs

    async def run(self, args: GrepArgs, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve_path(args.path) if args.path else ctx.cwd
        meta = {"pattern": args.pattern, "path": str(root) if args.path else ""}
        rg = await asyncio.to_thread(self._try_ripgrep, args, root)
        if rg is not None:
            return ToolResult.ok(
                rg, engine="ripgrep", matches=_count_lines(rg), **meta
            )
        out = await asyncio.to_thread(self._python_grep, args, root)
        return ToolResult.ok(out, engine="python", matches=_count_lines(out), **meta)

    @staticmethod
    def _try_ripgrep(args: GrepArgs, root: Path) -> str | None:
        cmd = ["rg", "--no-heading", args.pattern, str(root)]
        if args.output_mode == "files_with_matches":
            cmd.insert(1, "--files-with-matches")
        elif args.output_mode == "count":
            cmd.insert(1, "--count")
        else:
            cmd.insert(1, "--line-number")
            if args.context_lines:
                cmd[1:1] = ["--context", str(args.context_lines)]
        if args.glob:
            cmd[1:1] = ["--glob", args.glob]
        if args.type:
            cmd[1:1] = ["--type", args.type]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        if proc.returncode not in (0, 1):  # 1 = no matches
            return None
        return proc.stdout

    @staticmethod
    def _python_grep(args: GrepArgs, root: Path) -> str:
        import re as _re

        regex = _re.compile(args.pattern)
        out: list[str] = []
        matched_files: list[str] = []
        counts: dict[str, int] = {}
        files = root.rglob(args.glob) if args.glob else (
            [root] if root.is_file() else root.rglob("*")
        )
        for f in files:
            if not f.is_file():
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            file_hits = 0
            for n, line in enumerate(lines, 1):
                if regex.search(line):
                    file_hits += 1
                    if args.output_mode == "content":
                        ctx = args.context_lines
                        if ctx:
                            lo, hi = max(0, n - 1 - ctx), min(len(lines), n + ctx)
                            for j in range(lo, hi):
                                out.append(f"{f}:{j + 1}:{lines[j]}")
                        else:
                            out.append(f"{f}:{n}:{line}")
            if file_hits:
                matched_files.append(str(f))
                counts[str(f)] = file_hits
        if args.output_mode == "files_with_matches":
            return "\n".join(matched_files)
        if args.output_mode == "count":
            return "\n".join(f"{f}:{c}" for f, c in counts.items())
        return "\n".join(out)


# ── FileDeleteTool ───────────────────────────────────────────────────────────
class FileDeleteArgs(BaseModel):
    path: str = Field(..., description="Path to delete (file or directory).")


@register_tool
class FileDeleteTool(BaseTool):
    name: ClassVar[str] = "delete_file"
    description: ClassVar[str] = "Delete a file or directory (idempotent)."
    capability: ClassVar[CapabilityTier] = CapabilityTier.ARBITRARY_WRITE
    Args: ClassVar[type[BaseModel]] = FileDeleteArgs

    async def run(self, args: FileDeleteArgs, ctx: ToolContext) -> ToolResult:
        escape = _escape_error(ctx, args.path)
        if escape is not None:
            return escape
        path = ctx.resolve_path(args.path)
        if not path.exists():
            return ToolResult.ok(
                f"Nothing to delete: {path} (idempotent).",
                file_path=str(path),
                existed=False,
            )
        is_dir = path.is_dir()
        try:
            if is_dir:
                import shutil

                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            return ToolResult.error(
                f"Cannot delete {path}: {exc}", error_kind="io_error"
            )
        return ToolResult.ok(
            f"Deleted {path}.", file_path=str(path), is_dir=is_dir, existed=True
        )


__all__ = [
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "GlobTool",
    "GrepTool",
    "FileDeleteTool",
]
