"""Path resolution helpers (docs/reference/tools.md §8). Pure functions, no I/O.

`resolve_path` mirrors the ToolContext path contract: absolute paths are
returned as-is (normalised), relative paths are resolved against the tool cwd.
`is_within` is the containment predicate used by workspace-escape guards (T11).
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_path(cwd: Path, file_path: str) -> Path:
    """Resolve `file_path` against `cwd`. Absolute paths pass through verbatim.

    Relative paths are normalised (collapsing `..`/`.`), so a traversal like
    `../../etc/passwd` resolves to a concrete escaped path that `is_within` can
    detect, rather than leaving the `..` segments unresolved.
    """
    p = Path(file_path)
    if p.is_absolute():
        return Path(os.path.normpath(str(p)))
    return Path(os.path.normpath(str(cwd / p)))


def resolve_in_workspace(cwd: Path, file_path: str) -> tuple[Path, bool]:
    """Resolve `file_path` and report whether a *relative* input escaped `cwd`.

    Returns (resolved_path, escaped). `escaped` is True only when a relative
    input normalises to a location outside `cwd` (e.g. `../../etc/passwd`).
    Absolute inputs are never flagged here — they legitimately address paths
    outside the workspace and are gated by capability tiers/permissions
    (docs/reference/tools.md §9.2), not by the resolver.
    """
    resolved = resolve_path(cwd, file_path)
    if Path(file_path).is_absolute():
        return (resolved, False)
    return (resolved, not is_within(cwd, resolved))


def is_within(base: Path, target: Path) -> bool:
    """True iff `target` is `base` itself or a descendant (T11).

    Operates lexically on normalised (non-resolved) paths so it is pure and does
    not touch the filesystem. `..` segments are collapsed first so an escape via
    `base/../other` is correctly rejected. Callers that need symlink-aware checks
    should `.resolve()` both arguments first.
    """
    base_n = Path(os.path.normpath(str(base)))
    target_n = Path(os.path.normpath(str(target)))
    try:
        target_n.relative_to(base_n)
        return True
    except ValueError:
        return False


__all__ = ["resolve_path", "resolve_in_workspace", "is_within"]
