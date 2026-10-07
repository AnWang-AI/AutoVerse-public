"""Interactive worktree-mode orchestration (docs/reference/subagents.md §5a, docs/guides/using-cli-and-tui.md §3.10).

Single source of truth (P1) for entering/exiting the session-level ``worktree``
interaction mode. Both the model tools (``EnterWorktreeMode``/``ExitWorktreeMode``)
and the REPL three-state mode cycle call these helpers, so the build/teardown
logic lives in exactly one place.

The mode is a mode-ified form of the L2 *manual*-lifecycle worktree:
  - enter:  ``Isolator.scope(lifecycle="manual")`` → register → switch ``app.cwd``
  - active: every tool's ``ToolContext.cwd`` is rebuilt from ``app.cwd`` each turn,
            so writes land inside the worktree without touching the dispatcher.
  - exit:   ``Isolator.finalize(action)`` → switch ``app.cwd`` back → clear flag.

fail-closed: a non-git repo or a worktree-creation failure refuses to enter the
mode (it never silently degrades "isolated write" into "main-tree write", §3.2).

``core`` purity: depends only on ``ports``/``types``/stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from autoverse.ports.isolation import (
    FinalizeAction,
    IsolationError,
    IsoSpec,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autoverse.app.context import AppContext
    from autoverse.ports.isolation import IsoHandle, IsoResult


# Single source of truth key on ``AppContext.extra`` (parallel to "plan_mode").
WORKTREE_MODE_KEY = "worktree_mode"
PLAN_MODE_KEY = "plan_mode"

_EXIT_ACTIONS = {
    "keep": FinalizeAction.KEEP,
    "merge": FinalizeAction.MERGE,  # real commit + merge-back (fail-safe on conflict)
    "discard": FinalizeAction.DISCARD,
}


@dataclass(frozen=True)
class EnterResult:
    """Outcome of an enter attempt (P5: always actionable)."""

    ok: bool
    branch: str | None = None
    cwd: Path | None = None
    reused: bool = False  # True when we resumed an existing session worktree
    orphan_count: int = 0
    has_changes: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class ExitResult:
    """Outcome of an exit attempt."""

    ok: bool
    action: str = ""
    branch: str | None = None
    commit: str | None = None
    removed: bool = False
    note: str = ""
    error: str | None = None


def is_active(app: "AppContext") -> bool:
    """True when the session is currently in worktree mode."""
    wm = _state(app)
    return bool(isinstance(wm, dict) and wm.get("active"))


def active_handle(app: "AppContext") -> "IsoHandle | None":
    """The live IsoHandle for the active worktree mode, if any."""
    wm = _state(app)
    if isinstance(wm, dict) and wm.get("active"):
        h = wm.get("handle")
        return h  # type: ignore[return-value]
    return None


async def has_changes(app: "AppContext") -> bool | None:
    """Whether the active worktree holds work that would be lost on discard.

    Returns ``True``/``False`` from the isolator's ``has_changes`` probe, or
    ``None`` when it can't be determined (not in worktree mode, no isolator, or
    an isolator without the capability). Callers should treat ``None``
    conservatively — i.e. prompt the user rather than silently act (P5). This
    powers the "skip the keep/merge/discard dialog for a pristine box and
    silently clean it up" shortcut; it never mutates state.
    """
    handle = active_handle(app)
    if handle is None:
        return None
    isolator = getattr(app, "isolator", None)
    probe = getattr(isolator, "has_changes", None)
    if probe is None:
        return None
    try:
        return bool(await probe(handle))
    except Exception:  # noqa: BLE001 — unknown → conservative (caller prompts)
        return None


def _state(app: "AppContext") -> Any:
    extra = getattr(app, "extra", None)
    return extra.get(WORKTREE_MODE_KEY) if isinstance(extra, dict) else None


def _session_id(app: "AppContext") -> str:
    extra = getattr(app, "extra", None)
    sid = extra.get("session_id") if isinstance(extra, dict) else None
    return str(sid or "session")


async def enter(
    app: "AppContext",
    *,
    reason: str = "",
    base_ref: str = "HEAD",
    reuse: bool = True,
    base_path: str | Path | None = None,
) -> EnterResult:
    """Enter worktree mode, reusing this session's existing box for this repo.

    Clears plan mode (the two interaction modes are mutually exclusive). On any
    isolation failure, returns ``EnterResult(ok=False, error=...)`` and leaves the
    session untouched — it never degrades to writing in the main work-tree.

    Sticky reuse (docs/guides/using-cli-and-tui.md §3.10.5): a ``keep`` exit leaves the manual
    worktree registered (``removed=False``), so re-entering the SAME git repo —
    e.g. Tab out then Tab back — resumes the same box. The registry key includes
    the resolved repo root, so a nested child repository gets its own worktree
    instead of accidentally reusing the parent repo's box.
    """
    if is_active(app):
        return EnterResult(ok=False, error="already in worktree mode")

    isolator = getattr(app, "isolator", None)
    if isolator is None:
        return EnterResult(ok=False, error="no isolator configured for this session")

    entry_cwd = Path(getattr(app, "cwd", Path.cwd()))
    base_cwd = _resolve_base_cwd(entry_cwd, base_path)
    try:
        source_root = await _resolve_source_root(isolator, base_cwd)
    except IsolationError as exc:
        return EnterResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — repo resolution is part of isolation
        return EnterResult(ok=False, error=f"worktree repo resolution failed: {exc}")
    registry = getattr(app, "worktrees", None)

    # ── Reuse a still-live worktree from earlier in THIS session ──────────────
    existing = None
    if registry is not None:
        existing = _active_manual(registry, _session_id(app), source_root)
        if existing is None:
            fallback = _active_manual(registry, _session_id(app), None)
            if fallback is not None and _path_within(Path(fallback.cwd), base_cwd):
                existing = fallback
                old_source = getattr(existing, "source_root", None)
                if old_source is not None:
                    source_root = Path(old_source).resolve(strict=False)
    if existing is not None and reuse:
        if Path(existing.cwd).exists():
            worktree_cwd = _worktree_cwd(existing, source_root, base_cwd)
            orphan_count = _record_orphans(app, source_root)
            dirty = await _probe_existing_changes(app, existing)
            app.cwd = worktree_cwd
            _clear_plan_mode(app)
            app.extra["group_root"] = str(source_root)
            app.extra[WORKTREE_MODE_KEY] = {
                "active": True,
                "handle": existing,
                "branch": existing.branch,
                "base_cwd": str(base_cwd),
                "restore_cwd": str(entry_cwd),
                "source_root": str(source_root),
                "worktree_root": str(existing.cwd),
                "worktree_cwd": str(worktree_cwd),
                "reason": reason,
                "reused": True,
            }
            return EnterResult(
                ok=True,
                branch=existing.branch,
                cwd=worktree_cwd,
                reused=True,
                orphan_count=orphan_count,
                has_changes=dirty,
            )
        # Stale registration (dir vanished, e.g. removed out-of-band) → drop it
        # and fall through to build a fresh box.
        if registry is not None:
            try:
                await registry.cleanup(existing)
            except Exception:  # noqa: BLE001
                pass
    elif existing is not None and not reuse and registry is not None:
        try:
            await registry.cleanup(existing)
        except Exception:  # noqa: BLE001
            pass

    # Best-effort: prune stale worktree registrations left by a crashed prior run
    # (P2; non-destructive — only drops admin entries whose dir already vanished).
    prune = getattr(isolator, "prune", None)
    if prune is not None:
        try:
            await prune(source_root)
        except Exception:  # noqa: BLE001 — maintenance must not block entering
            pass
    spec = IsoSpec(lifecycle="manual", base_ref=base_ref)
    # manual lifecycle: scope() does NOT auto-clean on __aexit__, so we create the
    # box, let the context exit, and keep the handle alive across turns until exit.
    try:
        handle: "IsoHandle | None" = None
        async with isolator.scope(spec, base_cwd) as h:
            handle = h
    except IsolationError as exc:
        return EnterResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — never enter on an unexpected failure
        return EnterResult(ok=False, error=f"worktree creation failed: {exc}")

    if handle is None:  # pragma: no cover - defensive
        return EnterResult(ok=False, error="worktree handle was not produced")

    if getattr(handle, "source_root", None) is None:
        handle.source_root = source_root
    source_root = Path(getattr(handle, "source_root", source_root) or source_root)

    if registry is not None:
        try:
            registry.register_manual(_session_id(app), handle)
        except Exception:  # noqa: BLE001 — registry bookkeeping must not abort entry
            pass

    # Switch the session cwd; the next turn's ToolContext picks it up. Preserve
    # the relative subdirectory used to enter when it exists in the worktree.
    worktree_cwd = _worktree_cwd(handle, source_root, base_cwd)
    app.cwd = worktree_cwd
    _clear_plan_mode(app)
    orphan_count = _record_orphans(app, source_root)
    app.extra["group_root"] = str(source_root)
    app.extra[WORKTREE_MODE_KEY] = {
        "active": True,
        "handle": handle,
        "branch": handle.branch,
        "base_cwd": str(base_cwd),
        "restore_cwd": str(entry_cwd),
        "source_root": str(source_root),
        "worktree_root": str(handle.cwd),
        "worktree_cwd": str(worktree_cwd),
        "reason": reason,
    }
    return EnterResult(
        ok=True, branch=handle.branch, cwd=worktree_cwd, orphan_count=orphan_count
    )


async def _probe_existing_changes(app: "AppContext", handle: "IsoHandle") -> bool | None:
    isolator = getattr(app, "isolator", None)
    probe = getattr(isolator, "has_changes", None)
    if probe is None:
        return None
    try:
        return bool(await probe(handle))
    except Exception:  # noqa: BLE001 — observability only
        return None


async def exit(app: "AppContext", *, action: str = "keep") -> ExitResult:
    """Finalize the active worktree and switch back to the main work-tree.

    ``action`` is one of ``keep`` / ``merge`` / ``discard``. Unknown values fall
    back to the safest option (``keep``) rather than risking data loss.
    """
    wm = _state(app)
    if not (isinstance(wm, dict) and wm.get("active")):
        return ExitResult(ok=False, error="not in worktree mode")

    handle = wm.get("handle")
    base_cwd = wm.get("base_cwd")
    restore_cwd = wm.get("restore_cwd") or base_cwd
    fin_action = _EXIT_ACTIONS.get(action, FinalizeAction.KEEP)
    isolator = getattr(app, "isolator", None)

    # KEEP: refuse if there are uncommitted changes — the agent must commit
    # first (it knows which files it changed, unlike a blind add -A).
    if action == "keep" and isolator is not None and handle is not None:
        try:
            dirty_probe = getattr(isolator, "has_uncommitted_changes", None)
            dirty = (
                await dirty_probe(handle)
                if callable(dirty_probe)
                else await isolator.has_changes(handle)
            )
            if dirty:
                return ExitResult(
                    ok=False,
                    action=action,
                    error=(
                        "Uncommitted changes in the worktree — "
                        "git add your changed files, git commit them, "
                        "then retry ExitWorktreeMode(action='keep')."
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — can't verify; fail closed
            return ExitResult(
                ok=False,
                action=action,
                error=f"Could not verify worktree cleanliness before keep: {exc}",
            )

    result: "IsoResult | None" = None
    err: str | None = None
    if isolator is not None and handle is not None:
        try:
            result = await isolator.finalize(handle, fin_action)
        except Exception as exc:  # noqa: BLE001 — surface, but still leave the mode
            err = f"finalize failed: {exc}"

    # Always restore cwd and clear the flag, even if finalize reported an error
    # (the user asked to leave; a finalize error must not trap them in the box).
    if restore_cwd:
        app.cwd = Path(str(restore_cwd))
    # Drop the registry entry whenever the worktree was actually removed — that is
    # DISCARD *and* a successful MERGE (both tear the box down). KEEP / a fail-safe
    # MERGE that kept the branch leave the entry so the box is still tracked.
    removed = bool(getattr(result, "removed", False))
    registry = getattr(app, "worktrees", None)
    if registry is not None and handle is not None and removed:
        try:
            await registry.cleanup(handle)
        except Exception:  # noqa: BLE001
            pass
    app.extra[WORKTREE_MODE_KEY] = {"active": False}
    if restore_cwd:
        app.extra["group_root"] = str(restore_cwd)

    if err is not None:
        return ExitResult(ok=False, action=action, error=err)
    return ExitResult(
        ok=True,
        action=action,
        branch=getattr(result, "branch", None),
        commit=getattr(result, "commit", None),
        removed=removed,
        note=str(getattr(result, "note", "") or ""),
    )


def _clear_plan_mode(app: "AppContext") -> None:
    """Mutual exclusion: entering worktree mode leaves plan mode (§5a.2)."""
    extra = getattr(app, "extra", None)
    if isinstance(extra, dict) and isinstance(extra.get(PLAN_MODE_KEY), dict):
        extra[PLAN_MODE_KEY] = {"active": False}


def _record_orphans(app: "AppContext", repo_root: Path) -> int:
    registry = getattr(app, "worktrees", None)
    lister = getattr(registry, "list_orphans", None)
    if not callable(lister):
        return 0
    try:
        orphans = [str(p) for p in lister(repo_root)]
    except Exception:  # noqa: BLE001 — observability only
        orphans = []
    app.extra["worktree_orphans"] = orphans
    return len(orphans)


def _resolve_base_cwd(entry_cwd: Path, base_path: str | Path | None) -> Path:
    if base_path is None or str(base_path) == "":
        return entry_cwd
    path = Path(base_path).expanduser()
    if not path.is_absolute():
        path = entry_cwd / path
    return path.resolve(strict=False)


async def _resolve_source_root(isolator: object, base_cwd: Path) -> Path:
    resolver = getattr(isolator, "repo_root", None)
    if resolver is None:
        return base_cwd.resolve(strict=False)
    root = await resolver(base_cwd)
    return Path(root).resolve(strict=False)


def _active_manual(
    registry: object, session_id: str, source_root: Path | None
) -> "IsoHandle | None":
    probe = getattr(registry, "active_manual", None)
    if not callable(probe):
        return None
    try:
        return cast("IsoHandle | None", probe(session_id, source_root))
    except TypeError:
        try:
            return cast("IsoHandle | None", probe(session_id))
        except Exception:  # noqa: BLE001 — registry probe must not abort entry
            return None
    except Exception:  # noqa: BLE001 — registry probe must not abort entry
        return None


def _worktree_cwd(handle: "IsoHandle", source_root: Path, base_cwd: Path) -> Path:
    root = Path(handle.cwd)
    if _path_within(root, base_cwd):
        return base_cwd
    try:
        rel = base_cwd.resolve(strict=False).relative_to(source_root.resolve(strict=False))
    except ValueError:
        return root
    candidate = root / rel
    return candidate if candidate.exists() else root


def _path_within(base: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except ValueError:
        return False


__all__ = [
    "WORKTREE_MODE_KEY",
    "PLAN_MODE_KEY",
    "EnterResult",
    "ExitResult",
    "enter",
    "exit",
    "is_active",
    "active_handle",
    "has_changes",
]
