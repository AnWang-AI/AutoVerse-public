"""Message-list repair helpers for the turn loop (docs/architecture/runtime-flow.md).

Two pure transforms applied just before building an LLM request so the wire
payload is always well-formed:

  * ``repair_orphaned_tool_use`` — every assistant ToolUseBlock must be
    answered by a ToolResultBlock; an unanswered ("orphaned") tool_use gets a
    synthetic placeholder ToolResultBlock so the backend never rejects the
    history (L7).
  * ``merge_trailing_user`` — consecutive ``user`` messages are merged into one
    so an injected continuation prompt does not produce two user turns in a row
    (L8).
"""

from __future__ import annotations

from autoverse.types.messages import (
    Message,
    ToolResultBlock,
    ToolUseBlock,
)

_ORPHAN_PLACEHOLDER = "(no result captured)"


def repair_orphaned_tool_use(messages: list[Message]) -> list[Message]:
    """Insert placeholder tool_results for any unanswered tool_use blocks (L7).

    The placeholder tool_result is inserted *immediately after* the assistant
    message that produced the orphaned tool_use — not appended at the end of the
    history. OpenAI-compatible backends require every ``tool_calls`` message to
    be followed directly by the matching tool messages; if a turn is interrupted
    (Ctrl+C / ESC) mid-tool and the user then types a new prompt, appending the
    repair at the tail would leave the user prompt wedged between the tool_use
    and its result, triggering HTTP 400 ("insufficient tool messages following
    tool_calls message").
    """
    answered: set[str] = set()
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                answered.add(b.tool_use_id)

    out: list[Message] = []
    for m in messages:
        out.append(m)
        if m.role != "assistant":
            continue
        orphans = [
            b.id
            for b in m.content
            if isinstance(b, ToolUseBlock) and b.id not in answered
        ]
        if orphans:
            out.append(
                Message(
                    role="user",
                    content=tuple(
                        ToolResultBlock(
                            tool_use_id=oid,
                            content=_ORPHAN_PLACEHOLDER,
                            is_error=True,
                        )
                        for oid in orphans
                    ),
                )
            )
    return out


def merge_trailing_user(messages: list[Message]) -> list[Message]:
    """Merge any run of consecutive trailing ``user`` messages into one (L8)."""
    if len(messages) < 2:
        return messages
    out: list[Message] = []
    for m in messages:
        if out and out[-1].role == "user" and m.role == "user":
            prev = out[-1]
            out[-1] = Message(
                role="user",
                content=prev.content + m.content,
                metadata=prev.metadata,
            )
        else:
            out.append(m)
    return out


__all__ = ["repair_orphaned_tool_use", "merge_trailing_user"]
