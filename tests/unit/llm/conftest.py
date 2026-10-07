"""Shared fixtures for llm unit + acceptance tests: sample Messages and FakeTransport."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from autoverse.infra.llm.taxonomy import ErrorKind, LLMError
from autoverse.infra.llm.transport.base import WireRequest
from autoverse.infra.llm.registry import TransportSpec
from autoverse.infra.llm.transport.credential import none
from autoverse.types import (
    AbortSignal,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    WireKind,
)


@pytest.fixture
def sample_messages() -> list[Message]:
    return [
        Message(role="user", content=(TextBlock(text="What's the weather?"),)),
        Message(
            role="assistant",
            content=(
                TextBlock(text="Let me check."),
                ToolUseBlock(id="toolu_01", name="get_weather", input={"city": "Paris"}),
            ),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(tool_use_id="toolu_01", content="Sunny, 22C"),
            ),
        ),
    ]


@pytest.fixture
def image_message() -> Message:
    return Message(
        role="user",
        content=(
            TextBlock(text="describe"),
            ImageBlock(media_type="image/png", data="QQ=="),
        ),
    )


class FakeTransport:
    """In-memory transport: yields preset chunks, or raises a scripted sequence
    of LLMErrors (one per attempt) before succeeding. Network-free."""

    def __init__(
        self,
        spec: TransportSpec,
        chunks: list[dict[str, object]] | None = None,
        errors: list[LLMError] | None = None,
    ) -> None:
        self.spec = spec
        self._chunks = chunks or []
        self._errors = list(errors or [])
        self.calls = 0

    async def send(
        self, req: WireRequest, signal: AbortSignal, timeout: float
    ) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        for chunk in self._chunks:
            yield chunk

    def clone(self) -> "FakeTransport":
        return FakeTransport(self.spec, list(self._chunks), list(self._errors))


def fake_spec(
    wire: WireKind = WireKind.OPENAI, real_streaming: bool = True, sid: str = "fake.t"
) -> TransportSpec:
    return TransportSpec(
        id=sid,
        wire=wire,
        auth=none(),
        endpoint="local://fake",
        real_streaming=real_streaming,
    )


def err(
    status: int | None = None, kind: ErrorKind = ErrorKind.UNKNOWN, msg: str = "boom"
) -> LLMError:
    return LLMError(msg, http_status=status, kind=kind)


__all__ = ["FakeTransport", "fake_spec", "err"]
