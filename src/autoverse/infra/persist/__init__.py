"""Persistence subsystem (docs/reference/events-and-storage.md).

Concrete ``Store`` Port adapters (``JSONLStore``/``RedisStore``) plus task,
job and busy-lock stores. ``core`` only depends on the ``ports.Store``
Protocol; these adapters live in ``infra`` and are injected by the assembly
layer (P3).
"""

from __future__ import annotations

from autoverse.infra.persist.compaction_archive import (
    FileCompactionArchive,
    InMemoryCompactionArchive,
)
from autoverse.infra.persist.turn_index import FileTurnIndexStore, InMemoryTurnIndexStore

from autoverse.infra.persist.busy_lock import BusyLock
from autoverse.infra.persist.job_store import (
    InvalidTransition,
    Job,
    JobStatus,
    JobStore,
)
from autoverse.infra.persist.jsonl_store import JSONLStore, LoadReport
from autoverse.infra.persist.session_catalog import SQLiteSessionCatalog
from autoverse.infra.persist.serialize import (
    block_from_dict,
    block_to_dict,
    msg_from_dict,
    msg_to_dict,
)
from autoverse.infra.persist.task_store import TaskStore, TodoItem

__all__ = [
    "JSONLStore",
    "LoadReport",
    "SQLiteSessionCatalog",
    "BusyLock",
    "JobStore",
    "Job",
    "JobStatus",
    "InvalidTransition",
    "TaskStore",
    "TodoItem",
    "block_to_dict",
    "block_from_dict",
    "msg_to_dict",
    "msg_from_dict",
    "FileCompactionArchive",
    "InMemoryCompactionArchive",
    "FileTurnIndexStore",
    "InMemoryTurnIndexStore",
]
