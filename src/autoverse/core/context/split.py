"""Pure split / boundary / summary helpers for compaction (docs/architecture/runtime-flow.md).

Everything here is a pure function over message tuples — no I/O, no LLM. The
CompactionPolicy composes these to decide *what* to fold/summarise/truncate;
the policy itself owns the side-effecting orchestration.

A shared ``_estimate_messages`` byte/3 heuristic is the single source of token
estimation across split + view (matches Conversation._estimate_tokens).
"""

from __future__ import annotations

from autoverse.types.messages import (
    ContentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

# Summary marker so prior summaries can be located and merged on re-compaction.
_SUMMARY_MARKER = "[CONVERSATION SUMMARY]"

# Nominal char-equivalent cost charged per image block. Providers tokenise an
# image to a roughly fixed budget regardless of the base64 payload size, so we
# count a flat nominal (≈1k tokens after the byte/3 divide) rather than the
# (potentially multi-MB) base64 length.
_IMAGE_NOMINAL_CHARS = 3_000


def _estimate_block(block: ContentBlock) -> int:
    """Char-count estimate for one content block, aligned with what the wire
    codec actually sends — *not* Python ``repr``.

    Two block kinds must never be estimated by ``repr``:

    * ``ToolResultBlock`` — its ``metadata`` can stash megabytes of base64 image
      data (e.g. ``generate_image``, docs/reference/tools.md §11.7) that is *never* sent to the
      model (the codec serialises only ``content``). Counting the repr would
      inflate the estimate past the truncate ceiling and trigger an endless
      "Context truncated" loop, so only ``content`` is counted.
    * ``ImageBlock`` — providers tokenise an image to a roughly fixed budget, so
      it costs a flat nominal rather than its base64 ``data`` length.
    """
    text = getattr(block, "text", None)
    if isinstance(text, str):  # TextBlock / ThinkingBlock
        return len(text)
    if isinstance(block, ToolResultBlock):
        return len(block.content)
    if isinstance(block, ImageBlock):
        return _IMAGE_NOMINAL_CHARS
    return len(repr(block))


def _estimate_messages(messages: tuple[Message, ...]) -> int:
    """byte/3 heuristic shared across split + conversation view."""
    return sum(_estimate_block(b) for m in messages for b in m.content) // 3


def estimate_summary_prompt(messages: tuple[Message, ...], template: str) -> int:
    """Estimated token cost of summarising *messages* with *template*."""
    return _estimate_messages(messages) + (len(template) // 3)


def _is_tool_noise(msg: Message) -> bool:
    """A message is foldable tool-noise iff every block is a tool use/result."""
    return all(
        isinstance(b, (ToolUseBlock, ToolResultBlock)) for b in msg.content
    )


def adjust_tail_boundary(messages: tuple[Message, ...], idx: int) -> int:
    """Move a candidate split index *forward* so it never severs a tool_use from
    its matching tool_result.

    A boundary is unsafe if a message at/after ``idx`` carries a ToolResultBlock
    whose originating ToolUseBlock lives *before* ``idx``. We advance ``idx``
    until the cut no longer orphans a result from its use.
    """
    n = len(messages)
    if idx <= 0:
        return 0
    if idx >= n:
        return n
    # ids of tool uses kept on the head side (before idx).
    head_use_ids: set[str] = set()
    for m in messages[:idx]:
        for b in m.content:
            if isinstance(b, ToolUseBlock):
                head_use_ids.add(b.id)
    # if the tail's first message answers a head use, advance past it.
    while idx < n:
        first = messages[idx]
        answers_head = any(
            isinstance(b, ToolResultBlock) and b.tool_use_id in head_use_ids
            for b in first.content
        )
        if not answers_head:
            break
        idx += 1
    return idx


def safe_split_idx(messages: tuple[Message, ...], keep_tail: int) -> int:
    """Index that keeps at least ``keep_tail`` trailing messages and never cuts a
    tool_use/result pair. Returns 0 when nothing can be folded."""
    n = len(messages)
    if n <= keep_tail:
        return 0
    raw = n - keep_tail
    return adjust_tail_boundary(messages, raw)


def split_for_micro_compact(
    messages: tuple[Message, ...], keep_tail: int
) -> tuple[tuple[Message, ...], tuple[Message, ...]]:
    """Split into (foldable_head, protected_tail) for micro-compaction.

    Only tool-noise messages in the head are eligible to fold; intermixed
    real content is preserved by returning it in the tail-side untouched is
    not possible here, so micro-compaction keeps any non-noise head message in
    the *kept* bucket via the returned head being the noise-only prefix runs.
    """
    cut = safe_split_idx(messages, keep_tail)
    head = messages[:cut]
    tail = messages[cut:]
    # within the head, only fold contiguous tool-noise; keep the rest.
    foldable = tuple(m for m in head if _is_tool_noise(m))
    kept_head = tuple(m for m in head if not _is_tool_noise(m))
    return foldable, kept_head + tail


def extract_prior_summary(messages: tuple[Message, ...]) -> str:
    """Return the text of an existing summary message (marker-tagged), else ""."""
    for m in messages:
        for b in m.content:
            if isinstance(b, TextBlock) and _SUMMARY_MARKER in b.text:
                return b.text
    return ""


def extract_original_user_goal(messages: tuple[Message, ...]) -> str:
    """Return the text of the earliest user message (the original goal)."""
    for m in messages:
        if m.role == "user":
            for b in m.content:
                if isinstance(b, TextBlock):
                    return b.text
    return ""


def strip_summary_messages(
    messages: tuple[Message, ...],
) -> tuple[Message, ...]:
    """Drop any marker-tagged summary messages.

    Used before re-summarising a head so a prior summary is not folded into the
    fresh summary *and* re-appended via :func:`merge_summaries` (double-count)."""
    return tuple(
        m
        for m in messages
        if not any(
            isinstance(b, TextBlock) and _SUMMARY_MARKER in b.text
            for b in m.content
        )
    )


def merge_summaries(prior: str, fresh: str) -> str:
    """Return one replacement summary carrying the canonical marker.

    The summariser is responsible for consuming any prior summary. Re-appending
    it here made every compaction retain all superseded generations and defeated
    the context reduction over long sessions.
    """
    fresh = fresh.strip()
    if not fresh.startswith(_SUMMARY_MARKER):
        fresh = f"{_SUMMARY_MARKER}\n{fresh}"
    return fresh


def truncate_to_fit(text: str, max_chars: int) -> str:
    """Hard-truncate a string with an explicit elision marker."""
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(_ELISION))
    return text[:keep] + _ELISION


_ELISION = "\n…[truncated]…"


__all__ = [
    "_estimate_block",
    "_estimate_messages",
    "estimate_summary_prompt",
    "adjust_tail_boundary",
    "safe_split_idx",
    "split_for_micro_compact",
    "extract_prior_summary",
    "extract_original_user_goal",
    "strip_summary_messages",
    "merge_summaries",
    "truncate_to_fit",
]
