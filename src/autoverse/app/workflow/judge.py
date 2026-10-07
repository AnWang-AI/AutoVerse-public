"""LLM-backed completion judge for ``/goal`` (docs/reference/workflows.md §6.3).

The :class:`GoalGate` externalises "am I done?" from the model's self-perception
into an *independent judge* that re-checks a declarative completion condition each
turn. This module supplies the production judge: a single, low-temperature LLM
call that must answer with strict JSON ``{"done": bool, "reason": str}``.

Robustness over cleverness (§6.4 / P5):

* A *malfunction* (transport error, empty response) is surfaced as
  ``is_judge_error=True`` — never as "not achieved" — so the breaker can trip
  rather than feeding a fault back as negative feedback.
* A well-formed-but-non-JSON answer degrades to a conservative heuristic rather
  than raising, so a chatty model does not look like an outage.

``app`` may import ``core``/``infra``/``types`` (P3); this module touches only the
``LLMProvider`` Port, so the judge is backend-agnostic and trivially testable.
"""

from __future__ import annotations

import asyncio
import json
from typing import Sequence

from autoverse.app.workflow.goal import JudgeFn, JudgeResult
from autoverse.ports import LLMProvider
from autoverse.types.abort import AbortController
from autoverse.types.events import EventType, TextDeltaPayload, UsagePayload
from autoverse.types.llm import LLMRequest
from autoverse.types.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock

_JUDGE_SYSTEM = (
    "You are an impartial completion judge. Given a COMPLETION CONDITION and a "
    "transcript of an AI agent's work, decide whether the condition is now fully "
    "satisfied. Reply with a SINGLE line of strict JSON and nothing else:\n"
    '{"done": true|false, "reason": "<short reason>"}\n'
    "Be conservative: answer true only when you are confident the condition holds."
)


def _transcript_text(transcript: Sequence[object], *, limit: int = 12) -> str:
    """Render the tail of the transcript as ``role: text`` lines (judge input)."""
    lines: list[str] = []
    for msg in list(transcript)[-limit:]:
        role = getattr(msg, "role", "?")
        content = getattr(msg, "content", ())
        if isinstance(content, (list, tuple)):
            parts: list[str] = []
            for block in content:
                if isinstance(block, TextBlock) and block.text:
                    parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    args = json.dumps(block.input, ensure_ascii=False, default=str)
                    parts.append(f"[tool call: {block.name} {args[:1000]}]")
                elif isinstance(block, ToolResultBlock):
                    state = "error" if block.is_error else "result"
                    parts.append(
                        f"[tool {state}: {block.content[:2000]}]"
                    )
            text = " ".join(parts)
        else:
            text = str(content)
        text = text.strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _extract_json(raw: str) -> dict[str, object] | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse(raw: str) -> JudgeResult:
    text = raw.strip()
    if not text:
        # Empty stream = malfunction, never "not achieved" (§6.4).
        return JudgeResult(ok=False, reason="empty judge response", is_judge_error=True)
    obj = _extract_json(text)
    if obj is not None and "done" in obj:
        return JudgeResult(ok=bool(obj.get("done")), reason=str(obj.get("reason", "")))
    return JudgeResult(
        ok=False,
        reason=f"malformed judge response: {text[:160]}",
        is_judge_error=True,
    )


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.TimeoutError):
        return True
    text = str(exc).lower()
    if any(word in text for word in ("timeout", "timed out", "overloaded", "rate limit")):
        return True
    return any(code in text for code in ("429", "500", "502", "503", "504", "529"))


def make_llm_judge(
    provider: LLMProvider,
    *,
    model: str = "",
    max_tokens: int = 256,
    timeout: float = 30.0,
    max_retries: int = 2,
) -> JudgeFn:
    """Build a :data:`JudgeFn` that asks ``provider`` for a strict-JSON verdict.

    Transport failures propagate (the :class:`GoalEvaluator` converts them into
    ``is_judge_error=True``); malformed/empty *responses* are handled here.
    """

    async def judge(condition: str, transcript: Sequence[object]) -> JudgeResult:
        prompt = (
            f"COMPLETION CONDITION:\n{condition}\n\n"
            f"TRANSCRIPT:\n{_transcript_text(transcript)}\n\n"
            "Is the condition fully satisfied?"
        )
        req = LLMRequest(
            messages=(Message(role="user", content=(TextBlock(text=prompt),)),),
            system=_JUDGE_SYSTEM,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        last = JudgeResult(
            ok=False, reason="judge unavailable", is_judge_error=True
        )
        tokens = 0
        for attempt in range(max_retries + 1):
            signal = AbortController().signal
            chunks: list[str] = []
            attempt_tokens = 0

            async def _collect() -> None:
                nonlocal attempt_tokens
                async for event in provider.stream(req, signal):
                    if event.type is EventType.TEXT_DELTA and isinstance(
                        event.payload, TextDeltaPayload
                    ):
                        chunks.append(event.payload.text)
                    elif event.type is EventType.USAGE and isinstance(
                        event.payload, UsagePayload
                    ):
                        attempt_tokens += event.payload.usage.total

            try:
                await asyncio.wait_for(_collect(), timeout=timeout)
                tokens += attempt_tokens
                result = _parse("".join(chunks))
                result = JudgeResult(
                    ok=result.ok,
                    reason=result.reason,
                    tokens_used=tokens,
                    is_judge_error=result.is_judge_error,
                )
                if not result.is_judge_error:
                    return result
                last = result
            except asyncio.TimeoutError:
                tokens += attempt_tokens
                last = JudgeResult(
                    ok=False,
                    reason=f"judge timed out after {timeout:g}s",
                    tokens_used=tokens,
                    is_judge_error=True,
                )
            except Exception as exc:  # noqa: BLE001 — report judge failure as data
                tokens += attempt_tokens
                last = JudgeResult(
                    ok=False,
                    reason=f"judge error: {exc}",
                    tokens_used=tokens,
                    is_judge_error=True,
                )
                if not _is_transient(exc):
                    break
            if attempt < max_retries:
                await asyncio.sleep(min(1.0, 0.25 * (2**attempt)))
        return last

    return judge


__all__ = ["make_llm_judge"]
