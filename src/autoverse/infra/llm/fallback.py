"""Fallback — declarative three strategies (§7, §8).

RetryStrategy (same-transport exponential backoff), ProtocolSwitchStrategy
(walk a model's transports in preference order), ModelFallbackStrategy (degrade
to spec.fallback_model_id). ``FallbackPlan.run()`` yields structured retry
events and raises the final structured ``LLMError`` so the core turn cannot
mistake an exhausted request for an empty successful response.
"""

from __future__ import annotations

import asyncio
import inspect
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Protocol

from autoverse.infra.llm.codec.base import Codec
from autoverse.infra.llm.registry import ModelSpec
from autoverse.infra.llm.taxonomy import LLMError, classify
from autoverse.infra.llm.transport.base import Transport, WireRequest
from autoverse.types import (
    AbortSignal,
    ErrorClass,
    EventType,
    LLMRequest,
    RetryPayload,
    StreamEvent,
    make_event,
)

if TYPE_CHECKING:
    from autoverse.ports import LLMProvider
    from autoverse.infra.llm.router import Router

_BASE_TIMEOUT = 60.0
_RETRY_TIMEOUT = 120.0


class FallbackStrategy(Protocol):
    def run(
        self,
        req: LLMRequest,
        signal: AbortSignal,
        *,
        codec: Codec,
        transports: list[Transport],
    ) -> AsyncIterator[StreamEvent]: ...


@dataclass
class RetryStrategy:
    max_retries: int = 3
    # None means one initial attempt plus ``max_retries`` retries across the
    # entire transport/model chain. An explicit value remains available for
    # tests and advanced callers.
    global_max_attempts: int | None = None

    def delay(self, attempt: int) -> float:
        base = min(5.0 * float(2**attempt), 60.0)
        jitter = random.uniform(0.0, float(min(attempt + 1, 5)))
        return base + jitter

    def timeout(self, attempt: int) -> float:
        return _BASE_TIMEOUT if attempt == 0 else _RETRY_TIMEOUT

    @property
    def attempt_limit(self) -> int:
        return self.global_max_attempts or (self.max_retries + 1)


@dataclass
class AttemptBudget:
    max_attempts: int
    attempts: int = 0
    visited_models: set[str] = field(default_factory=set)

    def consume(self) -> bool:
        if self.attempts >= self.max_attempts:
            return False
        self.attempts += 1
        return True


async def _sleep_or_abort(delay: float, signal: AbortSignal) -> None:
    if delay <= 0:
        signal.raise_if_aborted()
        return
    try:
        await asyncio.wait_for(signal.wait(), timeout=delay)
    except TimeoutError:
        return
    signal.raise_if_aborted()


@dataclass
class ProtocolSwitchStrategy:
    """Preference ordering over a model's transports (clears account_id on
    switch — fresh routing per channel)."""

    spec: ModelSpec

    def order(self, transports: list[Transport]) -> list[Transport]:
        return list(transports)

    def clear_pinning(self, transport: Transport) -> None:
        """Drop a transport's captured node ``account_id`` when switching away
        from it, so A-protocol routing never leaks into B-protocol (§12).

        Optional hook: transports that don't pin nodes simply lack the method.
        """
        reset = getattr(transport, "reset_pinning", None)
        if callable(reset):
            reset()


@dataclass
class ModelFallbackStrategy:
    spec: ModelSpec
    router: "Router | None" = None
    budget: AttemptBudget | None = None

    @property
    def target(self) -> str | None:
        return self.spec.fallback_model_id

    def build(self) -> "LLMProvider | None":
        if self.router is None or self.spec.fallback_model_id is None:
            return None
        target = self.spec.fallback_model_id
        if self.budget is not None:
            if target in self.budget.visited_models:
                return None
            self.budget.visited_models.add(target)
        build = self.router.build
        if "_budget" in inspect.signature(build).parameters:
            return build(target, _budget=self.budget)
        return build(target)


@dataclass
class FallbackPlan:
    retry: RetryStrategy
    protocol_switch: ProtocolSwitchStrategy
    model_fallback: ModelFallbackStrategy
    budget: AttemptBudget | None = None

    def _send_decoded(
        self,
        transport: Transport,
        wire_req: WireRequest,
        signal: AbortSignal,
        *,
        codec: Codec,
        timeout: float,
    ) -> AsyncIterator[StreamEvent]:
        # transport exposes send_decoded if it is an adapter; else use the
        # shared decode_stream helper over its raw chunk stream.
        send_decoded = getattr(transport, "send_decoded", None)
        if send_decoded is not None:
            result: AsyncIterator[StreamEvent] = send_decoded(
                wire_req, signal, timeout
            )
            return result
        from autoverse.infra.llm.transport.base import decode_stream

        return decode_stream(transport, codec, wire_req, signal, timeout)

    async def run(
        self,
        req: LLMRequest,
        signal: AbortSignal,
        *,
        codec: Codec,
        transports: list[Transport],
        wire_req: WireRequest,
    ) -> AsyncIterator[StreamEvent]:
        ordered = self.protocol_switch.order(transports)
        budget = self.budget or AttemptBudget(self.retry.attempt_limit)
        budget.visited_models.add(self.protocol_switch.spec.model_id)
        self.model_fallback.budget = budget
        last_error = LLMError("all transports exhausted")

        for transport_index, transport in enumerate(ordered):
            for attempt in range(self.retry.max_retries + 1):
                if not budget.consume():
                    break
                response_started = False
                try:
                    async for ev in self._send_decoded(
                        transport,
                        wire_req,
                        signal,
                        codec=codec,
                        timeout=self.retry.timeout(attempt),
                    ):
                        if ev.type in {
                            EventType.TEXT_DELTA,
                            EventType.THINKING_DELTA,
                            EventType.TOOL_START,
                            EventType.USAGE,
                            EventType.TURN_END,
                        }:
                            response_started = True
                        yield ev
                    return
                except LLMError as e:
                    e.response_started = response_started
                    last_error = e
                    cls = classify(e)
                    # Once any model output has escaped, replaying the request can
                    # duplicate text or execute a tool twice. Surface the partial
                    # stream failure instead of transparently retrying it.
                    if response_started or cls is ErrorClass.PERMANENT:
                        raise
                    if cls is ErrorClass.FALLBACK_TRIGGER:
                        break
                    can_retry_same = (
                        attempt < self.retry.max_retries
                        and budget.attempts < budget.max_attempts
                    )
                    if not can_retry_same:
                        break
                    delay = (
                        min(e.retry_after, 300.0)
                        if e.retry_after is not None
                        else self.retry.delay(attempt)
                    )
                    yield make_event(
                        RetryPayload(
                            attempt=budget.attempts,
                            reason=e.message,
                            error_class=cls.value,
                            http_status=e.http_status,
                            platform_code=e.platform_code,
                            delay_seconds=delay,
                            next_transport=transport.spec.id,
                        )
                    )
                    await _sleep_or_abort(delay, signal)
            self.protocol_switch.clear_pinning(transport)
            if budget.attempts >= budget.max_attempts:
                break
            if transport_index + 1 < len(ordered):
                next_transport = ordered[transport_index + 1]
                yield make_event(
                    RetryPayload(
                        attempt=budget.attempts,
                        reason=last_error.message,
                        error_class=classify(last_error).value,
                        http_status=last_error.http_status,
                        platform_code=last_error.platform_code,
                        next_transport=next_transport.spec.id,
                    )
                )

        target = self.model_fallback.target
        if target is not None and budget.attempts < budget.max_attempts:
            yield make_event(
                RetryPayload(
                    attempt=budget.attempts,
                    reason=last_error.message,
                    error_class=classify(last_error).value,
                    http_status=last_error.http_status,
                    platform_code=last_error.platform_code,
                    next_model=target,
                )
            )
            fallback_provider = self.model_fallback.build()
            if fallback_provider is not None:
                async for ev in fallback_provider.stream(req, signal):
                    yield ev
                return

        last_error.retry_exhausted = True
        raise last_error


__all__ = [
    "FallbackStrategy",
    "RetryStrategy",
    "AttemptBudget",
    "ProtocolSwitchStrategy",
    "ModelFallbackStrategy",
    "FallbackPlan",
]
