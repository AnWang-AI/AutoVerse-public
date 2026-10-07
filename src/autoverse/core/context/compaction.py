"""CompactionPolicy — context-window management cascade (docs/architecture/runtime-flow.md).

The policy is the *only* side-effecting orchestrator of compaction; all pure
decisions are delegated to :mod:`autoverse.core.context.split`. The cascade:

  should_compact   -> token estimate vs. CompactionConfig thresholds
  maybe_compact    -> micro-fold tool noise (+COMPACTING(micro)), honouring a
                      cooldown; emergency-truncate when over the hard ceiling
  force_compact    -> meta summary via the LLM; self-limits the summary call and
                      falls back to overflow_truncate when even that fails

A calibrated EMA tracks the gap between our byte/3 estimate and the provider's
reported usage so the thresholds drift toward reality over a session.

The policy emits :class:`CompactingPayload` events; the actual message rewrite
is returned to the caller (the loop) via :class:`CompactOutcome` so the
Conversation stays the single writer (store-first).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from autoverse.core.context.config import CompactionConfig
from autoverse.core.context.split import (
    _estimate_messages,
    extract_original_user_goal,
    extract_prior_summary,
    merge_summaries,
    split_for_micro_compact,
    truncate_to_fit,
)
from autoverse.core.context.trace_index import strip_trace_blocks
from autoverse.core.context.summary_template import SUMMARY_TEMPLATE
from autoverse.ports import CompactionArchivePort, EventSink, LLMProvider
from autoverse.types.abort import AbortController, AbortError, AbortSignal
from autoverse.types import (
    CompactingPayload,
    EventType,
    LLMRequest,
    TextDeltaPayload,
    CompactionSegmentRef,
    make_event,
)
from autoverse.types.messages import Message, TextBlock

# Safe fallback when neither a live-model window provider nor an explicit config
# override is available — keeps the cascade thresholds positive.
_DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass
class CompactOutcome:
    """Result of a compaction attempt handed back to the loop."""

    changed: bool
    kind: str = ""
    messages: tuple[Message, ...] = ()
    tokens_before: int = 0
    tokens_after: int = 0
    summary: str = ""
    folded: tuple[Message, ...] = ()
    archive_ref: CompactionSegmentRef | None = None


class ContextBudgetError(RuntimeError):
    """The immutable request overhead cannot fit the active model window."""

    error_kind = "context_overflow"


@dataclass
class _CalibState:
    ema_ratio: float = 1.0
    turns_since_compact: int = field(default=10_000)
    # graded cooldown (docs/architecture/runtime-flow.md §4.7): target turns to wait, set per
    # compaction from the achieved reduction ratio; consecutive low-reduction
    # compactions escalate toward the "stuck" cooldown.
    cooldown_target: int = 0
    consecutive_low: int = 0


class CompactionPolicy:
    def __init__(
        self,
        llm: LLMProvider,
        config: CompactionConfig | None = None,
        window_provider: Callable[[], int] | None = None,
        archive: CompactionArchivePort | None = None,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self._cfg = config or CompactionConfig()
        self._calib = _CalibState()
        # When wired (app layer), this returns the *active* model's context
        # window so the thresholds re-target after a ``/model`` switch instead of
        # using a stale, hard-coded window. ``None`` (and tests) → static config.
        self._window_provider = window_provider
        self._archive = archive
        self._session_id = session_id

    def _window(self) -> int:
        """The effective context window the cascade thresholds are fractions of.

        Resolution order: the live model window (``window_provider`` — so
        ``/model`` re-targets compaction) → an explicit config override → a safe
        default. A provider failure or non-positive value falls through so the
        window is never zero (which would make every turn "over threshold")."""
        if self._window_provider is not None:
            try:
                live = int(self._window_provider() or 0)
            except Exception:  # noqa: BLE001 — window lookup must not break the loop
                live = 0
            if live > 0:
                return live
        if self._cfg.context_window > 0:
            return self._cfg.context_window
        return _DEFAULT_CONTEXT_WINDOW

    # ── estimation / calibration ────────────────────────────────────────────
    def estimate(self, messages: tuple[Message, ...]) -> int:
        return int(_estimate_messages(messages) * self._calib.ema_ratio)

    def calibrate(self, raw_estimate: int, actual_tokens: int) -> None:
        """Update the EMA ratio between actual usage and our raw estimate."""
        if raw_estimate <= 0 or actual_tokens <= 0:
            return
        observed = min(4.0, max(0.25, actual_tokens / raw_estimate))
        a = self._cfg.ema_alpha
        self._calib.ema_ratio = (1 - a) * self._calib.ema_ratio + a * observed

    def _calibrate_cooldown(self) -> None:
        self._calib.turns_since_compact += 1

    def note_turn(self) -> None:
        self._calibrate_cooldown()

    # ── decisions ─────────────────────────────────────────────────────────--
    # Thresholds are computed against the *live* window (``_window()``) rather
    # than the config's static ``context_window``, so a ``/model`` switch (or a
    # model-derived default) re-targets the cascade. The ratios still come from
    # config; only the multiplicand is dynamic.
    def should_compact(self, messages: tuple[Message, ...]) -> bool:
        return self.estimate(messages) >= int(self._window() * self._cfg.micro_ratio)

    def _over_hard_ceiling(self, messages: tuple[Message, ...]) -> bool:
        return self.estimate(messages) >= int(self._window() * self._cfg.truncate_ratio)

    def _over_compact_threshold(self, messages: tuple[Message, ...]) -> bool:
        return self.estimate(messages) >= int(self._window() * self._cfg.compact_ratio)

    def _cooldown_active(self) -> bool:
        return self._calib.turns_since_compact < self._calib.cooldown_target

    def _grade_cooldown(self, before: int, after: int) -> None:
        """Set the graded cooldown from the achieved reduction ratio (§4.7).

        Effective reduction (>= low_reduction_ratio) -> short cooldown and reset
        the low streak. Low reduction -> longer cooldown; a repeated low streak
        escalates to the 'stuck' cooldown to avoid thrashing on an incompressible
        history."""
        reduction = 0.0 if before <= 0 else (before - after) / before
        self._calib.turns_since_compact = 0
        if reduction >= self._cfg.low_reduction_ratio:
            self._calib.consecutive_low = 0
            self._calib.cooldown_target = self._cfg.cooldown_effective
        else:
            self._calib.consecutive_low += 1
            self._calib.cooldown_target = (
                self._cfg.cooldown_stuck
                if self._calib.consecutive_low >= 2
                else self._cfg.cooldown_low
            )

    # ── micro / emergency ─────────────────────────────────────────────────--
    async def maybe_compact(
        self,
        messages: tuple[Message, ...],
        events: EventSink,
        *,
        request: LLMRequest | None = None,
        turn_index: int = 0,
        signal: AbortSignal | None = None,
    ) -> CompactOutcome:
        """Run the automatic compaction cascade (docs/architecture/runtime-flow.md).

        The cascade, in order:
          * over the hard ceiling -> emergency truncate (ignores cooldown);
          * below the micro threshold -> no change;
          * cooldown active -> no change;
          * micro-fold tool noise when any is foldable (cheap, no LLM);
          * else, at/above the meta threshold -> escalate to an LLM summary
            (``force_compact``) instead of waiting for the hard ceiling.

        Emits COMPACTING(micro|overflow|truncate)."""
        before = await self._measure(messages, request)
        if before >= int(self._window() * self._cfg.truncate_ratio):
            return await self._emergency_truncate(
                messages, events, before, request=request, turn_index=turn_index
            )
        if before < int(self._window() * self._cfg.micro_ratio):
            return CompactOutcome(changed=False)
        if self._cooldown_active():
            return CompactOutcome(changed=False)

        foldable, rest = split_for_micro_compact(
            messages, self._cfg.micro_keep_tail
        )
        if not foldable:
            # Nothing cheap left to fold: escalate to an LLM meta-summary once we
            # are over the compact threshold, so the history is summarised before
            # it grows into the hard-ceiling truncate band.
            if before >= int(self._window() * self._cfg.compact_ratio):
                return await self.force_compact(
                    messages,
                    events,
                    request=request,
                    turn_index=turn_index,
                    signal=signal,
                )
            return CompactOutcome(changed=False)
        archive_ref = await self._archive_messages(
            foldable, kind="micro", turn_index=turn_index
        )
        note = self._archive_note(archive_ref)
        new_messages = ((note,) + rest) if note is not None else rest
        after = await self._measure(new_messages, request)
        if after >= before:
            return CompactOutcome(
                changed=False,
                kind="no_reduction",
                messages=messages,
                tokens_before=before,
                tokens_after=after,
                archive_ref=archive_ref,
            )
        self._grade_cooldown(before, after)
        await events.emit(
            make_event(
                CompactingPayload(
                    kind="micro",
                    tokens_before=before,
                    tokens_after=after,
                    messages_before=len(messages),
                    messages_after=len(new_messages),
                    archive_segment_id=archive_ref.segment_id if archive_ref else "",
                    folded_count=len(foldable),
                    folded_sha256=archive_ref.content_sha256 if archive_ref else "",
                )
            )
        )
        return CompactOutcome(
            changed=True,
            kind="micro",
            messages=new_messages,
            tokens_before=before,
            tokens_after=after,
            folded=foldable,
            archive_ref=archive_ref,
        )

    async def _emergency_truncate(
        self,
        messages: tuple[Message, ...],
        events: EventSink,
        before: int,
        *,
        request: LLMRequest | None,
        turn_index: int,
    ) -> CompactOutcome:
        archive_ref = await self._archive_messages(
            messages, kind="truncate", turn_index=turn_index
        )
        truncated = self._truncate_messages(messages, archive_ref)
        after = await self._measure(truncated, request)
        if after >= before:
            return CompactOutcome(
                changed=False,
                kind="no_reduction",
                messages=messages,
                tokens_before=before,
                tokens_after=after,
                archive_ref=archive_ref,
            )
        self._grade_cooldown(before, after)
        await events.emit(
            make_event(
                CompactingPayload(
                    kind="truncate",
                    tokens_before=before,
                    tokens_after=after,
                    messages_before=len(messages),
                    messages_after=len(truncated),
                    archive_segment_id=archive_ref.segment_id if archive_ref else "",
                    folded_count=len(messages),
                    folded_sha256=archive_ref.content_sha256 if archive_ref else "",
                )
            )
        )
        return CompactOutcome(
            changed=True,
            kind="truncate",
            messages=truncated,
            tokens_before=before,
            tokens_after=after,
            archive_ref=archive_ref,
        )

    def _truncate_messages(
        self,
        messages: tuple[Message, ...],
        archive_ref: CompactionSegmentRef | None = None,
    ) -> tuple[Message, ...]:
        tail = max(1, self._cfg.protected_tail)
        kept = messages[-tail:]
        goal = extract_original_user_goal(messages)
        prior = extract_prior_summary(messages)
        head_text = prior or goal or "(earlier conversation truncated)"
        total_chars = max(192, int(self._window() * self._cfg.compact_ratio * 3))
        per_message = max(64, total_chars // (len(kept) + 1))
        note = self._archive_note_text(archive_ref)
        head = Message(
            role="system",
            content=(
                TextBlock(
                    text=truncate_to_fit(
                        f"{head_text}{note}",
                        min(self._cfg.max_message_chars, per_message),
                    )
                ),
            ),
        )
        bounded_tail = tuple(
            Message(
                role=message.role,
                content=(
                    TextBlock(
                        text=truncate_to_fit(
                            self._message_text(message),
                            min(self._cfg.max_message_chars, per_message),
                        )
                    ),
                ),
            )
            for message in kept
        )
        return (head,) + bounded_tail

    # ── meta summary ──────────────────────────────────────────────────────--
    async def force_compact(
        self,
        messages: tuple[Message, ...],
        events: EventSink,
        *,
        request: LLMRequest | None = None,
        turn_index: int = 0,
        signal: AbortSignal | None = None,
    ) -> CompactOutcome:
        """Generate an LLM summary; on failure fall back to overflow_truncate."""
        before = await self._measure(messages, request)
        tail = self._cfg.protected_tail
        head = messages[:-tail] if len(messages) > tail else messages
        kept = messages[-tail:] if len(messages) > tail else ()
        # Rolling replacement: the summariser consumes the prior summary together
        # with newly foldable history and produces one replacement summary.  Never
        # append the full superseded summary, which would grow without bound.
        to_summarize = head
        archive_ref = await self._archive_messages(
            head, kind="summary", turn_index=turn_index
        )
        summary = await self._summarize_with_retry(
            to_summarize, signal or _NULL_SIGNAL, events
        )
        if summary is None:
            truncated = self._truncate_messages(messages, archive_ref)
            after = await self._measure(truncated, request)
            if after >= before:
                return CompactOutcome(
                    changed=False,
                    kind="no_reduction",
                    messages=messages,
                    tokens_before=before,
                    tokens_after=after,
                    archive_ref=archive_ref,
                )
            self._grade_cooldown(before, after)
            await events.emit(
                make_event(
                    CompactingPayload(
                        kind="overflow_truncate",
                        tokens_before=before,
                        tokens_after=after,
                        messages_before=len(messages),
                        messages_after=len(truncated),
                        archive_segment_id=archive_ref.segment_id if archive_ref else "",
                        folded_count=len(head),
                        folded_sha256=archive_ref.content_sha256 if archive_ref else "",
                    )
                )
            )
            return CompactOutcome(
                changed=True,
                kind="overflow_truncate",
                messages=truncated,
                tokens_before=before,
                tokens_after=after,
                archive_ref=archive_ref,
            )

        merged = merge_summaries(extract_prior_summary(messages), summary)
        merged = strip_trace_blocks(merged) + self._archive_note_text(archive_ref)
        summary_msg = Message(role="system", content=(TextBlock(text=merged),))
        new_messages = (summary_msg,) + tuple(kept)
        after = await self._measure(new_messages, request)
        if after >= before:
            truncated = self._truncate_messages(messages, archive_ref)
            truncated_after = await self._measure(truncated, request)
            if truncated_after >= before:
                return CompactOutcome(
                    changed=False,
                    kind="no_reduction",
                    messages=messages,
                    tokens_before=before,
                    tokens_after=truncated_after,
                    archive_ref=archive_ref,
                )
            self._grade_cooldown(before, truncated_after)
            await events.emit(
                make_event(
                    CompactingPayload(
                        kind="overflow_truncate",
                        tokens_before=before,
                        tokens_after=truncated_after,
                        messages_before=len(messages),
                        messages_after=len(truncated),
                        archive_segment_id=(
                            archive_ref.segment_id if archive_ref else ""
                        ),
                        folded_count=len(head),
                        folded_sha256=(
                            archive_ref.content_sha256 if archive_ref else ""
                        ),
                    )
                )
            )
            return CompactOutcome(
                changed=True,
                kind="overflow_truncate",
                messages=truncated,
                tokens_before=before,
                tokens_after=truncated_after,
                archive_ref=archive_ref,
            )
        self._grade_cooldown(before, after)
        await events.emit(
            make_event(
                CompactingPayload(
                    kind="overflow",
                    tokens_before=before,
                    tokens_after=after,
                    messages_before=len(messages),
                    messages_after=len(new_messages),
                    summary=merged,
                    archive_segment_id=archive_ref.segment_id if archive_ref else "",
                    folded_count=len(head),
                    folded_sha256=archive_ref.content_sha256 if archive_ref else "",
                )
            )
        )
        return CompactOutcome(
            changed=True,
            kind="overflow",
            messages=new_messages,
            tokens_before=before,
            tokens_after=after,
            summary=merged,
            folded=head,
            archive_ref=archive_ref,
        )

    async def _measure(
        self, messages: tuple[Message, ...], request: LLMRequest | None
    ) -> int:
        if request is None:
            return self.estimate(messages)
        measured_request = replace(request, messages=messages)
        try:
            prompt = await self._llm.count_tokens(measured_request)
        except Exception:  # noqa: BLE001 — conservative local fallback
            prompt = self.estimate(messages) + len(request.system) // 3
        # ``max_tokens`` is an upper bound, not a prediction that every request
        # will consume the entire allowance. Reserving an 8192 default against an
        # 8192-window local/echo model would make every request impossible. Cap
        # the planning reserve at 20% of the active window; large production
        # windows still reserve the configured output allowance in full.
        reserve = min(
            max(0, measured_request.max_tokens), max(512, self._window() // 5)
        )
        safety = max(256, int(self._window() * 0.02))
        return int(prompt * self._calib.ema_ratio) + reserve + safety

    async def fits(
        self, messages: tuple[Message, ...], request: LLMRequest
    ) -> bool:
        """Whether the complete request plus reserves fits the active window."""
        return await self._measure(messages, request) <= self._window()

    async def _archive_messages(
        self, messages: tuple[Message, ...], *, kind: str, turn_index: int
    ) -> CompactionSegmentRef | None:
        if not messages or self._archive is None or not self._session_id:
            return None
        return await self._archive.append(
            self._session_id,
            kind=kind,
            messages=messages,
            # Messages currently do not carry authoritative per-turn provenance.
            # Use a conservative session range instead of falsely claiming every
            # folded head message originated in the current turn.
            turn_start=0,
            turn_end=max(0, turn_index),
        )

    @staticmethod
    def _archive_note(ref: CompactionSegmentRef | None) -> Message | None:
        if ref is None:
            return None
        return Message(
            role="system",
            content=(TextBlock(text=CompactionPolicy._archive_note_text(ref).lstrip()),),
        )

    @staticmethod
    def _archive_note_text(ref: CompactionSegmentRef | None) -> str:
        if ref is None:
            return ""
        return (
            "\n\n[COMPACTED CONTEXT]\n"
            f"segment={ref.segment_id} kind={ref.kind} "
            f"turns={ref.turn_start}–{ref.turn_end}\n"
            "Use list/grep/search/read_compacted_context only when a missing detail matters."
        )

    @staticmethod
    def _message_text(message: Message) -> str:
        parts: list[str] = []
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
            elif hasattr(block, "content"):
                parts.append(str(getattr(block, "content", "")))
            elif hasattr(block, "input"):
                parts.append(json.dumps(getattr(block, "input", {}), ensure_ascii=False))
        return "\n".join(parts) or "(non-text content archived)"

    async def _summarize_with_retry(
        self,
        messages: tuple[Message, ...],
        signal: AbortSignal | None = None,
        events: EventSink | None = None,
    ) -> str | None:
        active_signal = signal or _NULL_SIGNAL
        attempts = self._cfg.summary_retries + 1
        for _ in range(attempts):
            try:
                return await self._summarize(messages, active_signal, events)
            except AbortError:
                raise
            except Exception:  # noqa: BLE001 — fall through to next attempt/fallback
                continue
        return None

    async def _summarize(
        self,
        messages: tuple[Message, ...],
        signal: AbortSignal | None = None,
        events: EventSink | None = None,
    ) -> str:
        active_signal = signal or _NULL_SIGNAL
        template = self._cfg.summary_template or SUMMARY_TEMPLATE
        req = LLMRequest(
            messages=tuple(messages),
            system=template,
            tools=(),
            max_tokens=self._cfg.summary_budget,
            # Meta-compaction is a faithful reduction task, not a creative
            # response.  A deterministic temperature also avoids the observed
            # reasoning-model failure mode where retries consume the entire
            # output budget in variable hidden reasoning and return no summary.
            temperature=0.0,
        )
        parts: list[str] = []
        async for ev in self._llm.stream(req, active_signal):
            if ev.type is EventType.TEXT_DELTA:
                payload = ev.payload
                assert isinstance(payload, TextDeltaPayload)
                parts.append(payload.text)
            elif ev.type is EventType.USAGE and events is not None:
                # Summary text/thinking are internal context-management data,
                # but their provider usage is real billable work.  Surface only
                # USAGE so trace/meter accounting remains complete without
                # rendering the hidden summarizer stream in the frontend.
                await events.emit(ev)
        text = "".join(parts).strip()
        if not text:
            raise ValueError("empty summary")
        return text


# A never-aborted signal for internal summary calls.
_NULL_SIGNAL = AbortController().signal


__all__ = ["CompactionPolicy", "CompactOutcome", "ContextBudgetError"]
