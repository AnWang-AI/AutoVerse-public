"""HTTP transport + SSE parsing (docs/reference/models.md §6.1, §8).

Assembles AuthContext (with body for HMAC signing), applies the composable
Credential, streams via httpx, raises structured LLMError on non-2xx, and parses
SSE into raw wire chunks. Cancellation is checked each loop iteration.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, AsyncIterator

import httpx

from autoverse.infra.llm.taxonomy import ErrorKind, LLMError
from autoverse.infra.llm.transport.base import WireRequest
from autoverse.infra.llm.transport.credential import (
    AuthContext,
    Credential,
    CredentialResolver,
)
from autoverse.types import AbortSignal

if TYPE_CHECKING:
    from autoverse.infra.llm.registry import TransportSpec


def _kind_for_status(
    status: int, platform_code: str | int | None = None
) -> ErrorKind:
    if isinstance(platform_code, str) and platform_code.lower() in {
        "context_length_exceeded",
        "context_window_exceeded",
        "context_overflow",
        "prompt_too_long",
    }:
        return ErrorKind.CONTEXT_OVERFLOW
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 400:
        return ErrorKind.BAD_REQUEST
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status >= 500:
        return ErrorKind.SERVER
    return ErrorKind.UNKNOWN


def _platform_code(text: str) -> str | int | None:
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, (str, int)):
                return code
        code = body.get("code")
        if isinstance(code, (str, int)):
            return code
    return None


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    for name in ("X-RateLimit-Reset", "RateLimit-Reset"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        now = datetime.now(timezone.utc).timestamp()
        return max(0.0, value - now) if value > now else max(0.0, value)
    return None


class HttpTransport:
    def __init__(
        self,
        spec: TransportSpec,
        auth: Credential,
        creds: CredentialResolver,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.spec = spec
        self._auth = auth
        self._creds = creds
        self._client = client or httpx.AsyncClient()
        self._account_id = ""

    async def send(
        self, req: WireRequest, signal: AbortSignal, timeout: float
    ) -> AsyncIterator[dict[str, object]]:
        signal.raise_if_aborted()
        payload = dict(req.body)
        payload["stream"] = req.stream
        body_bytes = json.dumps(payload).encode()
        ctx = AuthContext(
            secrets=self._creds,
            body=body_bytes,
            account_id=self._account_id,
        )
        headers = self._auth.apply({"Content-Type": "application/json"}, ctx)

        try:
            async with self._client.stream(
                "POST",
                self.spec.endpoint,
                content=body_bytes,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 300:
                    text = await resp.aread()
                    decoded = text.decode(errors="replace")
                    platform_code = _platform_code(decoded)
                    raise LLMError(
                        f"HTTP {resp.status_code}: {decoded[:500]}",
                        http_status=resp.status_code,
                        platform_code=platform_code,
                        kind=_kind_for_status(resp.status_code, platform_code),
                        retry_after=_retry_after(resp.headers),
                    )
                if self.spec.node_pinning:
                    pinned = resp.headers.get("X-Account-Id")
                    if pinned:
                        self._account_id = pinned
                async for chunk in self._iter_sse(resp, signal):
                    yield chunk
        except httpx.TimeoutException as e:
            # Wrap as structured RETRYABLE so fallback can retry rather than
            # letting the raw httpx exception escape and crash the stream.
            raise LLMError(
                f"request timed out: {e}", kind=ErrorKind.TIMEOUT
            ) from e
        except httpx.HTTPError as e:
            # Connection reset / read error / protocol error → retryable.
            raise LLMError(
                f"transport error: {e}", kind=ErrorKind.SERVER
            ) from e

    async def _iter_sse(
        self, resp: httpx.Response, signal: AbortSignal
    ) -> AsyncIterator[dict[str, object]]:
        async for line in resp.aiter_lines():
            signal.raise_if_aborted()
            if not line:
                continue
            if line.startswith("data: "):
                data = line[6:]
            elif line.startswith("data:"):
                data = line[5:]
            else:
                continue
            data = data.strip()
            if data == "[DONE]":
                break
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError as e:
                raise LLMError(
                    f"invalid SSE JSON: {data[:200]}",
                    kind=ErrorKind.SERVER,
                ) from e
            if isinstance(parsed, dict):
                yield parsed

    def reset_pinning(self) -> None:
        """Clear captured account_id (called on protocol switch to avoid stale routing)."""
        self._account_id = ""

    def clone(self) -> "HttpTransport":
        return HttpTransport(
            self.spec, self._auth, self._creds, client=httpx.AsyncClient()
        )

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["HttpTransport"]
