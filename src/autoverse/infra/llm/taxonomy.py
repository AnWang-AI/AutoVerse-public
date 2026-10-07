"""Structured error classification (docs/reference/models.md §8).

Classification never relies on substring matching. Transport raises ``LLMError``
carrying structured fields (HTTP status, platform code, error kind) and the pure
``classify`` function maps it to the authoritative ``ErrorClass`` from
``autoverse.types`` (never redefined here).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from autoverse.types import ErrorClass


class ErrorKind(Enum):
    UNKNOWN = "unknown"
    ABORT = "abort"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"
    UNSUPPORTED = "unsupported"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"


@dataclass
class LLMError(Exception):
    message: str
    http_status: int | None = None
    platform_code: str | int | None = None
    kind: ErrorKind = ErrorKind.UNKNOWN
    retry_after: float | None = None
    response_started: bool = False
    retry_exhausted: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)


def classify(e: LLMError) -> ErrorClass:
    if e.kind in (
        ErrorKind.ABORT,
        ErrorKind.BAD_REQUEST,
        ErrorKind.CONTEXT_OVERFLOW,
    ):
        return ErrorClass.PERMANENT
    if e.kind in (ErrorKind.AUTH, ErrorKind.UNSUPPORTED):
        return ErrorClass.FALLBACK_TRIGGER
    if e.kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER, ErrorKind.TIMEOUT):
        return ErrorClass.RETRYABLE
    status = e.http_status
    if status in (408, 425, 429) or (status or 0) >= 500:
        return ErrorClass.RETRYABLE
    if status in (401, 403):
        return ErrorClass.FALLBACK_TRIGGER
    if status == 400:
        return ErrorClass.PERMANENT
    return ErrorClass.FALLBACK_TRIGGER


__all__ = ["ErrorKind", "LLMError", "classify"]
