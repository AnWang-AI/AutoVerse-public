"""Workspace snapshot subsystem (docs/reference/tools.md §9.4, KDR-12).

A concrete :class:`~autoverse.ports.snapshot.SnapshotPort` adapter backed by a
*shadow* git directory: it captures/restores the work-tree without touching the
user's own ``.git`` history (a separate ``GIT_DIR`` with the work-tree pointed at
``cwd``). ``core`` only depends on the ``SnapshotPort`` Protocol; this adapter
lives in ``infra`` and is injected by the assembly layer (P3).
"""

from __future__ import annotations

from autoverse.infra.snapshot.git_shadow import GitShadowSnapshot

__all__ = ["GitShadowSnapshot"]
