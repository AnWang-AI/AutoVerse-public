"""autoverse.infra.llm.transport — transport layer (auth + endpoint + HTTP)."""

from __future__ import annotations

from autoverse.infra.llm.transport.base import (
    Transport,
    WireRequest,
    decode_stream,
)
from autoverse.infra.llm.transport.credential import (
    AuthContext,
    Credential,
    CredentialResolver,
    all_of,
    appkey,
    bearer,
    header,
    hmac_sha1,
    none,
    or_else,
)
from autoverse.infra.llm.transport.http import HttpTransport

__all__ = [
    "Transport",
    "WireRequest",
    "decode_stream",
    "HttpTransport",
    "Credential",
    "CredentialResolver",
    "AuthContext",
    "bearer",
    "header",
    "hmac_sha1",
    "appkey",
    "all_of",
    "or_else",
    "none",
]
