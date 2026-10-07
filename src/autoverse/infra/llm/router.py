"""Router — registry + routing収口 single point (§13, §3.4a).

Logical model name → assembled WireProvider. Adding a backend = add a ModelSpec
(registry) + reuse/add codec/transport; Router and core stay unchanged. Auth is
``ts.auth.apply(...)`` directly — no AuthKind branching.
"""

from __future__ import annotations

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.base import Codec
from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.fallback import (
    AttemptBudget,
    FallbackPlan,
    ModelFallbackStrategy,
    ProtocolSwitchStrategy,
    RetryStrategy,
)
from autoverse.infra.llm.provider import WireProvider
from autoverse.infra.llm.recovery_mw import RecoveryMiddleware
from autoverse.infra.llm.registry import (
    TransportSpec,
    _passthrough_spec,
    get_spec,
    resolve_alias,
)
from autoverse.infra.llm.streaming import SingleShotAdapter
from autoverse.infra.llm.transport.base import Transport
from autoverse.infra.llm.transport.credential import CredentialResolver
from autoverse.infra.llm.transport.http import HttpTransport
from autoverse.ports import LLMProvider
from autoverse.types import WireKind

_CODECS: dict[WireKind, Codec] = {
    WireKind.ANTHROPIC: AnthropicWireCodec(),
    WireKind.OPENAI: OpenAIWireCodec(),
}


class Router:
    def __init__(self, creds: CredentialResolver, *, max_retries: int = 3) -> None:
        self._creds = creds
        self._max_retries = max(0, max_retries)

    def build(
        self, model_name: str, *, _budget: AttemptBudget | None = None
    ) -> LLMProvider:
        spec = get_spec(resolve_alias(model_name)) or _passthrough_spec(
            model_name
        )
        budget = _budget
        codec = _CODECS[spec.transports[0].wire]
        transports = [self._make_transport(ts) for ts in spec.transports]
        recovery = (
            RecoveryMiddleware(set())
            if spec.capabilities.degradation_prone
            else None
        )
        plan = FallbackPlan(
            retry=RetryStrategy(max_retries=self._max_retries),
            protocol_switch=ProtocolSwitchStrategy(spec),
            model_fallback=ModelFallbackStrategy(spec, router=self, budget=budget),
            budget=budget,
        )
        return WireProvider(
            spec, codec, transports, fallback=plan, recovery=recovery
        )

    def _make_transport(self, ts: TransportSpec) -> Transport:
        transport = HttpTransport(ts, ts.auth, self._creds)
        if ts.real_streaming:
            return transport
        return SingleShotAdapter(transport, _CODECS[ts.wire])


__all__ = ["Router", "_CODECS"]
