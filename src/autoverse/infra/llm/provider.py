"""WireProvider — the single concrete LLMProvider implementation (§4).

Composes codec + transports + fallback, presenting the LLMProvider Port to core.
Performs the P5 vision pre-check, emits a REQUEST event (KDR-8) and optionally
runs the RecoveryMiddleware for degradation_prone backends.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import AsyncIterator

from autoverse.infra.llm.codec.base import Codec
from autoverse.infra.llm.fallback import FallbackPlan
from autoverse.infra.llm.recovery_mw import RecoveryMiddleware
from autoverse.infra.llm.registry import ModelSpec
from autoverse.infra.llm.transport.base import Transport, WireRequest
from autoverse.types import (
    AbortSignal,
    ContentBlock,
    EventType,
    LLMRequest,
    Message,
    ProviderCapabilities,
    RequestPayload,
    StreamEvent,
    UsagePayload,
    make_event,
)


def _all_blocks(req: LLMRequest) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    for msg in req.messages:
        blocks.extend(msg.content)
    return blocks


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class WireProvider:
    def __init__(
        self,
        spec: ModelSpec,
        codec: Codec,
        transports: list[Transport],
        *,
        fallback: FallbackPlan,
        recovery: RecoveryMiddleware | None,
    ) -> None:
        self.spec = spec
        self._capabilities = spec.capabilities
        self._codec = codec
        self._transports = transports
        self._fallback = fallback
        self._recovery = recovery

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _build_wire_request(self, req: LLMRequest) -> WireRequest:
        from autoverse.types import WireKind

        messages = self._codec.encode_messages(list(req.messages))
        body: dict[str, object] = {
            # Send the upstream-specific wire name. ``model_id`` is our clean
            # internal canonical id (``<source>/<model>``); ``wire_model`` carries
            # what the backend actually expects (OpenRouter's ``vendor/model`` or
            # a gateway's bare id). ``req.model`` may be a user alias — the Router
            # already resolved it into ``self.spec``.
            "model": self.spec.wire_model or self.spec.model_id,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if req.tools:
            encoded_tools = self._codec.encode_tools(list(req.tools))
            # Anthropic prompt-caching (§12): mark the last tool with
            # cache_control so the whole tools array is cached up to that point.
            if (
                self._codec.wire is WireKind.ANTHROPIC
                and self._capabilities.cache
                and encoded_tools
            ):
                last = dict(encoded_tools[-1])
                last["cache_control"] = {"type": "ephemeral"}
                encoded_tools[-1] = last
            body["tools"] = encoded_tools
        if req.system:
            encoded_system = self._codec.encode_system(
                req.system, cache=self._capabilities.cache
            )
            if self._codec.wire is WireKind.ANTHROPIC:
                # Anthropic wire carries system as a top-level field.
                body["system"] = encoded_system
            else:
                # OpenAI wire has no top-level ``system``; it is a message.
                # A bare top-level field is silently ignored by the backend,
                # which would drop the system prompt entirely.
                body["messages"] = [
                    {"role": "system", "content": encoded_system},
                    *messages,
                ]
        if self._capabilities.thinking and req.thinking_budget > 0:
            if self._codec.wire is WireKind.ANTHROPIC:
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": req.thinking_budget,
                }
            else:
                body["reasoning"] = {"max_tokens": req.thinking_budget}
        return WireRequest(body=body, stream=True)

    async def stream(
        self, req: LLMRequest, signal: AbortSignal
    ) -> AsyncIterator[StreamEvent]:
        # P5 pre-check (M6): codec rejects vision when not declared.
        self._codec.supports(_all_blocks(req), self._capabilities)

        tool_names = tuple(t.name for t in req.tools)
        tools_repr = json.dumps(
            [self._codec.encode_tools(list(req.tools))], sort_keys=True
        )
        yield make_event(
            RequestPayload(
                turn_index=0,
                system=req.system,
                system_sha256=_sha256(req.system),
                tool_names=tool_names,
                tools_sha256=_sha256(tools_repr),
                model=req.model or self.spec.model_id,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                thinking_budget=req.thinking_budget,
            )
        )

        wire_req = self._build_wire_request(req)
        # Codecs emit USAGE with an empty ``model`` (they have no knowledge of
        # the registry-resolved canonical id). Stamp it here so the meter /
        # trace / TUI can price the turn — without it every cost is $0 because
        # the meter records under "" which has no pricing entry.
        # Prefer the registry-resolved canonical id (req.model may be an alias).
        model_id = self.spec.model_id or req.model
        if self._recovery is not None:
            self._recovery.reset()
        try:
            async for ev in self._fallback.run(
                req,
                signal,
                codec=self._codec,
                transports=self._transports,
                wire_req=wire_req,
            ):
                ev = self._stamp_model(ev, model_id)
                if self._recovery is not None:
                    yield self._recovery.observe(ev)
                else:
                    yield ev
            if self._recovery is not None:
                for ev in self._recovery.flush():
                    yield self._stamp_model(ev, model_id)
        except Exception:  # noqa: BLE001 — reset recovery state before re-raising
            if self._recovery is not None:
                self._recovery.reset()
            raise

    @staticmethod
    def _stamp_model(ev: StreamEvent, model_id: str) -> StreamEvent:
        """Fill the canonical model id on USAGE events (codecs leave it blank)."""
        if ev.type is EventType.USAGE and isinstance(ev.payload, UsagePayload):
            if not ev.payload.model:
                return make_event(dataclasses.replace(ev.payload, model=model_id))
        return ev

    async def count_tokens(self, req: LLMRequest) -> int:
        return estimate_request_tokens(req)

    async def aclose(self) -> None:
        for transport in self._transports:
            close = getattr(transport, "aclose", None)
            if close is not None:
                await close()


def _message_chars(msg: Message) -> int:
    from autoverse.types import (
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        ImageBlock,
    )

    total = 0
    for b in msg.content:
        if isinstance(b, (TextBlock, ThinkingBlock)):
            total += len(b.text)
        elif isinstance(b, ToolResultBlock):
            total += len(b.content)
        elif isinstance(b, ToolUseBlock):
            total += len(b.name) + len(json.dumps(b.input, ensure_ascii=False))
        elif isinstance(b, ImageBlock):
            total += 4_000
    return total


def estimate_request_tokens(req: LLMRequest) -> int:
    """Conservative network-free estimate over the complete request payload."""
    total = len(req.system)
    total += sum(_message_chars(message) for message in req.messages)
    for tool in req.tools:
        total += len(tool.name) + len(tool.description)
        total += len(json.dumps(tool.json_schema, ensure_ascii=False, sort_keys=True))
    return max(1, total // 4)


__all__ = ["WireProvider"]
