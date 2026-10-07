"""EchoProvider — infra version (§15).

Real provider shape, no network. Emits text → usage → turn-end events. Used for
per-model smoke tests.
"""

from __future__ import annotations

from typing import AsyncIterator

from autoverse.types import (
    AbortSignal,
    LLMRequest,
    ProviderCapabilities,
    StreamEvent,
    TextDeltaPayload,
    TurnEndPayload,
    Usage,
    UsagePayload,
    make_event,
)
from autoverse.infra.llm.provider import estimate_request_tokens


class EchoProvider:
    def __init__(
        self,
        capabilities: ProviderCapabilities = ProviderCapabilities(),
        reply: str = "echo",
    ) -> None:
        self._capabilities = capabilities
        self._reply = reply

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def stream(
        self, req: LLMRequest, signal: AbortSignal
    ) -> AsyncIterator[StreamEvent]:
        signal.raise_if_aborted()
        yield make_event(TextDeltaPayload(text=self._reply))
        yield make_event(
            UsagePayload(
                usage=Usage(input_tokens=0, output_tokens=len(self._reply)),
                model=req.model or "echo",
            )
        )
        yield make_event(
            TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")
        )

    async def count_tokens(self, req: LLMRequest) -> int:
        return estimate_request_tokens(req)

    async def aclose(self) -> None:
        return None


__all__ = ["EchoProvider"]
