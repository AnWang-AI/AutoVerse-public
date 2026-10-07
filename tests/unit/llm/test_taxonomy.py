"""Error taxonomy: structured classification (no string matching, §8)."""

from __future__ import annotations

from autoverse.infra.llm.taxonomy import ErrorKind, LLMError, classify
from autoverse.types import ErrorClass


def test_abort_and_bad_request_permanent() -> None:
    assert classify(LLMError("x", kind=ErrorKind.ABORT)) is ErrorClass.PERMANENT
    assert (
        classify(LLMError("x", kind=ErrorKind.BAD_REQUEST))
        is ErrorClass.PERMANENT
    )


def test_auth_and_unsupported_fallback() -> None:
    assert (
        classify(LLMError("x", kind=ErrorKind.AUTH))
        is ErrorClass.FALLBACK_TRIGGER
    )
    assert (
        classify(LLMError("x", kind=ErrorKind.UNSUPPORTED))
        is ErrorClass.FALLBACK_TRIGGER
    )


def test_known_retryable_kinds() -> None:
    for k in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER, ErrorKind.TIMEOUT):
        assert classify(LLMError("x", kind=k)) is ErrorClass.RETRYABLE


def test_http_status_retryable() -> None:
    for status in (408, 425, 429, 500, 503, 529):
        assert (
            classify(LLMError("x", http_status=status)) is ErrorClass.RETRYABLE
        )


def test_http_auth_fallback() -> None:
    for status in (401, 403):
        assert (
            classify(LLMError("x", http_status=status))
            is ErrorClass.FALLBACK_TRIGGER
        )


def test_http_400_permanent() -> None:
    assert classify(LLMError("x", http_status=400)) is ErrorClass.PERMANENT


def test_unknown_conservatively_fallback() -> None:
    assert classify(LLMError("x")) is ErrorClass.FALLBACK_TRIGGER
    assert (
        classify(LLMError("x", http_status=418)) is ErrorClass.FALLBACK_TRIGGER
    )


def test_llmerror_is_exception() -> None:
    e = LLMError("boom", http_status=500)
    assert isinstance(e, Exception)
    assert str(e) == "boom"
