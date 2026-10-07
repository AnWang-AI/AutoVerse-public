"""Read-only session-listing Port (docs/architecture/frontend-adapters.md §4.2 / §9.2).

A narrow, read-only query contract — **separate** from :class:`EngineHandle`
(its driving facade is per-session; listing across sessions is orthogonal, so
mixing the two would bloat ``EngineHandle``'s stable surface, §4.2). The TUI /
Web session rails depend on this Protocol only; concrete adapters live in
``app/`` / ``infra/`` and are injected via ``AppContext`` (P3).

``SessionSummary`` is a pure derived snapshot (recomputable from ``Store``):
``message_count`` is the display prompt count (user-role text messages, not tool
result messages), ``status`` = the single highest-precedence display state
(§5.6.2), ``unseen`` = an orthogonal attention bit (§5.6.5). Those three are
derived rather than persisted ``SessionMeta`` fields (P1); ``pinned`` is a
projection of the current persisted metadata snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# The single displayable main status (§5.6.1). One session → one main state (P1);
# the app merges multiple live signals down to the highest-precedence one per the
# urgency order in §5.6.2:
#   awaiting_input > running > error_unseen > completed_unseen > busy_external > idle
SessionStatus = Literal[
    "idle",
    "running",
    "awaiting_input",
    "completed_unseen",
    "error_unseen",
    "busy_external",
]

# Display precedence (highest urgency first) — §5.6.2. Lower index = wins.
STATUS_PRECEDENCE: tuple[SessionStatus, ...] = (
    "awaiting_input",
    "running",
    "error_unseen",
    "completed_unseen",
    "busy_external",
    "idle",
)


def merge_status(*candidates: SessionStatus) -> SessionStatus:
    """Collapse several concurrent signals to the single highest-urgency status.

    Pure helper realising §5.6.2's precedence so both the Phase-1 app-side merge
    and any Phase-2 ``SessionSupervisor`` reduce to the same rule (P1)."""
    rank = {s: i for i, s in enumerate(STATUS_PRECEDENCE)}
    best: SessionStatus = "idle"
    best_rank = rank["idle"]
    for c in candidates:
        r = rank.get(c, rank["idle"])
        if r < best_rank:
            best_rank = r
            best = c
    return best


@dataclass(frozen=True)
class SessionSummary:
    """One session's display snapshot (a derived quantity, recomputable)."""

    session_id: str
    title: str = ""            # SessionMeta.title; caller falls back when empty (§9.1)
    model: str = ""
    cwd: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    message_count: int = 0     # display prompt count (user text messages), not a meta cache
    status: SessionStatus = "idle"  # merged single main state (§5.6.1, already §5.6.2-max)
    unseen: bool = False       # attention bit: last_activity_at > last_viewed_at (§5.6.5)
    pinned: bool = False       # rail projection from the same cached SessionMeta snapshot
    # The originating-project root used as the grouping key for worktree sessions
    # (§6.3.1); empty ⇒ the rail normalises ``cwd`` itself.
    group_root: str = ""


@runtime_checkable
class SessionListQuery(Protocol):
    """Read-only session enumeration (≤12-method read budget — uses 3, §4.2)."""

    async def list(self) -> list[SessionSummary]:
        """All session summaries, sorted by ``updated_at`` descending.

        Sorting is the implementation's job; the frontend only renders (§4.2)."""
        ...

    async def delete(self, session_id: str) -> None:
        """Delete a session (cascade of trace/tasks/jobs is the impl's call)."""
        ...

    async def rename(self, session_id: str, title: str) -> None:
        """Write ``SessionMeta.title`` (via ``Store.update_meta``, atomic line 0)."""
        ...


__all__ = [
    "SessionStatus",
    "STATUS_PRECEDENCE",
    "merge_status",
    "SessionSummary",
    "SessionListQuery",
]
