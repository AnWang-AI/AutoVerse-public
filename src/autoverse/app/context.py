"""AppContext — explicit DI container (docs/architecture/contracts.md §7.1).

All session-level shared state is collected here, created and injected by the
EngineBuilder at assembly time. No global mutable singletons (P4).

Optional fields use `object | None` placeholders; concrete interfaces are
defined in their subsystem docs and narrowed by those subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from autoverse.app.config import Config
from autoverse.ports import (
    BrowserPort,
    CompactionArchivePort,
    EventSink,
    SecretProvider,
    SessionCatalogPort,
    Store,
)

if TYPE_CHECKING:
    from autoverse.ports.isolation import Isolator, WorktreeRegistry
    from autoverse.ports.snapshot import SnapshotPort

SpawnFn = Callable[..., Awaitable[Any]]


@dataclass
class AppContext:
    config: Config
    store: Store
    cwd: Path
    model: str
    events: EventSink
    tools: object | None = None          # ToolRegistry (docs/reference/tools.md)
    prompt: object | None = None         # PromptAssembler (docs/architecture/runtime-flow.md)
    secrets: SecretProvider | None = None
    tasks: object | None = None          # TaskStore (docs/reference/events-and-storage.md)
    jobs: object | None = None           # InMemoryJobStore by default (events-and-storage.md)
    worktrees: "WorktreeRegistry | None" = None  # WorktreeRegistry (docs/reference/subagents.md)
    isolator: "Isolator | None" = None           # Isolator (docs/reference/subagents.md)
    goal: object | None = None           # GoalState (docs/reference/workflows.md)
    trace: object | None = None          # TraceCollector (docs/reference/events-and-storage.md)
    context_archive: CompactionArchivePort | None = None
    session_catalog: SessionCatalogPort | None = None
    browser: BrowserPort | None = None   # BrowserPort (docs/reference/tools.md §12)
    snapshot: "SnapshotPort | None" = None       # SnapshotPort (docs/reference/tools.md §9.4)
    image_backend: object | None = None  # image-gen backend (docs/reference/tools.md §11)
    memory: object | None = None         # AssembledMemory (memory/loader.py)
    extension_sources: tuple[object, ...] = ()
    spawn: SpawnFn | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def model_serviceable(self, model_id: str) -> bool:
        """Best-effort: does some transport of ``model_id`` have a usable credential?

        Network-free pre-flight over the model registry + this context's
        ``secrets`` (env → SecretProvider). The sub-agent runner calls this (via
        duck typing, so ``core`` keeps no import edge to the LLM registry) to
        avoid pinning a tier to a provider whose key is missing — which would
        otherwise 401 the whole spawn. Fail-safe: any error → ``True`` (assume
        serviceable; this guard must never be the reason a spawn is blocked)."""
        if not model_id:
            return True
        try:
            from autoverse.infra.llm.registry import model_credentials_available
            from autoverse.infra.llm.transport.credential import CredentialResolver

            resolver = CredentialResolver(self.secrets)
            return model_credentials_available(model_id, resolver)
        except Exception:  # noqa: BLE001 — never block a spawn on this heuristic
            return True


__all__ = ["AppContext", "SpawnFn"]
