"""Gateway acceptance scenarios G1-G8.

Given/When/Then style. Uses a FakePlatformAdapter implementing the
PlatformAdapter Protocol and a hand-rolled fake EngineHandle that yields a
scripted StreamEvent sequence — no real network or LLM needed (docs/reference/extensions.md).
Test taxonomy is documented in docs/reference/testing.md.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest

from autoverse.frontends.gateway.adapter import (
    EngineInput,
    OnInbound,
    OutboundRenderer,
    PlatformRegistry,
    normalize_inbound,
)
from autoverse.frontends.gateway.approval import (
    ApprovalRegistry,
    GatewayPermissionPort,
)
from autoverse.frontends.gateway.runner import (
    BackoffPolicy,
    GatewayRunner,
    RateLimitError,
)
from autoverse.frontends.gateway.types import (
    InboundAttachment,
    InboundMessage,
    OutboundMessage,
    PlatformCapabilities,
)
from autoverse.ports import EngineHandle, EventSink, PermissionPort
from autoverse.types import ImageBlock, StreamEvent
from autoverse.types.events import (
    EventType,
    TextDeltaPayload,
    ToolEndPayload,
    TurnEndPayload,
    make_event,
)
from autoverse.types.tools import CapabilityTier, Decision, ToolCall


# ── fakes ──────────────────────────────────────────────────────────────────


class FakePlatformAdapter:
    """A PlatformAdapter that scripts inbound messages and records outbound."""

    def __init__(
        self,
        name: str = "fake",
        bot_id: str = "default",
        *,
        capabilities: PlatformCapabilities | None = None,
        fail_sends: int = 0,
    ) -> None:
        self.name = name
        self.bot_id = bot_id
        self.capabilities = capabilities or PlatformCapabilities()
        self.sent: list[OutboundMessage] = []
        self.edits: list[tuple[str, str, str]] = []
        self._on_inbound: OnInbound | None = None
        self._connected = False
        self._fail_sends = fail_sends

    def bind(self, on_inbound: OnInbound) -> None:
        self._on_inbound = on_inbound

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, msg: OutboundMessage) -> str:
        if self._fail_sends > 0:
            self._fail_sends -= 1
            raise RateLimitError(retry_after=0.0)
        self.sent.append(msg)
        return f"mid-{len(self.sent)}"

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    @asynccontextmanager
    async def _typing_cm(self) -> AsyncIterator[None]:
        yield

    def typing(self, chat_id: str):  # type: ignore[no-untyped-def]
        return self._typing_cm()

    async def deliver(self, msg: InboundMessage) -> None:
        assert self._on_inbound is not None, "bind() not called"
        await self._on_inbound(msg)


class FakeEngine:
    """Fake EngineHandle yielding a scripted StreamEvent sequence."""

    def __init__(self, session_id: str, events: list[StreamEvent]) -> None:
        self._sid = session_id
        self._events = events
        self.last_input: EngineInput | None = None

    async def run(
        self, prompt: str, attachments: tuple[ImageBlock, ...] = ()
    ) -> AsyncIterator[StreamEvent]:
        self.last_input = EngineInput(prompt=prompt, attachments=attachments)
        for ev in self._events:
            yield ev

    def abort(self, reason: str = "cancel") -> None: ...

    async def clear_in_place(self) -> int:
        return 0

    async def compact(self) -> dict[str, int]:
        return {}

    def switch_model(self, model: str) -> None: ...

    @property
    def session_id(self) -> str:
        return self._sid


def _text_only_caps() -> PlatformCapabilities:
    return PlatformCapabilities(
        streaming_edit=False, rich_blocks=False, interactive_buttons=False
    )


def _msg(
    *,
    platform: str = "fake",
    bot_id: str = "default",
    chat_id: str = "c1",
    user_id: str = "u1",
    text: str = "hello",
    thread_id: str = "",
    is_dm: bool = False,
    attachments: tuple[InboundAttachment, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        platform=platform,
        bot_id=bot_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        thread_id=thread_id,
        is_dm=is_dm,
        attachments=attachments,
    )


# ── G1: inbound mapping ───────────────────────────────────────────────────────


def test_g1_inbound_attachment_maps_to_engine_input() -> None:
    # Given a platform message with an image attachment
    att = InboundAttachment(
        kind="image", url="", mime="image/png", filename="cat.png", data="aGVsbG8="
    )
    msg = _msg(text="look at this", attachments=(att,))
    # When normalized for a vision-capable model
    ei = normalize_inbound(msg, vision=True)
    # Then EngineInput carries the prompt text + an ImageBlock
    assert ei.prompt == "look at this"
    assert len(ei.attachments) == 1
    block = ei.attachments[0]
    assert isinstance(block, ImageBlock)
    assert block.media_type == "image/png"
    assert block.source_type == "base64"
    assert block.data == "aGVsbG8="


def test_g1_image_without_vision_degrades_to_text_note() -> None:
    att = InboundAttachment(kind="image", url="", filename="cat.png", data="aGk=")
    msg = _msg(text="see", attachments=(att,))
    # Non-vision model: image must not be silently dropped (P5)
    ei = normalize_inbound(msg, vision=False)
    assert ei.attachments == ()
    assert "cat.png" in ei.prompt and "vision" in ei.prompt


# ── G2: outbound throttle/merge on a non-streaming platform ────────────────────


def test_g2_text_deltas_merged_into_one_message() -> None:
    # Given many TEXT_DELTA events + a non-streaming (no streaming_edit) platform
    r = OutboundRenderer(_text_only_caps(), chat_id="c1")
    outs: list[OutboundMessage] = []
    for piece in ["Hel", "lo, ", "wor", "ld!"]:
        outs += r.render(make_event(TextDeltaPayload(text=piece)))
    # When mapping: nothing released mid-stream (merge_all)
    assert outs == []
    # Then TURN_END flushes a single merged message
    outs += r.render(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="done")))
    text_msgs = [o for o in outs if o.text]
    assert len(text_msgs) == 1
    assert text_msgs[0].text == "Hello, world!"


def test_g2_streaming_platform_flushes_progressively() -> None:
    caps = PlatformCapabilities(streaming_edit=True, max_text_len=2000)
    r = OutboundRenderer(caps, chat_id="c1")
    r.attach_edit_target("m1")
    # char window default 200 -> a long delta triggers an immediate flush
    big = "x" * 250
    outs = r.render(make_event(TextDeltaPayload(text=big)))
    assert len(outs) == 1
    assert outs[0].edit_message_id == "m1"


# ── G3: tool-card downgrade to text summary, no info loss ──────────────────────


def test_g3_tool_end_downgraded_to_text_summary() -> None:
    # Given a TOOL_END with rich metadata + a text-only platform
    payload = ToolEndPayload(
        tool_use_id="t1",
        tool_name="read_file",
        output="file contents here",
        duration_ms=42.0,
        metadata={"path": "/srv/x.txt", "lines": 12},
    )
    r = OutboundRenderer(_text_only_caps(), chat_id="c1")
    # When mapped
    outs = r.render(make_event(payload))
    # Then a single text summary preserving key info, no rich blocks
    assert len(outs) == 1
    out = outs[0]
    assert out.blocks == ()
    assert "read_file" in out.text
    assert "42" in out.text  # duration preserved
    assert "/srv/x.txt" in out.text  # metadata preserved
    assert "lines=12" in out.text


def test_g3_tool_end_error_summary_preserves_error() -> None:
    payload = ToolEndPayload(
        tool_use_id="t1",
        tool_name="bash",
        output="",
        is_error=True,
        error_kind="nonzero_exit",
        error_message="command failed: exit 1",
    )
    r = OutboundRenderer(_text_only_caps(), chat_id="c1")
    out = r.render(make_event(payload))[0]
    assert "bash" in out.text
    assert "command failed" in out.text


def test_g3_rich_platform_keeps_card() -> None:
    caps = PlatformCapabilities(rich_blocks=True)
    payload = ToolEndPayload(tool_use_id="t1", tool_name="read_file", output="x")
    r = OutboundRenderer(caps, chat_id="c1")
    out = r.render(make_event(payload))[0]
    assert out.blocks  # not downgraded
    assert out.blocks[0]["tool_name"] == "read_file"


# ── G4: per_user routing isolation ─────────────────────────────────────────────


async def test_g4_two_users_two_distinct_sessions() -> None:
    seen: dict[str, FakeEngine] = {}

    async def factory(sid: str, sink: EventSink, perm: PermissionPort) -> EngineHandle:
        eng = FakeEngine(sid, [make_event(TextDeltaPayload(text=f"hi from {sid}"))
                               , make_event(TurnEndPayload(0, 1, "done"))])
        seen[sid] = eng
        return eng

    runner = GatewayRunner(_registry(), factory)
    adapter = FakePlatformAdapter(capabilities=_text_only_caps())
    runner.attach_adapter(adapter)

    # When two different users message the same channel
    await runner.handle_inbound(adapter, _msg(user_id="u1", text="a"))
    await runner.handle_inbound(adapter, _msg(user_id="u2", text="b"))

    # Then two distinct sessions are created, never crossed
    assert len(seen) == 2
    sids = sorted(seen)
    assert sids[0] != sids[1]
    assert all(":u1" in s or ":u2" in s for s in sids)


# ── G5: busy -> enqueue or notify ──────────────────────────────────────────────


async def test_g5_busy_session_notifies_when_queue_disabled() -> None:
    # An engine whose run blocks until released, so the session stays busy.
    release = asyncio.Event()

    class BlockingEngine(FakeEngine):
        async def run(self, prompt, attachments=()):  # type: ignore[no-untyped-def]
            self.last_input = EngineInput(prompt, attachments)
            await release.wait()
            yield make_event(TurnEndPayload(0, 1, "done"))

    async def factory(sid, sink, perm):  # type: ignore[no-untyped-def]
        return BlockingEngine(sid, [])

    runner = GatewayRunner(_registry(), factory, max_queue_per_session=0)
    adapter = FakePlatformAdapter(capabilities=_text_only_caps())
    runner.attach_adapter(adapter)

    # Given the engine is busy on the first message
    first = asyncio.ensure_future(
        runner.handle_inbound(adapter, _msg(user_id="u1", text="first"))
    )
    await asyncio.sleep(0.02)  # let it acquire the lock and block in run()
    # When the same user sends again while busy
    await runner.handle_inbound(adapter, _msg(user_id="u1", text="second"))
    # Then they are told the previous message is still being processed (G5)
    assert any("still being processed" in o.text for o in adapter.sent)

    release.set()
    await first


async def test_g5_busy_session_enqueues_when_allowed() -> None:
    order: list[str] = []
    gate = asyncio.Event()

    class SerialEngine(FakeEngine):
        async def run(self, prompt, attachments=()):  # type: ignore[no-untyped-def]
            order.append(f"start:{prompt}")
            if prompt == "first":
                await gate.wait()
            order.append(f"end:{prompt}")
            yield make_event(TurnEndPayload(0, 1, "done"))

    async def factory(sid, sink, perm):  # type: ignore[no-untyped-def]
        return SerialEngine(sid, [])

    runner = GatewayRunner(_registry(), factory, max_queue_per_session=2)
    adapter = FakePlatformAdapter(capabilities=_text_only_caps())
    runner.attach_adapter(adapter)

    f1 = asyncio.ensure_future(
        runner.handle_inbound(adapter, _msg(user_id="u1", text="first"))
    )
    await asyncio.sleep(0.02)
    f2 = asyncio.ensure_future(
        runner.handle_inbound(adapter, _msg(user_id="u1", text="second"))
    )
    await asyncio.sleep(0.02)
    # second is enqueued (waiting on the lock), not processed yet
    assert "start:second" not in order
    gate.set()
    await asyncio.gather(f1, f2)
    # Then the queued message runs after the first, serialized (no drop)
    assert order == ["start:first", "end:first", "start:second", "end:second"]


# ── G6: unattended approval timeout -> fail-safe deny ──────────────────────────


async def test_g6_ask_no_responder_times_out_to_deny() -> None:
    # Given an ask-tier call + nobody to approve + a short timeout
    class RecordingSink:
        def __init__(self) -> None:
            self.events: list[StreamEvent] = []

        async def emit(self, event: StreamEvent) -> None:
            self.events.append(event)

    sink = RecordingSink()
    approvals = ApprovalRegistry()
    port = GatewayPermissionPort(
        sink, approvals, matcher=lambda c: "ask", ask_timeout=0.05
    )
    call = ToolCall(id="call-1", name="bash", input={"command": "rm -rf /"},
                    tier=CapabilityTier.PROCESS)
    # When the approval window elapses with no responder
    decision = await port.check(call)
    # Then fail-safe default DENY with an explanation (G6)
    assert isinstance(decision, Decision)
    assert decision.action == "deny"
    assert decision.reason and "timed out" in decision.reason
    # And an APPROVAL event was emitted so a human *could* have responded
    assert any(e.type is EventType.APPROVAL for e in sink.events)


async def test_g6_responder_can_allow_before_timeout() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.events: list[StreamEvent] = []

        async def emit(self, event: StreamEvent) -> None:
            self.events.append(event)

    sink = RecordingSink()
    approvals = ApprovalRegistry()
    port = GatewayPermissionPort(
        sink, approvals, matcher=lambda c: "ask", ask_timeout=2.0,
        requesting_user_id="u1",
    )
    call = ToolCall(id="call-9", name="write_file", input={"file_path": "x"})

    async def approve_soon() -> None:
        await asyncio.sleep(0.02)
        approvals.resolve_by_user("call-9", Decision.allow(), user_id="u1")

    asyncio.ensure_future(approve_soon())
    decision = await port.check(call)
    assert decision.action == "allow"


# ── G7: new platform onboarding, runner unchanged ─────────────────────────────


async def test_g7_second_platform_handled_with_no_runner_change() -> None:
    built: list[str] = []

    async def factory(sid, sink, perm):  # type: ignore[no-untyped-def]
        built.append(sid)
        return FakeEngine(sid, [
            make_event(TextDeltaPayload(text="ok")),
            make_event(TurnEndPayload(0, 1, "done")),
        ])

    # Register a brand-new platform "slackish" in the registry only.
    registry = _registry()
    registry.register(
        "slackish",
        lambda: FakePlatformAdapter(
            name="slackish", capabilities=_text_only_caps()
        ),
    )
    runner = GatewayRunner(registry, factory)
    # add_platform uses the registry; the runner code path is identical to G4.
    adapter = runner.add_platform("slackish")
    assert adapter.name == "slackish"

    await runner.handle_inbound(adapter, _msg(platform="slackish", text="hi"))
    # Then the new platform is wired end-to-end with no runner changes.
    assert built  # an engine was built for it
    assert any(o.text == "ok" for o in adapter.sent)


# ── G8: rate-limit / reconnect -> backoff, no message lost ─────────────────────


async def test_g8_rate_limit_then_backoff_no_message_lost() -> None:
    # Given a platform whose first two sends hit 429
    delivered: list[str] = []

    async def factory(sid, sink, perm):  # type: ignore[no-untyped-def]
        return FakeEngine(sid, [
            make_event(TextDeltaPayload(text="answer")),
            make_event(TurnEndPayload(0, 1, "done")),
        ])

    # fail the first 2 send attempts (429), then succeed
    adapter = FakePlatformAdapter(capabilities=_text_only_caps(), fail_sends=2)
    runner = GatewayRunner(
        _registry(), factory,
        backoff=BackoffPolicy(base_seconds=0.0, max_retries=5),
    )
    runner.attach_adapter(adapter)

    await runner.handle_inbound(adapter, _msg(text="q"))
    # Then after backoff retries the message is delivered (not lost, G8)
    assert any(o.text == "answer" for o in adapter.sent)


async def test_g8_exhausted_retries_park_then_resend() -> None:
    # First message: every send 429s past max retries -> parked on retry queue.
    # Second message: adapter recovers -> parked + new both delivered, in order.

    class PromptEcho(FakeEngine):
        async def run(self, prompt, attachments=()):  # type: ignore[no-untyped-def]
            self.last_input = EngineInput(prompt, attachments)
            yield make_event(TextDeltaPayload(text=prompt))
            yield make_event(TurnEndPayload(0, 1, "done"))

    async def factory(sid, sink, perm):  # type: ignore[no-untyped-def]
        return PromptEcho(sid, [])

    # max_retries small so the first send exhausts and parks.
    adapter = FakePlatformAdapter(capabilities=_text_only_caps(), fail_sends=10)
    runner = GatewayRunner(
        _registry(), factory,
        backoff=BackoffPolicy(base_seconds=0.0, max_retries=2),
    )
    runner.attach_adapter(adapter)

    await runner.handle_inbound(adapter, _msg(text="q1"))
    assert adapter.sent == []  # all 429'd -> parked, nothing delivered yet
    # Adapter recovers; next inbound drains the retry queue first (G8).
    adapter._fail_sends = 0
    await runner.handle_inbound(adapter, _msg(text="q2"))
    texts = [o.text for o in adapter.sent]
    assert "q1" in texts and "q2" in texts  # nothing lost
    assert texts.index("q1") < texts.index("q2")  # in order


# ── helpers ────────────────────────────────────────────────────────────────


def _registry() -> PlatformRegistry:
    return PlatformRegistry()


# ── G-wiring: EngineBuilder.make_gateway_runner produces a usable runner ──────


async def test_gateway_runner_wired_from_engine_builder(tmp_path) -> None:
    """Given an EngineBuilder,
    When ``make_gateway_runner()`` is called,
    Then it returns a GatewayRunner whose engine_factory builds real engines,
    and a platform adapter attached to it can drive an inbound message through
    to a real EngineHandle and receive an outbound response.
    """
    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions

    builder = EngineBuilder(EngineOptions(model="echo", cwd=tmp_path))
    runner = builder.make_gateway_runner()
    assert runner is not None

    adapter = FakePlatformAdapter(capabilities=_text_only_caps())
    runner.attach_adapter(adapter)

    # Echo model yields a text delta + turn end; the gateway should deliver it.
    await runner.handle_inbound(adapter, _msg(text="hello"))
    assert any(o.text for o in adapter.sent)
