"""ModelRegistry: single source of truth, alias resolution, derived tables."""

from __future__ import annotations

from autoverse.infra.llm.registry import (
    MODEL_ALIASES,
    MODEL_FALLBACK,
    MODEL_OPTIONS,
    MODEL_REGISTRY,
    ModelSpec,
    TransportSpec,
    _passthrough_spec,
    get_context_window,
    get_spec,
    resolve_alias,
)
from autoverse.types import WireKind


def test_registry_has_deepseek_and_echo() -> None:
    # Clean canonical ids follow ``<source>/<model>``. The official DeepSeek
    # direct-connect owns the bare-source ``deepseek/`` prefix; OpenRouter's
    # mirror lives under ``openrouter/``.
    assert "deepseek/deepseek-v4-pro" in MODEL_REGISTRY
    assert "openrouter/deepseek-v4-pro" in MODEL_REGISTRY
    assert "echo" in MODEL_REGISTRY

    official = MODEL_REGISTRY["deepseek/deepseek-v4-pro"]
    assert official.transports[0].wire is WireKind.OPENAI
    assert official.transports[0].endpoint == "https://api.deepseek.com/chat/completions"
    # ``wire_model`` decouples our clean id from the upstream name: the official
    # API expects the unprefixed ``deepseek-v4-pro``.
    assert official.wire_model == "deepseek-v4-pro"
    # DeepSeek auto-caches; cache must be enabled so the meter prices reported
    # cached_tokens at the cache-read rate (regression: was False).
    assert official.capabilities.cache is True

    openrouter = MODEL_REGISTRY["openrouter/deepseek-v4-pro"]
    assert (
        openrouter.transports[0].endpoint
        == "https://openrouter.ai/api/v1/chat/completions"
    )
    # OpenRouter expects the ``vendor/model`` wire name.
    assert openrouter.wire_model == "deepseek/deepseek-v4-pro"


def test_resolve_alias_is_identity_without_aliases() -> None:
    # Pure-clean naming carries zero aliases: resolve_alias is the identity
    # function (unknown names pass through to the OpenRouter fallback).
    assert resolve_alias("deepseek/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
    assert resolve_alias("UNKNOWN") == "UNKNOWN"  # passthrough


def test_get_spec() -> None:
    assert isinstance(get_spec("deepseek/deepseek-v4-pro"), ModelSpec)
    assert get_spec("nope") is None


def test_get_context_window() -> None:
    assert get_context_window("deepseek/deepseek-v4-pro") == 1_000_000
    assert get_context_window("deepseek/deepseek-v4-pro", override=10) == 10
    assert get_context_window("unknown") == 200_000


def test_model_options_only_picker_visible() -> None:
    ids = {o["model_id"] for o in MODEL_OPTIONS}
    assert "echo" not in ids  # show_in_picker=False
    assert "deepseek/deepseek-v4-pro" in ids


def test_model_fallback_table() -> None:
    # deepseek declares no fallback; echo declares none → table is empty.
    assert "deepseek/deepseek-v4-pro" not in MODEL_FALLBACK


def test_no_aliases() -> None:
    # Pure-clean naming: the alias table is empty by design.
    assert MODEL_ALIASES == {}


def test_passthrough_spec_conservative() -> None:
    spec = _passthrough_spec("custom-model")
    assert spec.model_id == "custom-model"
    assert spec.transports[0].wire is WireKind.OPENAI
    assert spec.capabilities.vision is False
    assert spec.capabilities.cache is False
    assert isinstance(spec.transports[0], TransportSpec)
