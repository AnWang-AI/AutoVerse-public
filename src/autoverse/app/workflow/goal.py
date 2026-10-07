"""GoalState / GoalEvaluator — declarative goal-driven termination (docs/reference/workflows.md §6).

A ``/goal`` workflow externalises the "am I done?" judgement from the model's
self-perception into an *independent judge* that re-checks a declarative
completion condition each turn (§6.1). The judge is the ``EXTERNAL_JUDGE``
termination layer (§5).

Two things this module is careful about:

* **No global singleton** (P4): :class:`GoalStore` is a plain object held by
  ``AppContext.goal`` and injected, not a module-level instance.
* **Judge-fault circuit breaker** (§6.4): a judge that *malfunctions* (timeout /
  5xx / bad JSON) is strictly distinguished from a judge that says "not yet".
  The former must never be fed back as negative feedback (P5); after
  ``MAX_CONSECUTIVE_JUDGE_ERRORS`` consecutive faults the breaker trips, the goal
  is auto-cleared, and the user is told — rather than burning tokens forever.

``app`` may import ``core``; this module imports only ``app.workflow.registry``
for the breaker constant and stdlib.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol, Sequence, runtime_checkable

from autoverse.app.workflow.registry import MAX_CONSECUTIVE_JUDGE_ERRORS
from autoverse.ports import Store

MAX_CONDITION_LENGTH = 4000
CLEAR_ALIASES = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})


@dataclass
class GoalEntry:
    """Per-session active goal + breaker bookkeeping (§6.2)."""

    condition: str
    session_id: str
    # Wall time is intentionally persisted; monotonic clocks cannot survive a
    # process restart or cold session resume.
    started_at: float = field(default_factory=time.time)
    turns_evaluated: int = 0
    tokens_spent: int = 0
    last_reason: str = ""
    achieved: bool = False
    # Increments only on judge *malfunction*; any normal verdict resets it (§6.4).
    consecutive_judge_errors: int = 0


class GoalStore:
    """At most one active goal per session, persisted through SessionMeta.

    The object remains session-local and dependency-injected (no process-global
    singleton), while ``SessionMeta.extra["goal_state"]`` is the durable truth
    used by cold resume and session switching.
    """

    def __init__(
        self,
        max_consecutive_errors: int = MAX_CONSECUTIVE_JUDGE_ERRORS,
        *,
        store: Store | None = None,
    ) -> None:
        self._active: dict[str, GoalEntry] = {}
        self._last_achieved: dict[str, GoalEntry] = {}
        self._max_errors = max_consecutive_errors
        self._store = store

    def set(self, session_id: str, condition: str) -> GoalEntry:
        condition = condition.strip()
        if not condition:
            raise ValueError("Goal condition cannot be empty.")
        if len(condition) > MAX_CONDITION_LENGTH:
            raise ValueError(
                f"Goal condition is too long ({len(condition)} characters; "
                f"maximum {MAX_CONDITION_LENGTH})."
            )
        entry = GoalEntry(condition=condition, session_id=session_id)
        self._active[session_id] = entry
        return entry

    def get(self, session_id: str) -> GoalEntry | None:
        return self._active.get(session_id)

    def last_achieved(self, session_id: str) -> GoalEntry | None:
        return self._last_achieved.get(session_id)

    def clear(self, session_id: str) -> GoalEntry | None:
        return self._active.pop(session_id, None)

    def mark_achieved(self, session_id: str, reason: str = "") -> GoalEntry | None:
        entry = self._active.pop(session_id, None)
        if entry is None:
            return None
        entry.achieved = True
        if reason:
            entry.last_reason = reason
        self._last_achieved[session_id] = entry
        return entry

    def format_status(self, session_id: str) -> str:
        entry = self.get(session_id)
        if entry is not None:
            elapsed = max(0, int(time.time() - entry.started_at))
            return (
                f"goal: active · {entry.condition}\n"
                f"{entry.turns_evaluated} evaluations · {elapsed}s"
            )
        achieved = self.last_achieved(session_id)
        if achieved is not None:
            return f"goal: achieved · {achieved.condition}"
        return "goal: none set"

    def restore(self, session_id: str, raw: object) -> None:
        """Restore one session's active/last goal from a JSON-safe snapshot."""
        self._active.pop(session_id, None)
        self._last_achieved.pop(session_id, None)
        if not isinstance(raw, Mapping):
            return
        active = self._entry_from_dict(raw.get("active"), session_id)
        achieved = self._entry_from_dict(raw.get("last_achieved"), session_id)
        if active is not None and not active.achieved:
            self._active[session_id] = active
        if achieved is not None:
            achieved.achieved = True
            self._last_achieved[session_id] = achieved

    async def persist(self, session_id: str) -> None:
        if self._store is None:
            return
        try:
            await self._store.update_meta(
                session_id,
                goal_state={
                    "active": self._entry_to_dict(self.get(session_id)),
                    "last_achieved": self._entry_to_dict(
                        self.last_achieved(session_id)
                    ),
                },
            )
        except Exception:  # noqa: BLE001 — goal metadata must not crash the active turn
            return

    @staticmethod
    def _entry_to_dict(entry: GoalEntry | None) -> dict[str, object] | None:
        if entry is None:
            return None
        return {
            "condition": entry.condition,
            "session_id": entry.session_id,
            "started_at": entry.started_at,
            "turns_evaluated": entry.turns_evaluated,
            "tokens_spent": entry.tokens_spent,
            "last_reason": entry.last_reason,
            "achieved": entry.achieved,
            "consecutive_judge_errors": entry.consecutive_judge_errors,
        }

    @staticmethod
    def _entry_from_dict(raw: object, session_id: str) -> GoalEntry | None:
        if not isinstance(raw, Mapping):
            return None
        condition = str(raw.get("condition", "")).strip()
        if not condition:
            return None
        return GoalEntry(
            condition=condition,
            session_id=session_id,
            started_at=float(raw.get("started_at", time.time())),
            turns_evaluated=int(raw.get("turns_evaluated", 0)),
            tokens_spent=int(raw.get("tokens_spent", 0)),
            last_reason=str(raw.get("last_reason", "")),
            achieved=bool(raw.get("achieved", False)),
            consecutive_judge_errors=int(raw.get("consecutive_judge_errors", 0)),
        )

    def record_judgement(self, session_id: str, *, is_judge_error: bool) -> bool:
        """Fold one judge outcome into the breaker; return ``True`` iff it tripped.

        A tripped breaker auto-clears the goal (§6.4). A normal verdict (achieved
        or not) resets the consecutive-error counter to zero.
        """
        entry = self._active.get(session_id)
        if entry is None:
            return False
        if not is_judge_error:
            entry.consecutive_judge_errors = 0
            return False
        entry.consecutive_judge_errors += 1
        if entry.consecutive_judge_errors >= self._max_errors:
            self.clear(session_id)
            return True
        return False


@dataclass(frozen=True)
class JudgeResult:
    """Structured judge verdict (§6.3). ``is_judge_error`` ≠ "not achieved"."""

    ok: bool                       # True only when the judge is confident it's done
    reason: str = ""               # short reason when ok is False
    tokens_used: int = 0
    is_judge_error: bool = False   # True = judge malfunction (timeout/5xx/bad JSON)


# A judge call: (condition, transcript) -> JudgeResult. The default is LLM-backed
# but any callable satisfies the contract, so the evaluator is trivially testable.
JudgeFn = Callable[[str, Sequence[object]], Awaitable[JudgeResult]]


@runtime_checkable
class Judge(Protocol):
    async def evaluate(self, condition: str, transcript: Sequence[object]) -> JudgeResult: ...


class GoalEvaluator:
    """Wraps a judge call so any raised exception becomes ``is_judge_error=True``.

    This is the one place that converts "the judge blew up" into a *typed* result
    rather than letting it propagate as a negative verdict (§6.4 / P5). It does
    NOT itself own breaker state — that lives in :class:`GoalStore` so per-session
    bookkeeping has a single home.
    """

    def __init__(self, judge: JudgeFn | None) -> None:
        self._judge = judge

    async def evaluate(self, condition: str, transcript: Sequence[object]) -> JudgeResult:
        if self._judge is None:
            # No judge wired → treat as malfunction, never as "not achieved".
            return JudgeResult(ok=False, reason="no judge configured", is_judge_error=True)
        try:
            return await self._judge(condition, transcript)
        except Exception as exc:  # noqa: BLE001 — typed as judge-fault, not negative verdict
            return JudgeResult(
                ok=False, reason=f"judge error: {exc}", is_judge_error=True
            )


__all__ = [
    "GoalEntry",
    "GoalStore",
    "JudgeResult",
    "JudgeFn",
    "Judge",
    "GoalEvaluator",
    "MAX_CONSECUTIVE_JUDGE_ERRORS",
    "MAX_CONDITION_LENGTH",
    "CLEAR_ALIASES",
]
