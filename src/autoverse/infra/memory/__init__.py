"""Project memory assembly (docs/reference/config.md).

:class:`MemoryLoader` discovers the memory files (``CLAUDE.md`` /
``AGENTS.md``) from ``cwd`` only (no upward traversal) and assembles them into an
annotated prompt-append string. The FTS5 retrieval classes are optional helpers;
the default EngineBuilder prompt path still uses full cwd-only memory assembly.

This subsystem lives in ``infra`` and is never imported by ``core`` (P3).
"""

from __future__ import annotations

from autoverse.infra.memory.loader import (
    AssembledMemory,
    MemoryBlock,
    MemoryLoader,
)
from autoverse.infra.memory.retrieval import (
    FTS5_AVAILABLE,
    Fts5RetrievalMemory,
    RetrievedMemory,
    should_use_retrieval,
)

__all__ = [
    "AssembledMemory",
    "MemoryBlock",
    "MemoryLoader",
    "FTS5_AVAILABLE",
    "Fts5RetrievalMemory",
    "RetrievedMemory",
    "should_use_retrieval",
]
