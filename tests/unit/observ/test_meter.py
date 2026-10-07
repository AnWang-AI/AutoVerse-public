"""Unit tests — Meter pricing edge cases (docs/reference/events-and-storage.md §4.7)."""

from __future__ import annotations

from autoverse.infra.observ.meter import CostBreakdown, Meter, ModelPricing, PRICING
from autoverse.types.messages import Usage


def test_pricing_seeded_from_registry() -> None:
    # Registry-known models are present in the default pricing table.
    assert "deepseek/deepseek-v4-pro" in PRICING


def test_unknown_model_cost_is_unknown() -> None:
    meter = Meter({})
    cost = meter.cost_for("nope", Usage(input_tokens=1_000_000))
    assert cost.known is False
    assert cost.total_usd == 0.0


def test_cost_breakdown_includes_cache_tokens() -> None:
    meter = Meter({"m": ModelPricing(input=2.0, output=8.0, cache_read=0.2, cache_write=2.5)})
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    cost = meter.cost_for("m", usage)
    assert cost.input_usd == 2.0
    assert cost.output_usd == 8.0
    assert cost.cache_read_usd == 0.2
    assert cost.cache_write_usd == 2.5
    assert round(cost.total_usd, 2) == 12.7


def test_openai_codec_cache_hit_not_double_billed() -> None:
    """End-to-end regression: a cache hit decoded from the OpenAI wire must be
    billed cache-read-only on the cached portion, not input + cache-read. With
    the old codec (input_tokens == full prompt_tokens) the 800k cached tokens
    would be charged at 2.0 + 0.2 per Mtok; the fix keeps buckets disjoint."""
    from autoverse.infra.llm.codec.base import DecodeState
    from autoverse.infra.llm.codec.openai import OpenAIWireCodec
    from autoverse.types import EventType

    codec = OpenAIWireCodec()
    st = DecodeState()
    chunk = {
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 800_000},
        },
    }
    events = codec.decode_chunk(chunk, st)
    usage = [e for e in events if e.type is EventType.USAGE][0].payload.usage

    meter = Meter({"m": ModelPricing(input=2.0, output=8.0, cache_read=0.2, cache_write=2.5)})
    cost = meter.cost_for("m", usage)
    # 200k fresh input @2.0 = 0.4 ; 800k cached @0.2 = 0.16 ; total 0.56.
    # (Old double-billing would have yielded 2.0 + 0.16 = 2.16.)
    assert round(cost.input_usd, 4) == 0.4
    assert round(cost.cache_read_usd, 4) == 0.16
    assert round(cost.total_usd, 4) == 0.56


def test_meter_aggregates_per_model_and_total() -> None:
    meter = Meter({"a": ModelPricing(input=1.0, output=1.0)})
    meter.record("a", Usage(input_tokens=500_000))
    meter.record("a", Usage(input_tokens=500_000))
    assert meter.usage_by_model()["a"].input_tokens == 1_000_000
    assert meter.total_usage().input_tokens == 1_000_000
    assert round(meter.total_cost().total_usd, 4) == 1.0


def test_cost_breakdown_add_combines_known_flag() -> None:
    known = CostBreakdown(input_usd=1.0)
    unknown = CostBreakdown.unknown()
    combined = known + unknown
    assert combined.known is False


def test_total_cost_keeps_known_models_when_one_is_unknown() -> None:
    """A priced model + an unpriced model: total must still count the priced
    model's cost (not silently zeroed), while flagging that an unknown was
    present (docs/reference/events-and-storage.md §4.7)."""
    meter = Meter({"m1": ModelPricing(input=2.0, output=10.0)})
    meter.record("m1", Usage(input_tokens=1_000_000, output_tokens=500_000))  # 2 + 5 = 7
    meter.record("m2", Usage(input_tokens=1_000_000))  # unpriced
    total = meter.total_cost()
    assert round(total.total_usd, 2) == 7.0  # m1 cost survives
    assert total.known is False  # but it is flagged as containing an unknown


def test_cost_breakdown_total_usd_sums_components_even_if_unknown() -> None:
    """total_usd reflects the known components rather than collapsing to 0."""
    mixed = CostBreakdown(input_usd=3.0, output_usd=4.0, known=False)
    assert mixed.total_usd == 7.0
