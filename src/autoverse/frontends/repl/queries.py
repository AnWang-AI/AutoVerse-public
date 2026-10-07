"""Read-only query helpers for the REPL command catalogue (gap3, §3.3).

These are *pure* derivations over the public ``AppContext`` slots and the
observ ``Meter`` — they never reflect ``EngineHandle`` privates (P3) and never
mutate state. Each helper degrades to ``None``/empty when the underlying slot
is absent (every ``AppContext`` slot is nullable per contracts §7.1), letting
the command handlers render a clear "not available" message instead of raising.

The command layer (``builtin_cmds``) owns presentation; this module owns the
"where does the number come from" logic, keeping the two concerns orthogonal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from autoverse.app.context import AppContext
from autoverse.infra.observ.meter import CostBreakdown, Meter
from autoverse.types.messages import Usage


@dataclass(frozen=True)
class ContextSnapshot:
    """Context-window occupancy, derived from the trace meter when reachable."""

    tokens_used: int
    window: int
    compact_threshold: int


def _meter(app: AppContext) -> Meter | None:
    """Return the public ``Meter`` if the app's trace exposes one (no privates).

    ``AppContext.trace`` is a nullable slot; ``TraceCollector`` publishes a
    public ``meter`` property (docs/reference/events-and-storage.md). We read only that public
    surface — never a ``_private`` attribute (P3).
    """
    trace = getattr(app, "trace", None)
    if trace is None:
        return None
    meter = getattr(trace, "meter", None)
    if isinstance(meter, Meter):
        return meter
    return None


def total_usage(app: AppContext) -> Usage | None:
    """Aggregate token usage across all models, or ``None`` if no meter."""
    meter = _meter(app)
    if meter is None:
        return None
    return meter.total_usage()


def usage_by_model(app: AppContext) -> dict[str, Usage]:
    """Per-model token usage, empty when no meter is reachable."""
    meter = _meter(app)
    if meter is None:
        return {}
    return meter.usage_by_model()


def cost_by_model(app: AppContext) -> dict[str, CostBreakdown]:
    """Per-model cost breakdown, empty when no meter is reachable."""
    meter = _meter(app)
    if meter is None:
        return {}
    return meter.cost_by_model()


def total_cost(app: AppContext) -> CostBreakdown | None:
    """Aggregate cost across models, or ``None`` if no meter."""
    meter = _meter(app)
    if meter is None:
        return None
    return meter.total_cost()


def context_snapshot(app: AppContext) -> ContextSnapshot | None:
    """Derive context-window occupancy from public config + meter.

    The window size and compaction threshold come from the authoritative
    ``Config`` tree; the tokens-used estimate is the meter's total input+output
    when a meter is reachable. Returns ``None`` only when both are unavailable.
    """
    config = getattr(app, "config", None)
    if config is None:
        return None
    compaction = getattr(config, "compaction", None)
    # context_window is 0 = "auto": resolve the active model's real window from
    # the registry (an explicit >0 config value is an override/cap). This keeps
    # /context and the compaction policy reporting the *same* window.
    from autoverse.infra.llm.registry import get_context_window

    override = int(getattr(compaction, "context_window", 0) or 0) or None
    window = get_context_window(getattr(app, "model", "") or "", override=override)
    threshold_ratio = float(getattr(compaction, "compact_threshold", 0.0) or 0.0)
    usage = total_usage(app)
    tokens_used = usage.total if usage is not None else 0
    return ContextSnapshot(
        tokens_used=tokens_used,
        window=window,
        compact_threshold=int(window * threshold_ratio),
    )


def secret_aliases(app: AppContext) -> list[str] | None:
    """List secret *aliases* (never values) when the provider supports it.

    Returns ``None`` when there is no provider, and an empty list when the
    provider exists but cannot enumerate aliases (env-only degradation).
    """
    secrets = getattr(app, "secrets", None)
    if secrets is None:
        return None
    lister = getattr(secrets, "list_aliases", None)
    if lister is None:
        return []
    result = cast("list[str]", lister())
    return list(result)


def session_id_of(app: AppContext) -> str | None:
    """Current session id from public AppContext state (no engine privates).

    Trace is optional, so command correctness must not depend on observability
    being enabled. EngineBuilder always publishes the id in ``app.extra``.
    """
    trace = getattr(app, "trace", None)
    sid = getattr(trace, "session_id", None) if trace is not None else None
    if sid is None:
        extra = getattr(app, "extra", None)
        if isinstance(extra, dict):
            sid = extra.get("session_id")
    return str(sid) if sid else None


__all__ = [
    "ContextSnapshot",
    "total_usage",
    "usage_by_model",
    "cost_by_model",
    "total_cost",
    "context_snapshot",
    "secret_aliases",
    "session_id_of",
]
