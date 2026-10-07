"""HttpTransport — network exceptions are wrapped into structured LLMError.

Regression: raw httpx exceptions (timeout, connection reset) used to escape
``send`` and crash the stream because fallback only catches ``LLMError``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from autoverse.infra.llm.registry import TransportSpec
from autoverse.infra.llm.taxonomy import ErrorKind, LLMError, classify
from autoverse.infra.llm.transport.base import WireRequest
from autoverse.infra.llm.transport.credential import CredentialResolver, bearer
from autoverse.infra.llm.transport.http import HttpTransport
from autoverse.types import AbortController, ErrorClass, WireKind

ENDPOINT = "https://example.test/v1/chat"


def _transport() -> HttpTransport:
    spec = TransportSpec(
        id="t",
        wire=WireKind.OPENAI,
        auth=bearer("KEY"),
        endpoint=ENDPOINT,
        real_streaming=True,
    )
    return HttpTransport(spec, spec.auth, CredentialResolver())


async def _drain(t: HttpTransport) -> None:
    ctrl = AbortController()
    async for _ in t.send(WireRequest(body={"model": "m"}, stream=True), ctrl.signal, 5.0):
        pass


@respx.mock
async def test_timeout_wrapped_as_retryable_llm_error() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("slow"))
    t = _transport()
    with pytest.raises(LLMError) as ei:
        await _drain(t)
    assert ei.value.kind is ErrorKind.TIMEOUT
    assert classify(ei.value) is ErrorClass.RETRYABLE
    await t.aclose()


@respx.mock
async def test_connect_error_wrapped_as_retryable_llm_error() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    t = _transport()
    with pytest.raises(LLMError) as ei:
        await _drain(t)
    assert ei.value.kind is ErrorKind.SERVER
    assert classify(ei.value) is ErrorClass.RETRYABLE
    await t.aclose()


@respx.mock
async def test_429_preserves_string_platform_code_and_retry_after() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={
                "error": {
                    "code": "ServerOverloaded",
                    "message": "busy",
                    "type": "TooManyRequests",
                }
            },
        )
    )
    t = _transport()
    with pytest.raises(LLMError) as ei:
        await _drain(t)
    assert ei.value.kind is ErrorKind.RATE_LIMIT
    assert ei.value.platform_code == "ServerOverloaded"
    assert ei.value.retry_after == 7.0
    await t.aclose()


@respx.mock
async def test_context_overflow_code_is_preserved_as_structured_kind() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "too long",
                }
            },
        )
    )
    t = _transport()
    with pytest.raises(LLMError) as ei:
        await _drain(t)
    assert ei.value.kind is ErrorKind.CONTEXT_OVERFLOW
    assert ei.value.platform_code == "context_length_exceeded"
    await t.aclose()
