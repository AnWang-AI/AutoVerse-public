"""Phase 3 acceptance fixtures — core loop test doubles + make_loop factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.core.context.compaction import CompactionPolicy
from autoverse.core.context.config import CompactionConfig, RecoveryConfig
from autoverse.core.context.recovery import RecoveryPolicy
from autoverse.core.loop.turn_loop import TurnLoop
from autoverse.core.state.conversation import Conversation
from autoverse.core.workflow.stop_gate import CompletionGate, GateRegistry
from autoverse.types import ProviderCapabilities
from autoverse.types.tools import ToolCall, ToolResult

from tests._doubles import (
    FakeEngineFactory,
    FakeIsolator,
    FixedGate,
    MemStore,
    RaisingStore,
    ScriptedProvider,
    ScriptedSubEngine,
)

# Re-export doubles for tests that import from the acceptance conftest.
__all__ = [
    "FixedGate",
    "MemStore",
    "RaisingStore",
    "ScriptedProvider",
    "FakeIsolator",
    "FakeEngineFactory",
    "ScriptedSubEngine",
    "make_loop",
]


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    def of_type(self, etype: Any) -> list[Any]:
        return [e for e in self.events if e.type == etype]


async def _noop_dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
    return [ToolResult.ok(f"ran {c.name}") for c in calls]


def make_loop(
    provider: ScriptedProvider,
    *,
    session_id: str = "s1",
    sink: RecordingSink | None = None,
    gates: GateRegistry | None = None,
    dispatch: Any = None,
    compaction: CompactionPolicy | None = None,
    recovery: RecoveryPolicy | None = None,
    tasks: object | None = None,
    cwd: Path | None = None,
) -> tuple[TurnLoop, Conversation, RecordingSink, AppContext]:
    sink = sink or RecordingSink()
    store = MemStore()
    conv = Conversation(store, session_id)
    app = AppContext(
        config=Config(),
        store=store,
        cwd=cwd or Path.cwd(),
        model="test-model",
        events=sink,
        tasks=tasks,
    )
    if gates is None:
        gates = GateRegistry()
        gates.register(CompletionGate())
    compaction = compaction or CompactionPolicy(provider, CompactionConfig())
    recovery = recovery or RecoveryPolicy(RecoveryConfig())
    loop = TurnLoop(
        llm=provider,
        conversation=conv,
        events=sink,
        compaction=compaction,
        recovery=recovery,
        gates=gates,
        dispatch_tools=dispatch or _noop_dispatch,
        app=app,
    )
    return loop, conv, sink, app


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


# ── Phase 4 sub-agent fixtures ────────────────────────────────────────────────
from tests._doubles import make_git_repo  # noqa: E402

from autoverse.core.subagent.jobs import InMemoryJobStore, ResourceLock  # noqa: E402
from autoverse.core.subagent.runner import RunOutcome, SubAgentRunner  # noqa: E402
from autoverse.core.tools.context import ToolContext  # noqa: E402
from autoverse.core.tools.registry import ToolRegistry  # noqa: E402
from autoverse.core.tools.tracker import FileTracker  # noqa: E402
from autoverse.types.abort import AbortController  # noqa: E402
import autoverse.core.tools.builtin  # noqa: E402,F401  (trigger tool registration)


def make_subagent_ctx(
    *,
    cwd: Path,
    model: str = "parent-model",
    tools: ToolRegistry | None = None,
    sink: RecordingSink | None = None,
    controller: AbortController | None = None,
    spawn: Any = None,
) -> ToolContext:
    """Build a ToolContext suitable for SubAgentRunner.spawn(req, ctx)."""
    sink = sink or RecordingSink()
    controller = controller or AbortController()
    app = AppContext(
        config=Config(),
        store=MemStore(),
        cwd=cwd,
        model=model,
        events=sink,
        tools=tools if tools is not None else ToolRegistry.build(),
        spawn=spawn,
    )
    return ToolContext(
        cwd=cwd,
        permissions=_StubPermissions(),
        tracker=FileTracker(),
        signal=controller.signal,
        events=sink,
        app=app,
    )


class _StubPermissions:
    async def check(self, call: Any) -> Any: ...
    def check_sync(self, call: Any) -> Any: ...
    async def request_approval(self, call: Any) -> Any: ...
    def cancel_all(self) -> None: ...


def make_runner(
    *,
    engine_factory: Any = None,
    isolator: Any = None,
    job_store: Any = None,
    resource_lock: Any = None,
    sink: RecordingSink | None = None,
    spawn: Any = None,
) -> SubAgentRunner:
    return SubAgentRunner(
        engine_factory=engine_factory or FakeEngineFactory(RunOutcome(summary="ok")),
        isolator=isolator or FakeIsolator(),
        resource_lock=resource_lock or ResourceLock(),
        job_store=job_store or InMemoryJobStore(),
        events=sink or RecordingSink(),
        spawn=spawn,
    )


@pytest.fixture
def subagent_runner() -> SubAgentRunner:
    return make_runner()


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one initial commit (for real-worktree tests)."""
    return make_git_repo(tmp_path)
