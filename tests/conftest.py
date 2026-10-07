"""Shared fixtures for Phase 2 tool/permission tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.infra.diagnostics.null import NullDiagnostics
from autoverse.infra.permission.fixed import AllowAllPermissionAdapter
from autoverse.ports import PermissionPort
from autoverse.types.abort import AbortController
from autoverse.types.events import StreamEvent

from tests._doubles import FixedGate, MemStore, RaisingStore, ScriptedProvider

__all__ = ["FixedGate", "MemStore", "RaisingStore", "ScriptedProvider"]


class RecordingSink:
    """EventSink that records every emitted event for assertions."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    async def emit(self, event: StreamEvent) -> None:
        self.events.append(event)

    def of_type(self, etype: Any) -> list[StreamEvent]:
        return [e for e in self.events if e.type == etype]


@pytest.fixture
def recording_sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def tmp_workdir(tmp_path: Path) -> Path:
    return tmp_path


def make_ctx(
    cwd: Path,
    *,
    sink: RecordingSink | None = None,
    permissions: PermissionPort | None = None,
    controller: AbortController | None = None,
    isolation_root: Path | None = None,
    isolation_source_root: Path | None = None,
) -> ToolContext:
    sink = sink or RecordingSink()
    controller = controller or AbortController()
    perms: PermissionPort = permissions or AllowAllPermissionAdapter()
    app = AppContext(
        config=Config(),
        store=_DummyStore(),
        cwd=cwd,
        model="test-model",
        events=sink,
    )
    return ToolContext(
        cwd=cwd,
        permissions=perms,
        tracker=FileTracker(),
        signal=controller.signal,
        events=sink,
        app=app,
        diagnostics=NullDiagnostics(),
        isolation_root=isolation_root,
        isolation_source_root=isolation_source_root,
    )


class _DummyStore:
    async def append(self, *a: Any, **k: Any) -> None: ...
    async def load(self, *a: Any, **k: Any) -> list[Any]:
        return []
    async def rewrite(self, *a: Any, **k: Any) -> None: ...
    async def create(self, *a: Any, **k: Any) -> None: ...
    async def load_meta(self, *a: Any, **k: Any) -> Any: ...
    async def update_meta(self, *a: Any, **k: Any) -> None: ...
    async def list_sessions(self) -> list[str]:
        return []
    async def delete(self, *a: Any, **k: Any) -> None: ...


@pytest.fixture
def make_tool_ctx() -> Any:
    return make_ctx


@pytest.fixture(autouse=True, scope="session")
def _hermetic_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep the suite hermetic after gap10 made the engine persist by default.

    ``EngineBuilder`` now defaults to a ``JSONLStore`` under ``Config.session_dir``
    (``~/.autoverse/...``), an encrypted secret vault and a per-session trace
    store. Redirect ``$HOME`` to a throwaway tmp dir so building an engine never
    writes into the real developer ``$HOME``. Tests that pass explicit paths are
    unaffected — this only changes the *default* locations.
    """
    home = tmp_path_factory.mktemp("home")
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)  # Windows parity
    for key in [k for k in os.environ if k.startswith("AUTOVERSE_")]:
        del os.environ[key]
    Path(home, ".autoverse").mkdir(parents=True, exist_ok=True)
