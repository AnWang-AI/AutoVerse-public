"""ModelRegistry — the single source of truth for logical models (docs/reference/models.md §3.2).

``ModelSpec`` and ``TransportSpec`` are authoritatively defined here (the old
simplified ``ModelSpec`` in ``types/llm.py`` has been removed). Context window,
picker options, routing/fallback tables are all derived from ``_SPECS`` — to add
a model, change this file only (P1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autoverse.infra.llm.transport.credential import (
    Credential,
    CredentialResolver,
    all_of,
    bearer,
    header,
    none,
)
from autoverse.types import ProviderCapabilities, WireKind


@dataclass(frozen=True)
class TransportSpec:
    """One concrete channel: endpoint + auth + which wire dialect it speaks."""

    id: str
    wire: WireKind
    auth: Credential
    endpoint: str
    real_streaming: bool = True
    node_pinning: bool = False


@dataclass(frozen=True)
class ModelSpec:
    """All facts of one logical model — the single source of truth.

    ``wire`` is not a top-level field: it travels with each ``TransportSpec``
    (a model may carry multiple transports of different wires).
    """

    model_id: str
    label: str
    description: str
    # The clean ``model_id`` above is our internal canonical name (``<source>/
    # <model>``, shown in the picker/sidebar and typed by users). ``wire_model``
    # is what actually goes on the wire as the ``model`` field — it carries the
    # upstream-specific name (OpenRouter's ``vendor/model``, a gateway's bare id).
    # ``None`` means "send ``model_id`` verbatim" (used by passthrough/echo).
    wire_model: str | None = None
    context_window: int = 200_000
    aliases: tuple[str, ...] = ()
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    transports: tuple[TransportSpec, ...] = ()
    fallback_model_id: str | None = None
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    # Explicit prompt-cache prices (USD per 1M tokens). ``None`` means "derive
    # from input price heuristically" (cache_read = input×0.1, write = input×1.25
    # — see meter._default_pricing). Set these for backends that publish real
    # cache rates, e.g. official DeepSeek's automatic prefix cache.
    cache_read_price_per_mtok: float | None = None
    cache_write_price_per_mtok: float | None = None
    show_in_picker: bool = True


_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        model_id="openrouter/deepseek-v4-pro",
        wire_model="deepseek/deepseek-v4-pro",
        label="DeepSeek V4 Pro (OpenRouter)",
        description="DeepSeek V4 Pro via OpenRouter (openai wire, bearer).",
        context_window=1_000_000,
        aliases=(),
        # DeepSeek performs automatic context (prefix) caching server-side; the
        # OpenAI wire reports the hit via ``prompt_tokens_details.cached_tokens``.
        # No ``cache_control`` breakpoint is needed (that is Anthropic-only, and
        # the provider gates injection on WireKind.ANTHROPIC). Enabling ``cache``
        # lets the meter price the reported cached tokens at the cache-read rate.
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=True, vision=False
        ),
        transports=(
            TransportSpec(
                id="openrouter.dsv4.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenRouter list price (deepseek/deepseek-v4-pro), USD per 1M tokens
        # (verified against the OpenRouter /models API). No cache-write charge.
        input_price_per_mtok=0.435,
        output_price_per_mtok=0.87,
        cache_read_price_per_mtok=0.003625,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="openrouter/deepseek-v4-flash",
        wire_model="deepseek/deepseek-v4-flash",
        label="DeepSeek V4 Flash (OpenRouter)",
        description="DeepSeek V4 Flash via OpenRouter (openai wire, bearer).",
        # OpenRouter advertises 1,048,576 top-level; routed top_provider serves
        # 1,000,000 — use the effective limit.
        context_window=1_000_000,
        aliases=(),
        # DeepSeek performs automatic server-side prefix caching; the OpenAI
        # wire reports hits via ``prompt_tokens_details.cached_tokens``.
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=True, vision=False
        ),
        transports=(
            TransportSpec(
                id="openrouter.dsv4flash.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenRouter list price (deepseek/deepseek-v4-flash), USD per 1M tokens
        # (verified against the OpenRouter /models API). No cache-write charge.
        input_price_per_mtok=0.09,
        output_price_per_mtok=0.18,
        cache_read_price_per_mtok=0.02,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        # Official DeepSeek direct connect. ``wire_model`` is the unprefixed name
        # the official API expects; ``model_id`` is our clean ``deepseek/`` id.
        model_id="deepseek/deepseek-v4-pro",
        wire_model="deepseek-v4-pro",
        label="DeepSeek V4 Pro (Official)",
        description="DeepSeek V4 Pro via the official api.deepseek.com (openai wire, bearer DEEPSEEK_API_KEY).",
        # Official spec: 1M context, 384K max output (api-docs pricing page).
        context_window=1_000_000,
        aliases=(),
        # Official API performs server-side prefix caching and reports hits via
        # ``prompt_cache_hit_tokens`` / ``prompt_tokens_details.cached_tokens``;
        # responses also carry ``reasoning_content`` (thinking).
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=True, vision=False
        ),
        transports=(
            TransportSpec(
                id="deepseek.dsv4pro.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("DEEPSEEK_API_KEY"),
                endpoint="https://api.deepseek.com/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # Official api.deepseek.com list price (USD per 1M tokens).
        # https://api-docs.deepseek.com/quick_start/pricing
        input_price_per_mtok=0.435,
        output_price_per_mtok=0.87,
        # Automatic server-side prefix cache: hits billed at the published
        # cache-hit rate; there is no separate cache-write surcharge (a miss is
        # just charged at full input price), so write price is 0.
        cache_read_price_per_mtok=0.003625,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="deepseek/deepseek-v4-flash",
        wire_model="deepseek-v4-flash",
        label="DeepSeek V4 Flash (Official)",
        description="DeepSeek V4 Flash via the official api.deepseek.com (openai wire, bearer DEEPSEEK_API_KEY).",
        context_window=1_000_000,
        aliases=(),
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=True, vision=False
        ),
        transports=(
            TransportSpec(
                id="deepseek.dsv4flash.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("DEEPSEEK_API_KEY"),
                endpoint="https://api.deepseek.com/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # Official api.deepseek.com list price (USD per 1M tokens).
        # https://api-docs.deepseek.com/quick_start/pricing
        input_price_per_mtok=0.14,
        output_price_per_mtok=0.28,
        # Automatic server-side prefix cache; hits billed at the published rate,
        # no separate cache-write surcharge.
        cache_read_price_per_mtok=0.0028,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="openrouter/glm-5.2",
        wire_model="z-ai/glm-5.2",
        label="GLM 5.2 (OpenRouter)",
        description="Zhipu GLM 5.2 via OpenRouter (openai wire, bearer).",
        # Multiple OpenRouter providers now serve the full 1,048,576 window
        # (Z.AI, Friendli, Novita, DeepInfra, Parasail, StreamLake). Empirically
        # verified: a 364,045-token input routed to Novita succeeds — capped at
        # 1,000,000 to match the other 1M models' effective limit.
        context_window=1_000_000,
        aliases=(),
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="openrouter.glm52.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenRouter list price (z-ai/glm-5.2), USD per 1M tokens
        # (verified against the OpenRouter /models API). No cache-write charge.
        input_price_per_mtok=1.4,
        output_price_per_mtok=4.4,
        cache_read_price_per_mtok=0.26,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        # OpenCode Go gateway. ``wire_model`` is the bare ``glm-5.2`` the Go
        # endpoint expects; ``model_id`` is our clean ``opencode-go/`` id.
        model_id="opencode-go/glm-5.2",
        wire_model="glm-5.2",
        label="GLM 5.2 (OpenCode Go)",
        description="Zhipu GLM 5.2 via the OpenCode Go subscription gateway (openai wire, bearer OPENCODE_API_KEY).",
        context_window=1_000_000,
        aliases=(),
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="opencode.go.glm52.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENCODE_API_KEY"),
                endpoint="https://opencode.ai/zen/go/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenCode Go is a flat subscription (usage-limited, not per-token billed):
        # the gateway returns ``cost: "0"``. We deliberately mirror the OpenRouter
        # GLM 5.2 list price here so the meter shows a comparable *notional* spend
        # — handy for gauging usage against the pay-per-token alternative. These
        # are not real charges.
        input_price_per_mtok=1.4,
        output_price_per_mtok=4.4,
        cache_read_price_per_mtok=0.26,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        # Volcengine Ark Agent Plan subscription gateway. Ark Agent Plan exposes
        # an *Anthropic-compatible* Messages endpoint (``/api/plan/v1/messages``)
        # — verified live: it returns native Anthropic frames (``type:message``
        # with ``thinking``/``text`` blocks and ``cache_read_input_tokens``), so
        # we use ``WireKind.ANTHROPIC`` to reuse the Anthropic codec. ``wire_model``
        # is the bare ``glm-5.2`` the endpoint expects; ``model_id`` is our clean
        # ``volcengine-plan/`` id. Auth = ``x-api-key: <ARK_API_KEY>`` plus the
        # required static ``anthropic-version`` header. Mirrors the ``opencode-go``
        # flat-subscription pattern.
        model_id="volcengine-plan/glm-5.2",
        wire_model="glm-5.2",
        label="GLM 5.2 (Volcengine Ark Plan)",
        description="Zhipu GLM 5.2 via the Volcengine Ark Agent Plan subscription gateway (anthropic wire, x-api-key ARK_API_KEY).",
        context_window=1_024_000,
        aliases=(),
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="volcengine.plan.glm52.anthropic",
                wire=WireKind.ANTHROPIC,
                auth=all_of(
                    header("x-api-key", secret_name="ARK_API_KEY"),
                    header("anthropic-version", value="2023-06-01"),
                ),
                endpoint="https://ark.cn-beijing.volces.com/api/plan/v1/messages",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # Ark Agent Plan is a flat subscription (套餐 AFP deduction, not real
        # per-token billing). We mirror the OpenRouter GLM 5.2 list price as a
        # *notional* spend so the meter shows comparable usage — not real charges.
        input_price_per_mtok=1.4,
        output_price_per_mtok=4.4,
        cache_read_price_per_mtok=0.26,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="openrouter/kimi-k2.7-code",
        wire_model="moonshotai/kimi-k2.7-code",
        label="Kimi K2.7 Code",
        description="Moonshot Kimi K2.7 Code via OpenRouter (openai wire, bearer).",
        context_window=262_144,
        aliases=(),
        # OpenRouter reports input_modalities=[text, image] (text+image->text).
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=False, vision=True
        ),
        transports=(
            TransportSpec(
                id="openrouter.kimi27.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenRouter list price (moonshotai/kimi-k2.7-code), USD per 1M tokens
        # (verified against the OpenRouter /models API). No cache-write charge.
        input_price_per_mtok=0.74,
        output_price_per_mtok=3.5,
        cache_read_price_per_mtok=0.15,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="openrouter/mimo-v2.5-pro",
        wire_model="xiaomi/mimo-v2.5-pro",
        label="MiMo V2.5 Pro",
        description="Xiaomi MiMo V2.5 Pro via OpenRouter (openai wire, bearer).",
        # OpenRouter top_provider serves up to 1,048,576; capped at 1,000,000.
        context_window=1_000_000,
        aliases=(),
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="openrouter.mimo25pro.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenRouter list price (xiaomi/mimo-v2.5-pro), USD per 1M tokens
        # (verified against the OpenRouter /models API). No cache-write charge.
        input_price_per_mtok=0.435,
        output_price_per_mtok=0.87,
        cache_read_price_per_mtok=0.0036,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="openrouter/minimax-m3",
        wire_model="minimax/minimax-m3",
        label="MiniMax M3",
        description="MiniMax M3 via OpenRouter (openai wire, bearer).",
        # OpenRouter dynamically routes large requests to providers with bigger
        # windows (Novita 1M, Parasail 1,048,576). Empirically verified: 639K-token
        # inputs succeed; the hard limit error fires at 1,048,576 — capped at 1,000,000.
        context_window=1_000_000,
        aliases=(),
        # OpenRouter reports input_modalities=[text, image, video] (text+image+video->text).
        capabilities=ProviderCapabilities(
            streaming=True, thinking=True, cache=False, vision=True
        ),
        transports=(
            TransportSpec(
                id="openrouter.minimaxm3.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        # OpenRouter list price (minimax/minimax-m3), USD per 1M tokens
        # (verified against the OpenRouter /models API). No cache-write charge.
        input_price_per_mtok=0.3,
        output_price_per_mtok=1.2,
        cache_read_price_per_mtok=0.06,
        cache_write_price_per_mtok=0.0,
    ),
    ModelSpec(
        model_id="echo",
        label="Echo (mock)",
        description="Local echo backend for smoke tests; no network.",
        # The local mock has no backend-enforced window. Keep enough room for the
        # production system prompt + builtin schemas so context budgeting does
        # not reject smoke-test requests before EchoProvider can run.
        context_window=200_000,
        aliases=(),
        capabilities=ProviderCapabilities(
            streaming=True, thinking=False, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="echo.openai.none",
                wire=WireKind.OPENAI,
                auth=none(),
                endpoint="local://echo",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        fallback_model_id=None,
        show_in_picker=False,
    ),
)


def _source_label(spec: ModelSpec) -> str:
    """Human-readable provenance for the picker, derived from the transport.

    Distinguishes otherwise near-identical entries (e.g. the official vs the
    OpenRouter DeepSeek V4 models) by where the request is actually sent."""
    from urllib.parse import urlparse

    if not spec.transports:
        return "?"
    endpoint = spec.transports[0].endpoint
    host = (urlparse(endpoint).hostname or endpoint).lower()
    known = {
        "openrouter.ai": "OpenRouter",
        "api.deepseek.com": "DeepSeek (official)",
        "opencode.ai": "OpenCode Go",
        "ark.cn-beijing.volces.com": "Volcengine Ark Plan",
    }
    if host in known:
        return known[host]
    if endpoint.startswith("local://"):
        return "local"
    return host


def _build_registry(
    specs: tuple[ModelSpec, ...],
) -> tuple[
    dict[str, ModelSpec],
    dict[str, str],
    list[dict[str, object]],
    dict[str, str],
]:
    registry: dict[str, ModelSpec] = {}
    aliases: dict[str, str] = {}
    options: list[dict[str, object]] = []
    fallback: dict[str, str] = {}
    for spec in specs:
        registry[spec.model_id] = spec
        for alias in spec.aliases:
            aliases[alias.lower()] = spec.model_id
        if spec.show_in_picker:
            options.append(
                {
                    "model_id": spec.model_id,
                    "label": spec.label,
                    "description": spec.description,
                    "context_window": spec.context_window,
                    "input_price_per_mtok": spec.input_price_per_mtok,
                    "output_price_per_mtok": spec.output_price_per_mtok,
                    "vision": spec.capabilities.vision,
                    "source": _source_label(spec),
                }
            )
        if spec.fallback_model_id is not None:
            fallback[spec.model_id] = spec.fallback_model_id
    return registry, aliases, options, fallback


(
    MODEL_REGISTRY,
    MODEL_ALIASES,
    MODEL_OPTIONS,
    MODEL_FALLBACK,
) = _build_registry(_SPECS)


def resolve_alias(name: str) -> str:
    """Alias → canonical model_id; pass through unchanged if not an alias."""
    return MODEL_ALIASES.get(name.lower(), name)


def get_spec(model_id: str) -> ModelSpec | None:
    return MODEL_REGISTRY.get(model_id)


def model_source(model_id: str) -> str:
    """Provider/provenance label for a model id (e.g. ``OpenRouter``), '' if unknown."""
    spec = MODEL_REGISTRY.get(model_id)
    return _source_label(spec) if spec is not None else ""


def model_credentials_available(model_id: str, resolver: CredentialResolver) -> bool:
    """True iff some transport of ``model_id`` can authenticate with current secrets.

    Network-free pre-flight built on ``Credential.is_satisfiable``. Unknown ids fall
    back to the passthrough spec (single OpenRouter bearer), so an unregistered id is
    reported available exactly when ``OPENROUTER_API_KEY`` resolves. A transport-less
    spec is treated as available (never block a spawn on a missing-key heuristic)."""
    spec = get_spec(model_id) or _passthrough_spec(model_id)
    if not spec.transports:
        return True
    return any(t.auth.is_satisfiable(resolver) for t in spec.transports)


def get_context_window(model_id: str, override: int | None = None) -> int:
    if override is not None:
        return override
    spec = MODEL_REGISTRY.get(model_id)
    if spec is not None:
        return spec.context_window
    return 200_000


def _passthrough_spec(model_id: str) -> ModelSpec:
    """Conservative spec for unknown models: default window, single bearer
    OpenRouter transport (openai wire), no advanced capabilities. Lets any
    OpenRouter model id (e.g. ``deepseek/deepseek-r1``) work with the same key."""
    return ModelSpec(
        model_id=model_id,
        label=model_id,
        description="Passthrough (unregistered) model.",
        context_window=200_000,
        capabilities=ProviderCapabilities(
            streaming=True, thinking=False, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id=f"passthrough.{model_id}.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
                node_pinning=False,
            ),
        ),
        show_in_picker=False,
    )


__all__ = [
    "TransportSpec",
    "ModelSpec",
    "MODEL_REGISTRY",
    "MODEL_ALIASES",
    "MODEL_OPTIONS",
    "MODEL_FALLBACK",
    "resolve_alias",
    "get_spec",
    "model_source",
    "model_credentials_available",
    "get_context_window",
    "_passthrough_spec",
]
