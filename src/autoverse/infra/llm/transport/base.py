"""Transport base (docs/reference/models.md §3.4).

Transport handles auth + endpoint + timeout + raw streaming/SSE — never message
encoding or retry policy. ``decode_stream`` is the shared helper wiring send →
decode_chunk, reused by HttpTransport and SingleShotAdapter; retries and
fallback are owned by ``infra.llm.fallback``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Protocol

from autoverse.infra.llm.codec.base import Codec, DecodeState
from autoverse.types import AbortSignal, StreamEvent

if TYPE_CHECKING:
    from autoverse.infra.llm.registry import TransportSpec


@dataclass
class WireRequest:
    body: dict[str, object]
    stream: bool


class Transport(Protocol):
    spec: TransportSpec

    def send(
        self, req: WireRequest, signal: AbortSignal, timeout: float
    ) -> AsyncIterator[dict[str, object]]: ...

    def clone(self) -> "Transport": ...


async def decode_stream(
    transport: Transport,
    codec: Codec,
    req: WireRequest,
    signal: AbortSignal,
    timeout: float,
) -> AsyncIterator[StreamEvent]:
    """Send via transport and decode each raw chunk into StreamEvents."""
    st = DecodeState()
    async for chunk in transport.send(req, signal, timeout):
        for ev in codec.decode_chunk(chunk, st):
            yield ev


__all__ = ["WireRequest", "Transport", "decode_stream"]
