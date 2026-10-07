"""Shared test doubles for Phase 3 (core loop) acceptance + unit tests.

Re-exported by tests/conftest.py and tests/acceptance/conftest.py so both
acceptance and unit suites can build them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from autoverse.ports import SessionMeta
from autoverse.ports.isolation import (
    FinalizeAction,
    IsoHandle,
    IsoResult,
    IsoSpec,
    IsolationError,
)
from autoverse.types import (
    AbortSignal,
    LLMRequest,
    ProviderCapabilities,
    StreamEvent,
)
from autoverse.types.messages import Message
from autoverse.core.subagent.runner import RunOutcome
from autoverse.core.workflow.stop_gate import GateDecision, Verdict


class MemStore:
    """In-memory Store implementing all 9 protocol methods + a rewrite log."""

    def __init__(self) -> None:
        self._sessions: dict[str, list[Message]] = {}
        self._meta: dict[str, SessionMeta] = {}
        self.rewrite_log: list[tuple[str, int, str]] = []

    async def append(self, session_id: str, *msgs: Message) -> None:
        self._sessions.setdefault(session_id, []).extend(msgs)

    async def load(self, session_id: str, *, since: int = 0) -> list[Message]:
        return list(self._sessions.get(session_id, []))[since:]

    async def rewrite(
        self, session_id: str, msgs: list[Message], reason: str
    ) -> None:
        self._sessions[session_id] = list(msgs)
        self.rewrite_log.append((session_id, len(msgs), reason))

    async def create(self, session_id: str, meta: SessionMeta) -> None:
        self._sessions.setdefault(session_id, [])
        self._meta[session_id] = meta

    async def load_meta(self, session_id: str) -> SessionMeta:
        return self._meta.get(session_id, SessionMeta(session_id=session_id))

    async def update_meta(self, session_id: str, **fields: object) -> None:
        meta = self._meta.setdefault(session_id, SessionMeta(session_id=session_id))
        for k, v in fields.items():
            setattr(meta, k, v)

    async def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._meta.pop(session_id, None)


class RaisingStore(MemStore):
    """MemStore whose append always raises (used to prove store-first, L11)."""

    async def append(self, session_id: str, *msgs: Message) -> None:
        raise RuntimeError("disk full")


class ScriptedProvider:
    """LLMProvider double that yields a pre-scripted list of events per stream.

    ``scripts`` is a list of "turns"; each turn is a list of StreamEvents that
    will be yielded on successive ``stream()`` calls. Exhausting the scripts
    replays the final turn. A turn entry may also be an ``Exception`` instance,
    in which case ``stream`` raises it (after honouring the abort signal).
    """

    def __init__(
        self,
        scripts: list[object],
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self._scripts = scripts
        self._idx = 0
        self._capabilities = capabilities or ProviderCapabilities()
        self.requests: list[LLMRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _next_turn(self) -> object:
        if self._idx < len(self._scripts):
            turn = self._scripts[self._idx]
            self._idx += 1
            return turn
        return self._scripts[-1] if self._scripts else []

    async def stream(
        self, req: LLMRequest, signal: AbortSignal
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        signal.raise_if_aborted()
        turn = self._next_turn()
        if isinstance(turn, Exception):
            raise turn
        assert isinstance(turn, list)
        for ev in turn:
            signal.raise_if_aborted()
            if isinstance(ev, Exception):
                raise ev
            assert isinstance(ev, StreamEvent)
            yield ev

    async def count_tokens(self, req: LLMRequest) -> int:
        return max(1, len(req.system) // 4)

    async def aclose(self) -> None:
        return None


class FixedGate:
    """A StopGate that always returns a preset GateDecision."""

    def __init__(
        self,
        gate_id: str,
        verdict: Verdict,
        *,
        injected_message: Message | None = None,
        reason: str = "",
    ) -> None:
        self._id = gate_id
        self._decision = GateDecision(
            gate_id=gate_id,
            verdict=verdict,
            injected_message=injected_message,
            reason=reason,
        )

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, view: object, app: object) -> GateDecision:
        return self._decision


class FakeIsolator:
    """Scriptable Isolator double (docs/reference/subagents.md §13 / docs/reference/subagents.md §9).

    Records the call order so tests can assert "audit before finalize" (S8).
    ``scope_error`` makes ``scope`` raise IsolationError (S6 fail-closed).
    ``audit_tainted`` makes ``audit`` return a tainted verdict (S8).
    """

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        source_root: Path | None = None,
        scope_error: bool = False,
        audit_tainted: bool = False,
        dirty: bool = False,
    ) -> None:
        self._cwd = cwd or Path("/tmp/fake-worktree")
        self._source_root = source_root
        self._scope_error = scope_error
        self._audit_tainted = audit_tainted
        self._dirty = dirty
        self.calls: list[str] = []
        self.base_cwds: list[Path] = []

    async def repo_root(self, base_cwd: Path) -> Path:
        self.calls.append("repo_root")
        return self._source_root or Path(base_cwd)

    @asynccontextmanager
    async def scope(self, spec: IsoSpec, base_cwd: Path) -> AsyncIterator[IsoHandle]:
        self.calls.append("scope")
        self.base_cwds.append(Path(base_cwd))
        if self._scope_error:
            raise IsolationError("fake worktree creation failed")
        handle = IsoHandle(
            cwd=self._cwd,
            branch="autoverse/wt/fake",
            base_commit="base0",
            source_root=self._source_root or Path(base_cwd),
        )
        try:
            yield handle
        finally:
            self.calls.append("cleanup")

    async def audit(self, iso: IsoHandle, policy: str | None) -> tuple[bool, str | None]:
        self.calls.append("audit")
        if self._audit_tainted and policy == "git_diff_must_be_empty":
            return True, "[VERIFICATION_TAINTED] tracked source modified: ['src/x.py']"
        return False, None

    async def has_changes(self, iso: IsoHandle) -> bool:
        self.calls.append("has_changes")
        return self._dirty

    async def has_uncommitted_changes(self, iso: IsoHandle) -> bool:
        self.calls.append("has_uncommitted_changes")
        return self._dirty

    async def finalize(self, iso: IsoHandle, action: FinalizeAction) -> IsoResult:
        self.calls.append("finalize")
        # Mirror the real isolator's "removed" semantics: DISCARD and a (here
        # always-successful) MERGE tear the box down; KEEP/AUTOCOMMIT do not.
        removed = action in (FinalizeAction.DISCARD, FinalizeAction.MERGE)
        note = {
            FinalizeAction.DISCARD: "discarded",
            FinalizeAction.MERGE: "merged",
            FinalizeAction.KEEP: "kept",
        }.get(action, "")
        return IsoResult(branch=iso.branch, commit="commit-sha", removed=removed, note=note)

    async def prune(self, repo_root: Path) -> int:
        self.calls.append("prune")
        return 1


class ScriptedSubEngine:
    """A child engine double that returns a scripted RunOutcome.

    Used by FakeEngineFactory. Captures the construction kwargs so tests can
    assert what the runner passed (cwd, tools, bash_check_mode, ...).
    """

    def __init__(self, outcome: RunOutcome, *, hang: bool = False, **kwargs: Any) -> None:
        self.outcome = outcome
        self.hang = hang
        self.kwargs = kwargs

    async def run(self) -> RunOutcome:
        if self.hang:
            import asyncio

            await asyncio.sleep(3600)
        return self.outcome


class FakeEngineFactory:
    """Factory double: returns a ScriptedSubEngine and records every construction."""

    def __init__(
        self,
        outcome: RunOutcome | None = None,
        *,
        hang: bool = False,
    ) -> None:
        self._outcome = outcome or RunOutcome(summary="done")
        self._hang = hang
        self.constructions: list[dict[str, Any]] = []
        self.last: ScriptedSubEngine | None = None

    def __call__(self, **kwargs: Any) -> ScriptedSubEngine:
        self.constructions.append(dict(kwargs))
        engine = ScriptedSubEngine(self._outcome, hang=self._hang, **kwargs)
        self.last = engine
        return engine


def make_git_repo(root: Path) -> Path:
    """Create a throwaway git repo with one commit under ``root``; return its path."""
    import subprocess

    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "test@autoverse.local")
    git("config", "user.name", "autoverse-test")
    git("config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return repo


__all__ = [
    "MemStore",
    "RaisingStore",
    "ScriptedProvider",
    "FixedGate",
    "FakeIsolator",
    "ScriptedSubEngine",
    "FakeEngineFactory",
    "make_git_repo",
]
