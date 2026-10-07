"""Redactor — strip secrets before data enters the trace/export (O7/O12).

Two redaction modes, combined:

- **Known secret values**: any literal secret string registered with the
  redactor (e.g. an API key resolved from the vault) is replaced wherever it
  appears in a string or dict value.
- **Key-shaped patterns**: common secret shapes (Authorization headers, ``sk-``
  / Bearer tokens, ``key=...`` assignments) are masked by regex.

Redaction runs at collector fold time, so what lands on disk is already masked
(docs/reference/events-and-storage.md §4.9). Failure is fail-safe: if a regex blows up on a field,
the whole field is masked with ``[redaction-error]`` rather than leaking plaintext.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from autoverse.types import (
    ContentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

REDACTED = "***REDACTED***"

#: Substrings that mark a dict *key* as carrying a secret value, regardless of
#: the value's shape (so ``{"password": "x"}`` is masked even when ``x`` matches
#: no value pattern). Matched against the lower-cased, separator-stripped key so
#: ``api_key``/``apiKey``/``API-KEY`` all hit ``apikey``. See docs/reference/events-and-storage.md §4.9.
_SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "authorization",
    "credential",
    "privatekey",
)


def _key_norm(key: str) -> str:
    """Lower-case the key and drop separators so ``api_key`` → ``apikey``."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_secret_key(key: str) -> bool:
    norm = _key_norm(key)
    return any(marker in norm for marker in _SECRET_KEY_MARKERS)

#: Regex patterns for common secret shapes (masked even if not pre-registered).
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)"
               r"\s*[:=]\s*)['\"]?[A-Za-z0-9._\-]{6,}['\"]?"),
    re.compile(r"\b(sk-)[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
)


class Redactor:
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        # Sort longest first so containing secrets are masked before substrings.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def add_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def text(self, value: str) -> str:
        try:
            out = value
            for secret in self._secrets:
                out = out.replace(secret, REDACTED)
            for pat in _PATTERNS:
                out = pat.sub(self._mask_match, out)
            return out
        except (re.error, TypeError):
            return "[redaction-error]"

    def value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return self.mapping(value)
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value

    def mapping(self, data: Mapping[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in data.items():
            key = str(k)
            # Mask by secret-named key first: a value like {"password": "x"} must
            # be redacted even when "x" matches no value-shape pattern (§4.9).
            if isinstance(v, str) and _is_secret_key(key):
                out[key] = REDACTED
            else:
                out[key] = self.value(v)
        return out

    def message(self, message: Message) -> Message:
        """Return a recursively redacted copy safe for durable persistence."""
        blocks: list[ContentBlock] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append(TextBlock(text=self.text(block.text)))
            elif isinstance(block, ThinkingBlock):
                blocks.append(
                    ThinkingBlock(
                        text=self.text(block.text), signature=block.signature
                    )
                )
            elif isinstance(block, ToolUseBlock):
                blocks.append(
                    ToolUseBlock(
                        id=block.id,
                        name=block.name,
                        input=self.mapping(block.input),
                    )
                )
            elif isinstance(block, ToolResultBlock):
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=self.text(block.content),
                        is_error=block.is_error,
                        metadata=self.mapping(block.metadata),
                    )
                )
            elif isinstance(block, ImageBlock):
                blocks.append(
                    ImageBlock(
                        media_type=block.media_type,
                        data=(
                            self.text(block.data)
                            if block.source_type == "url"
                            else block.data
                        ),
                        source_type=block.source_type,
                    )
                )
        return Message(
            role=message.role,
            content=tuple(blocks),
            metadata=self.mapping(message.metadata),
        )

    @staticmethod
    def _mask_match(m: re.Match[str]) -> str:
        prefix = m.group(1) if m.groups() else None
        # Preserve a leading "Authorization:" / "api_key=" label, mask the value.
        return f"{prefix}{REDACTED}" if prefix else REDACTED


__all__ = ["Redactor", "REDACTED"]
