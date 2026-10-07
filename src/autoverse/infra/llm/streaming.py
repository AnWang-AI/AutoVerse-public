"""SingleShotAdapter — non-streaming backend → single-event stream (§6.2).

Wraps a non-streaming transport so it presents the same StreamEvent contract as
a true-streaming one. The whole response is collected, then decoded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from autoverse.infra.llm.codec.base import Codec, DecodeState
from autoverse.infra.llm.transport.base import Transport, WireRequest
from autoverse.types import AbortSignal, StreamEvent

if TYPE_CHECKING:
    from autoverse.infra.llm.registry import TransportSpec


class SingleShotAdapter:
    def __init__(self, transport: Transport, codec: Codec) -> None:
        self._transport = transport
        self._codec = codec
        self.spec: TransportSpec = transport.spec

    async def send(
        self, req: WireRequest, signal: AbortSignal, timeout: float
    ) -> AsyncIterator[dict[str, object]]:
        async for chunk in self._transport.send(req, signal, timeout):
            yield chunk

    async def send_decoded(
        self, req: WireRequest, signal: AbortSignal, timeout: float
    ) -> AsyncIterator[StreamEvent]:
        chunks = [c async for c in self._transport.send(req, signal, timeout)]
        st = DecodeState()
        for chunk in chunks:
            for ev in self._codec.decode_chunk(chunk, st):
                yield ev

    def clone(self) -> "SingleShotAdapter":
        return SingleShotAdapter(self._transport.clone(), self._codec)


__all__ = ["SingleShotAdapter"]
