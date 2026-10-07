"""Meter — token + cost aggregation by model pricing (docs/reference/events-and-storage.md §4.7, O2).

Costs are computed from ``Usage`` (input/output/cache tokens) times a per-model
pricing table (USD per 1M tokens). Pricing defaults are seeded from the LLM
``ModelRegistry`` so the trace subsystem does not maintain a *second* source of
truth (P1) — but the table is injectable for testing and unknown models report
``None`` rather than fabricating a price.
"""

from __future__ import annotations

from dataclasses import dataclass

from autoverse.types.messages import Usage


@dataclass(frozen=True)
class ModelPricing:
    """Per-1M-token prices in USD. Cache prices default to input price."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


def _default_pricing() -> dict[str, ModelPricing]:
    """Seed the pricing table from the registry's known model specs."""
    table: dict[str, ModelPricing] = {}
    try:
        from autoverse.infra.llm.registry import _SPECS
    except Exception:  # pragma: no cover - registry optional at meter import
        return table
    for spec in _SPECS:
        # Honour explicit cache prices when the spec publishes them (e.g. the
        # official DeepSeek prefix cache); otherwise fall back to the heuristic
        # (cache_read = input×0.1, cache_write = input×1.25).
        cache_read = spec.cache_read_price_per_mtok
        if cache_read is None:
            cache_read = spec.input_price_per_mtok * 0.1
        cache_write = spec.cache_write_price_per_mtok
        if cache_write is None:
            cache_write = spec.input_price_per_mtok * 1.25
        table[spec.model_id] = ModelPricing(
            input=spec.input_price_per_mtok,
            output=spec.output_price_per_mtok,
            cache_read=cache_read,
            cache_write=cache_write,
        )
        for alias in spec.aliases:
            table[alias] = table[spec.model_id]
    return table


#: Default pricing table seeded from the model registry.
PRICING: dict[str, ModelPricing] = _default_pricing()


@dataclass(frozen=True)
class CostBreakdown:
    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_write_usd: float = 0.0
    known: bool = True

    @property
    def total_usd(self) -> float:
        # Sum the *known* components regardless of ``known``: an unpriced model
        # contributes zero-valued components (see ``unknown``), so it must not
        # zero out the cost of priced models when breakdowns are combined.
        # ``known`` separately flags "does this total contain an unknown part".
        return (
            self.input_usd + self.output_usd + self.cache_read_usd + self.cache_write_usd
        )

    @classmethod
    def unknown(cls) -> "CostBreakdown":
        return cls(known=False)

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            input_usd=self.input_usd + other.input_usd,
            output_usd=self.output_usd + other.output_usd,
            cache_read_usd=self.cache_read_usd + other.cache_read_usd,
            cache_write_usd=self.cache_write_usd + other.cache_write_usd,
            known=self.known and other.known,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "input_usd": self.input_usd,
            "output_usd": self.output_usd,
            "cache_read_usd": self.cache_read_usd,
            "cache_write_usd": self.cache_write_usd,
            "total_usd": self.total_usd,
            "known": self.known,
        }


class Meter:
    """Aggregates token usage + cost, per model and total (O2)."""

    def __init__(self, pricing: dict[str, ModelPricing] | None = None) -> None:
        self._pricing = pricing if pricing is not None else dict(PRICING)
        self._by_model: dict[str, Usage] = {}

    def record(self, model: str, usage: Usage) -> None:
        self._by_model[model] = self._by_model.get(model, Usage()) + usage

    def pricing(self, model: str) -> ModelPricing | None:
        return self._pricing.get(model)

    def cost_for(self, model: str, usage: Usage) -> CostBreakdown:
        price = self._pricing.get(model)
        if price is None:
            return CostBreakdown.unknown()
        return CostBreakdown(
            input_usd=usage.input_tokens / 1e6 * price.input,
            output_usd=usage.output_tokens / 1e6 * price.output,
            cache_read_usd=usage.cache_read_tokens / 1e6 * price.cache_read,
            cache_write_usd=usage.cache_write_tokens / 1e6 * price.cache_write,
        )

    def usage_by_model(self) -> dict[str, Usage]:
        return dict(self._by_model)

    def total_usage(self) -> Usage:
        total = Usage()
        for usage in self._by_model.values():
            total = total + usage
        return total

    def cost_by_model(self) -> dict[str, CostBreakdown]:
        return {m: self.cost_for(m, u) for m, u in self._by_model.items()}

    def total_cost(self) -> CostBreakdown:
        total = CostBreakdown()
        for model, usage in self._by_model.items():
            total = total + self.cost_for(model, usage)
        return total


__all__ = ["Meter", "CostBreakdown", "ModelPricing", "PRICING"]
