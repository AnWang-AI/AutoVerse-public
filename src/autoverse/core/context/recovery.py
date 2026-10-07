"""RecoveryPolicy — LLM error & degradation recovery (docs/reference/models.md §9-§11).

Decides how the loop should react to a failed or degraded LLM turn. Recovery is
classified from *structured* error fields (HTTP status, error kind) — never by
substring-matching an error message (mirrors :mod:`infra.llm.taxonomy`, which is
the authoritative classifier exercised in the taxonomy unit tests, L21).

To keep ``core`` free of any ``infra`` import, the inline-JSON tool-call scanner
algorithm is *copied* here as a pure function (``parse_inline_json_tool_calls``)
rather than imported from ``infra.llm.recovery_mw``.

All thresholds live in :class:`~autoverse.core.context.config.RecoveryConfig`.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from enum import Enum

from autoverse.core.context.config import RecoveryConfig
from autoverse.types.messages import (
    Message,
    TextBlock,
    ToolUseBlock,
)


class RecoveryClass(Enum):
    """The five recovery routes for a failed/degraded LLM turn."""

    NONE = "none"                  # no error -> nothing to recover
    RETRY_SAME = "retry_same"      # transient: retry the same model/transport
    FALLBACK_MODEL = "fallback"    # auth/unsupported: switch model/transport
    COMPACT_RETRY = "compact_retry"  # context overflow: compact then retry
    UNRECOVERABLE = "unrecoverable"  # permanent / aborted: surface to caller


@dataclass(frozen=True)
class RecoveryAction:
    cls: RecoveryClass
    should_retry: bool = False
    should_fallback: bool = False
    should_compact: bool = False
    reason: str = ""


# ── inline-JSON scanner (copied from infra.llm.recovery_mw; core-local) ───────
def _try_parse(blob: str) -> dict[str, object] | None:
    candidate = blob.strip()
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    cleaned = candidate.replace(",}", "}").replace(",]", "]")
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        obj = ast.literal_eval(candidate)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError):
        pass
    return None


def _scan_objects(text: str) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    parsed = _try_parse(text[start : i + 1])
                    if parsed is not None:
                        out.append(parsed)
                    start = -1
    return out


def parse_inline_json_tool_calls(
    text: str, executed: set[str]
) -> list[dict[str, object]]:
    """Extract tool_use objects embedded as plain text (core-local copy).

    Each recovered dict has keys ``id``/``name``/``input`` suitable for
    ``ToolUseBlock(**tu)``. Echoed ids already in ``executed`` are dropped.
    """
    recovered: list[dict[str, object]] = []
    for obj in _scan_objects(text):
        if obj.get("type") != "tool_use":
            continue
        tool_id = str(obj.get("id", ""))
        name = str(obj.get("name", ""))
        raw_input = obj.get("input", {})
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        if tool_id and tool_id in executed:
            continue
        if name == "":
            continue
        recovered.append({"id": tool_id, "name": name, "input": tool_input})
    return recovered


class RecoveryPolicy:
    def __init__(self, config: RecoveryConfig | None = None) -> None:
        self._cfg = config or RecoveryConfig()
        self._retries = 0
        self._fallbacks = 0
        self._resumes = 0
        self._pending_token_override: int | None = None

    # ── classification (structured fields only, no substring matching) ───────
    def classify(self, error: object | None) -> RecoveryAction:
        if error is None:
            return RecoveryAction(cls=RecoveryClass.NONE)
        if bool(getattr(error, "response_started", False)):
            return RecoveryAction(
                cls=RecoveryClass.UNRECOVERABLE, reason="partial_response"
            )
        if bool(getattr(error, "retry_exhausted", False)):
            return RecoveryAction(
                cls=RecoveryClass.UNRECOVERABLE, reason="retry_exhausted"
            )
        status = getattr(error, "http_status", None)
        kind = self._error_kind(error)

        if kind in ("abort", "bad_request"):
            return RecoveryAction(cls=RecoveryClass.UNRECOVERABLE, reason=kind)
        if kind == "context_overflow":
            return RecoveryAction(
                cls=RecoveryClass.COMPACT_RETRY, should_compact=True, reason=kind
            )
        if kind in ("auth", "unsupported"):
            return RecoveryAction(
                cls=RecoveryClass.FALLBACK_MODEL, should_fallback=True, reason=kind
            )
        if kind in ("rate_limit", "server", "timeout"):
            return RecoveryAction(
                cls=RecoveryClass.RETRY_SAME, should_retry=True, reason=kind
            )
        # fall back to HTTP status when no structured kind is present.
        if isinstance(status, int):
            if status in (408, 425, 429) or status >= 500:
                return RecoveryAction(
                    cls=RecoveryClass.RETRY_SAME, should_retry=True, reason=str(status)
                )
            if status in (401, 403):
                return RecoveryAction(
                    cls=RecoveryClass.FALLBACK_MODEL,
                    should_fallback=True,
                    reason=str(status),
                )
            if status == 400:
                return RecoveryAction(
                    cls=RecoveryClass.UNRECOVERABLE, reason=str(status)
                )
        return RecoveryAction(cls=RecoveryClass.FALLBACK_MODEL, should_fallback=True)

    @staticmethod
    def _error_kind(error: object) -> str:
        kind = getattr(error, "error_kind", None)
        if isinstance(kind, str) and kind:
            return kind
        kind_enum = getattr(error, "kind", None)
        value = getattr(kind_enum, "value", None)
        if isinstance(value, str):
            return value
        return ""

    def can_retry(self) -> bool:
        return self._retries < self._cfg.max_retries

    def can_fallback(self) -> bool:
        return self._fallbacks < self._cfg.max_fallbacks

    def note_retry(self) -> None:
        self._retries += 1

    @property
    def retry_count(self) -> int:
        return self._retries

    def note_fallback(self) -> None:
        self._fallbacks += 1

    def begin_turn(self) -> None:
        """Reset recovery budgets; limits are per user turn, not per session."""
        self._retries = 0
        self._fallbacks = 0
        self._resumes = 0
        self._pending_token_override = None

    # ── MAX_TOKENS text resume (L19) ──────────────────────────────────────---
    def maybe_resume_text(self, stop_reason: str) -> bool:
        """Return True if a MAX_TOKENS-truncated turn should be auto-resumed.

        Schedules a one-shot ``max_tokens`` bonus for the resumed request.
        """
        if stop_reason != "MAX_TOKENS":
            return False
        if self._resumes >= self._cfg.max_resume_text:
            return False
        self._resumes += 1
        self._pending_token_override = self._cfg.resume_token_bonus
        return True

    def consume_max_tokens_override(self) -> int | None:
        """Return and clear a pending one-shot ``max_tokens`` bonus, else None."""
        override = self._pending_token_override
        self._pending_token_override = None
        return override

    # ── truncated tool_use repair (L20) ──────────────────────────────────---
    def repair_truncated_tool_use(
        self, message: Message, stop_reason: str
    ) -> Message:
        """Drop a trailing, truncated tool_use block from a MAX_TOKENS turn.

        When a turn is cut off mid-tool-call (stop_reason MAX_TOKENS) the final
        ToolUseBlock is unreliable; we strip it so the message contains only
        complete blocks. If stripping would empty the message, a placeholder
        text block is inserted.
        """
        if stop_reason != "MAX_TOKENS":
            return message
        blocks = list(message.content)
        if blocks and isinstance(blocks[-1], ToolUseBlock):
            blocks = blocks[:-1]
        if not blocks:
            blocks = [TextBlock(text="(response truncated)")]
        return Message(role=message.role, content=tuple(blocks), metadata=message.metadata)

    # ── inline-JSON tool recovery, gated by capabilities (L18) ──────────────-
    def maybe_recover_json_tools(
        self,
        text: str,
        executed: set[str],
        *,
        degradation_prone: bool,
    ) -> list[ToolUseBlock]:
        """Recover text-embedded tool calls — only for degradation_prone backends.

        Returns the recovered :class:`ToolUseBlock`s (empty when disabled, when
        the backend is not degradation_prone, or when nothing is found).
        """
        if not self._cfg.enable_json_tool_recovery or not degradation_prone:
            return []
        recovered = parse_inline_json_tool_calls(text, executed)
        blocks: list[ToolUseBlock] = []
        for tu in recovered:
            tool_id = str(tu["id"])
            name = str(tu["name"])
            tool_input = tu["input"]
            assert isinstance(tool_input, dict)
            if tool_id:
                executed.add(tool_id)
            blocks.append(ToolUseBlock(id=tool_id, name=name, input=tool_input))
        return blocks


__all__ = [
    "RecoveryClass",
    "RecoveryAction",
    "RecoveryPolicy",
    "parse_inline_json_tool_calls",
]
