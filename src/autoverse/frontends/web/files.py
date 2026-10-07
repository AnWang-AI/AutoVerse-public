"""Workspace file browsing and capability-scoped preview helpers for WebUI.

Directory and write operations remain session-cwd scoped. Read-only preview
helpers may additionally receive an exact set of external paths surfaced by the
active session; no parent-directory authority is inferred. Every helper returns
a JSON-ready dictionary for ``server.py`` routes.
"""

from __future__ import annotations

import mimetypes
import os
import re
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Collection, Iterable

MAX_TEXT_FILE_BYTES = 1 * 1024 * 1024
MAX_RAW_FILE_BYTES = 25 * 1024 * 1024

_IMAGE_MIME_BY_SUFFIX = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
_TABLE_SUFFIXES = {".csv", ".tsv"}
_MARKDOWN_SUFFIXES = {".md", ".markdown"}

_INLINE_CODE_PATH_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_MARKDOWN_LINK_PATH_RE = re.compile(r"\[[^\]\n]+\]\((?:<)?([^)>\n]+)(?:>)?\)")
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][\w+.-]*:")

_IGNORED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "~",
}


class FileAccessError(Exception):
    """HTTP-shaped error raised by file preview helpers."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def list_directory_payload(
    cwd: Path, raw_path: str | None, *, show_hidden: bool = False
) -> dict[str, Any]:
    """Return a JSON-ready directory listing under *cwd*."""

    root = cwd.expanduser().resolve()
    path = _resolve_under_root(root, raw_path or ".")
    if not path.is_dir():
        raise FileAccessError(404, f"not a directory: {_display_path(path, root)}")

    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if not show_hidden and entry.name.startswith("."):
                    continue
                if _is_filtered_entry(entry.name):
                    continue
                entries.append(_entry_payload(entry, root, path))
    except PermissionError as exc:
        raise FileAccessError(403, "permission denied") from exc
    except OSError as exc:
        raise FileAccessError(400, str(exc)) from exc

    entries.sort(key=lambda e: (not bool(e["is_dir"]), str(e["name"]).lower()))
    return {
        "path": _display_path(path, root),
        "root": str(root),
        "entries": entries,
    }


def rename_file(
    cwd: Path, raw_path: str | None, new_name: str
) -> dict[str, Any]:
    """Rename a file or directory under *cwd* and return the new path info."""

    root = cwd.expanduser().resolve()
    path = _resolve_under_root(root, raw_path or "")
    if not path.exists():
        raise FileAccessError(404, f"file not found: {_display_path(path, root)}")
    safe = str(new_name).strip()
    if not safe:
        raise FileAccessError(400, "new name is empty")
    if "/" in safe or "\\" in safe:
        raise FileAccessError(400, "new name must not contain path separators")
    if safe.startswith("."):
        raise FileAccessError(400, "new name must not start with a dot")
    target = path.parent / safe
    if not _is_relative_to(target, root):
        raise FileAccessError(403, "target is outside the session directory")
    if target.exists():
        raise FileAccessError(409, f"target already exists: {safe}")
    try:
        path.rename(target)
    except PermissionError as exc:
        raise FileAccessError(403, "permission denied") from exc
    except OSError as exc:
        raise FileAccessError(400, str(exc)) from exc
    return {
        "old_path": _display_path(path, root),
        "path": _display_path(target, root),
        "name": safe,
        "is_dir": target.is_dir(),
    }


def delete_file(cwd: Path, raw_path: str | None) -> dict[str, Any]:
    """Delete a file or directory under *cwd*."""

    root = cwd.expanduser().resolve()
    path = _resolve_under_root(root, raw_path or "")
    if not path.exists():
        raise FileAccessError(404, f"file not found: {_display_path(path, root)}")
    is_dir = path.is_dir()
    try:
        if is_dir:
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()
    except PermissionError as exc:
        raise FileAccessError(403, "permission denied") from exc
    except OSError as exc:
        raise FileAccessError(400, str(exc)) from exc
    return {
        "path": _display_path(path, root),
        "is_dir": is_dir,
        "deleted": True,
    }


def read_text_file_payload(
    cwd: Path,
    raw_path: str | None,
    *,
    allowed_external_paths: Collection[Path] = (),
) -> dict[str, Any]:
    """Return UTF-8 text content from *cwd* or an authorized external file."""

    root = cwd.expanduser().resolve()
    path = _resolve_preview_file_path(
        root,
        raw_path or "",
        allowed_external_paths=allowed_external_paths,
    )
    if not path.is_file():
        raise FileAccessError(404, f"not a file: {_display_path(path, root)}")
    try:
        size = path.stat().st_size
    except PermissionError as exc:
        raise FileAccessError(403, "permission denied") from exc
    if size > MAX_TEXT_FILE_BYTES:
        raise FileAccessError(
            413,
            f"file too large ({size // 1024} KiB); max {MAX_TEXT_FILE_BYTES // 1024} KiB",
        )
    try:
        raw = path.read_bytes()
    except PermissionError as exc:
        raise FileAccessError(403, "permission denied") from exc
    except OSError as exc:
        raise FileAccessError(400, str(exc)) from exc
    if b"\x00" in raw[:8192]:
        raise FileAccessError(415, "binary file cannot be previewed as text")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileAccessError(415, "file is not valid UTF-8") from exc
    return {
        "kind": _text_preview_kind(path),
        "path": _display_path(path, root),
        "root": str(root),
        "name": path.name,
        "content": content,
        "size": size,
        "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
        "language": path.suffix.lower().lstrip(".") or "txt",
    }


def read_file_preview_payload(
    cwd: Path,
    raw_path: str | None,
    *,
    allowed_external_paths: Collection[Path] = (),
) -> dict[str, Any]:
    """Return WebUI preview metadata for an authorized file.

    Text-like files include UTF-8 content. Browser-native media such as images
    and PDFs return metadata only; the Web server serves their bytes via the raw
    endpoint after applying the same path authorization and size checks.
    """

    root = cwd.expanduser().resolve()
    path = _resolve_preview_file_path(
        root,
        raw_path or "",
        allowed_external_paths=allowed_external_paths,
    )
    if not path.is_file():
        raise FileAccessError(404, f"not a file: {_display_path(path, root)}")
    size = _file_size(path)
    suffix = path.suffix.lower()
    if suffix in _IMAGE_MIME_BY_SUFFIX:
        _check_raw_size(size)
        return _raw_preview_payload(path, root, size, "image", _IMAGE_MIME_BY_SUFFIX[suffix])
    if suffix == ".pdf":
        _check_raw_size(size)
        return _raw_preview_payload(path, root, size, "pdf", "application/pdf")
    try:
        return read_text_file_payload(
            cwd,
            raw_path,
            allowed_external_paths=allowed_external_paths,
        )
    except FileAccessError as exc:
        if exc.status_code not in {413, 415}:
            raise
    _check_raw_size(size)
    return _raw_preview_payload(path, root, size, "raw", _guess_mime_type(path))


def raw_file_info(
    cwd: Path,
    raw_path: str | None,
    *,
    allowed_external_paths: Collection[Path] = (),
) -> tuple[Path, str, str]:
    """Return a validated raw-preview file path, mime type, and display name."""

    root = cwd.expanduser().resolve()
    path = _resolve_preview_file_path(
        root,
        raw_path or "",
        allowed_external_paths=allowed_external_paths,
    )
    if not path.is_file():
        raise FileAccessError(404, f"not a file: {_display_path(path, root)}")
    _check_raw_size(_file_size(path))
    return path, _guess_mime_type(path), path.name


def _resolve_under_root(root: Path, raw_path: str) -> Path:
    text = str(raw_path or ".").strip() or "."
    try:
        candidate = Path(text).expanduser()
    except (OSError, ValueError) as exc:
        raise FileAccessError(400, "invalid path") from exc
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise FileAccessError(400, "invalid path") from exc
    if not _is_relative_to(resolved, root):
        raise FileAccessError(403, "path is outside the session directory")
    return resolved


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except PermissionError as exc:
        raise FileAccessError(403, "permission denied") from exc


def _check_raw_size(size: int) -> None:
    if size > MAX_RAW_FILE_BYTES:
        raise FileAccessError(
            413,
            f"file too large ({size // 1024} KiB); max {MAX_RAW_FILE_BYTES // 1024} KiB",
        )


def _text_preview_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in _TABLE_SUFFIXES:
        return "table"
    return "text"


def _guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_MIME_BY_SUFFIX:
        return _IMAGE_MIME_BY_SUFFIX[suffix]
    if suffix == ".pdf":
        return "application/pdf"
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _raw_preview_payload(
    path: Path, root: Path, size: int, kind: str, mime: str
) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": _display_path(path, root),
        "root": str(root),
        "name": path.name,
        "size": size,
        "mime": mime,
        "language": path.suffix.lower().lstrip(".") or "bin",
    }


def referenced_external_preview_paths(
    cwd: Path,
    blocks: object,
    *,
    supplemental_paths: Iterable[str] = (),
) -> frozenset[Path]:
    """Return exact external files explicitly surfaced by the current session.

    Conversation Markdown contributes link destinations and inline-code paths;
    callers may additionally supply file paths already shown in trusted sidebar
    state such as Changed or Memory. Workspace-relative references are omitted
    because the normal cwd boundary already authorizes them.
    """

    root = cwd.expanduser().resolve()
    candidates = list(supplemental_paths)
    for text in _iter_strings(blocks):
        decoded = unescape(text)
        candidates.extend(_INLINE_CODE_PATH_RE.findall(decoded))
        candidates.extend(_MARKDOWN_LINK_PATH_RE.findall(decoded))

    paths: set[Path] = set()
    for raw in candidates:
        path = _resolve_reference_candidate(root, raw)
        if path is not None and not _is_relative_to(path, root):
            paths.add(path)
    return frozenset(paths)


def _resolve_preview_file_path(
    root: Path,
    raw_path: str,
    *,
    allowed_external_paths: Collection[Path] = (),
) -> Path:
    text = _strip_preview_location_suffix(raw_path)
    path = _resolve_path(root, text)
    if _is_relative_to(path, root):
        return path
    allowed = {_resolve_path(root, str(candidate)) for candidate in allowed_external_paths}
    if path not in allowed:
        raise FileAccessError(403, "path is outside the session directory")
    return path


def _resolve_path(root: Path, raw_path: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise FileAccessError(400, "invalid path")
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FileAccessError(400, "invalid path") from exc


def _resolve_reference_candidate(root: Path, raw_path: str) -> Path | None:
    text = _strip_preview_location_suffix(str(raw_path or "").strip())
    if not _looks_like_file_reference(text) or _URI_SCHEME_RE.match(text):
        return None
    try:
        return _resolve_path(root, text)
    except FileAccessError:
        return None


def _looks_like_file_reference(value: str) -> bool:
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return False
    if text.startswith(("/", "~/", "./", "../")):
        return True
    basename = text.replace("\\", "/").rsplit("/", 1)[-1]
    if basename.lower() in {
        "dockerfile",
        "gemfile",
        "justfile",
        "makefile",
        "procfile",
        "rakefile",
    }:
        return True
    return bool(Path(basename).suffix)


def _iter_strings(value: object, *, depth: int = 0) -> Iterable[str]:
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item, depth=depth + 1)


def _strip_preview_location_suffix(raw_path: str) -> str:
    text = str(raw_path or "").strip()
    for sep in ("#", "?"):
        head, found, _tail = text.partition(sep)
        if found and head:
            text = head
            break
    parts = text.rsplit(":", 2)
    if len(parts) >= 2 and parts[-1].isdigit() and parts[-2]:
        if len(parts) == 3 and parts[-2].isdigit():
            return parts[0]
        return ":".join(parts[:-1])
    return text


def _entry_payload(entry: os.DirEntry[str], root: Path, parent: Path) -> dict[str, Any]:
    path = parent / entry.name
    try:
        stat = entry.stat(follow_symlinks=False)
        try:
            is_dir = entry.is_dir(follow_symlinks=True)
        except OSError:
            is_dir = False
        size = None if is_dir else stat.st_size
        modified = datetime.fromtimestamp(stat.st_mtime).isoformat()
        symlink = entry.is_symlink()
    except (OSError, PermissionError):
        is_dir = False
        size = None
        modified = None
        symlink = False
    return {
        "name": entry.name,
        "path": _display_path(path, root),
        "is_dir": is_dir,
        "size": size,
        "modified": modified,
        "is_symlink": symlink,
    }


def _is_filtered_entry(name: str) -> bool:
    return name in _IGNORED_DIR_NAMES


def _display_path(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    out = rel.as_posix()
    return "." if out == "." else out


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "FileAccessError",
    "MAX_RAW_FILE_BYTES",
    "MAX_TEXT_FILE_BYTES",
    "delete_file",
    "list_directory_payload",
    "raw_file_info",
    "referenced_external_preview_paths",
    "read_file_preview_payload",
    "read_text_file_payload",
    "rename_file",
]
