"""GatewayRunner — lifecycle + inbound->engine->outbound orchestration.

The Runner is platform-agnostic: it drives any object implementing
:class:`~autoverse.frontends.gateway.adapter.PlatformAdapter`. Onboarding a new
platform = implement the adapter + register it in the
:class:`~autoverse.frontends.gateway.adapter.PlatformRegistry`; the Runner code
is unchanged (G7/P8).

It handles:
* busy sessions — a per-session busy lock; while busy a new message is enqueued
  (bounded) or the user is told the previous one is still running (G5, ref D5).
* outbound rate-limit / disconnect — a :class:`RateLimitError` from ``send``
  triggers exponential backoff retry; messages are never lost — failed sends go
  to a per-session retry queue and are re-sent before new output (G8).
* approval routing — wires a :class:`GatewayPermissionPort` + registry so an
  inbound ``yes/no`` (or button click) resolves a pending approval.

The Runner depends only on :class:`~autoverse.ports.EngineHandle` for engine
access (P3) — no engine private attributes, no ``getattr`` probing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from autoverse.ports import EngineHandle, EventSink
from autoverse.types import StreamEvent
from autoverse.types.events import ApprovalPayload, EventType
from autoverse.types.tools import Decision

from autoverse.frontends.gateway.adapter import (
    OutboundMessage,
    OutboundRenderer,
    PlatformAdapter,
    PlatformRegistry,
    normalize_inbound,
)
from autoverse.frontends.gateway.approval import (
    ApprovalRegistry,
    GatewayPermissionPort,
)
from autoverse.frontends.gateway.router import (
    ActiveSession,
    SessionPolicy,
    SessionRouter,
)
from autoverse.frontends.gateway.types import InboundMessage


class RateLimitError(Exception):
    """Raised by an adapter ``send``/``edit`` when the platform returns 429."""

    def __init__(self, message: str = "rate limited", retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class BackoffPolicy:
    """Exponential backoff for outbound retries and connection restarts (G8)."""

    base_seconds: float = 0.05
    factor: float = 2.0
    max_seconds: float = 5.0
    max_retries: int = 6

    def delay(self, attempt: int) -> float:
        d = self.base_seconds * (self.factor ** attempt)
        return min(d, self.max_seconds)


# A vision-capability probe per session (default: assume vision so images flow).
VisionProbe = Callable[[EngineHandle], bool]


@dataclass
class _OutboundSender:
    """Sends OutboundMessages through an adapter with backoff + retry queue (G8).

    On a :class:`RateLimitError` the message is retried with exponential backoff;
    if all retries are exhausted it is parked on ``retry_queue`` and re-sent
    (in order) ahead of the next send, so no message is ever lost.
    """

    adapter: PlatformAdapter
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    retry_queue: list[OutboundMessage] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: OutboundMessage) -> None:
        await self._drain_retry_queue()
        await self._send_one(msg)

    async def _drain_retry_queue(self) -> None:
        while self.retry_queue:
            queued = self.retry_queue.pop(0)
            await self._send_one(queued, requeue_on_fail=False)

    async def _send_one(
        self, msg: OutboundMessage, *, requeue_on_fail: bool = True
    ) -> None:
        for attempt in range(self.backoff.max_retries + 1):
            try:
                mid = await self.adapter.send(msg)
                self.sent.append(mid)
                return
            except RateLimitError as exc:
                if attempt >= self.backoff.max_retries:
                    if requeue_on_fail:
                        self.retry_queue.append(msg)  # never lose it (G8)
                    return
                delay = exc.retry_after or self.backoff.delay(attempt)
                await self.sleep(delay)


class GatewayRunner:
    def __init__(
        self,
        registry: PlatformRegistry,
        engine_factory: Callable[[str, EventSink, GatewayPermissionPort], Awaitable[EngineHandle]],
        *,
        session: SessionPolicy | None = None,
        ask_timeout: float = 120.0,
        backoff: BackoffPolicy | None = None,
        vision_probe: VisionProbe | None = None,
        max_queue_per_session: int = 1,
    ) -> None:
        self._registry = registry
        self._engine_factory = engine_factory
        self._policy = session or SessionPolicy()
        self._ask_timeout = ask_timeout
        self._backoff = backoff or BackoffPolicy()
        self._vision_probe = vision_probe or (lambda _e: True)
        self._max_queue = max_queue_per_session
        self._adapters: dict[str, PlatformAdapter] = {}
        self._senders: dict[str, _OutboundSender] = {}
        self._approvals: dict[str, ApprovalRegistry] = {}  # per session_id
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        # Router builds engines lazily; the per-session sink/permission are wired
        # at acquire time, so the factory is closed over via _build_engine.
        self._router = SessionRouter(self._policy, self._build_engine)
        self._pending_session_wiring: dict[
            str, tuple[EventSink, GatewayPermissionPort]
        ] = {}

    # ── platform onboarding (G7) ───────────────────────────────────────────────
    def add_platform(self, name: str, *args: object, **kwargs: object) -> PlatformAdapter:
        """Create + register an adapter from the registry (no Runner change)."""
        adapter = self._registry.create(name, *args, **kwargs)
        key = f"{adapter.name}:{adapter.bot_id}"
        adapter.bind(self._make_on_inbound(adapter))
        self._adapters[key] = adapter
        self._senders[key] = _OutboundSender(adapter=adapter, backoff=self._backoff)
        return adapter

    def attach_adapter(self, adapter: PlatformAdapter) -> None:
        """Attach an already-constructed adapter (used by tests/fakes)."""
        key = f"{adapter.name}:{adapter.bot_id}"
        adapter.bind(self._make_on_inbound(adapter))
        self._adapters[key] = adapter
        self._senders[key] = _OutboundSender(adapter=adapter, backoff=self._backoff)

    async def start(self) -> None:
        """Connect every attached adapter on its own task; block until stop()."""
        for key, adapter in self._adapters.items():
            self._tasks.append(
                asyncio.create_task(self._run_adapter(adapter), name=f"gw-{key}")
            )
        if not self._adapters:
            return
        await self._stop.wait()
        for t in self._tasks:
            t.cancel()

    async def _run_adapter(self, adapter: PlatformAdapter) -> None:
        """Run + supervise an adapter's connect(): on unexpected exit, restart
        with exponential backoff (only this platform; G8)."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await adapter.connect()
                return  # clean return = listener stopped intentionally
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — reconnect until backoff is exhausted
                if attempt >= self._backoff.max_retries:
                    return
                await asyncio.sleep(self._backoff.delay(attempt))
                attempt += 1

    async def stop(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001 — best-effort disconnect during stop
                pass
        await self._router.shutdown()
        self._stop.set()

    # ── inbound dispatch (§5) ──────────────────────────────────────────────────
    def _make_on_inbound(
        self, adapter: PlatformAdapter
    ) -> Callable[[InboundMessage], Awaitable[None]]:
        async def on_inbound(msg: InboundMessage) -> None:
            await self.handle_inbound(adapter, msg)

        return on_inbound

    async def handle_inbound(
        self, adapter: PlatformAdapter, msg: InboundMessage
    ) -> None:
        key = f"{adapter.name}:{adapter.bot_id}"
        sender = self._senders.get(key)
        if sender is None:
            sender = _OutboundSender(adapter=adapter, backoff=self._backoff)
            self._senders[key] = sender

        session_id = self._router.route(
            platform=msg.platform,
            bot_id=msg.bot_id,
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            thread_id=msg.thread_id,
            is_dm=msg.is_dm,
        )
        approvals = self._approvals.setdefault(session_id, ApprovalRegistry())

        # An inbound yes/no may be a reply resolving a pending approval (G6).
        if self._try_resolve_approval(approvals, msg):
            return

        sink = OutboundRenderer(
            adapter.capabilities, chat_id=msg.chat_id, reply_to=""
        )
        perm = GatewayPermissionPort(
            _RendererSink(sink, sender),
            approvals,
            ask_timeout=self._ask_timeout,
            requesting_user_id=msg.user_id,
        )
        self._pending_session_wiring[session_id] = (_RendererSink(sink, sender), perm)
        session = await self._router.acquire(session_id)

        # busy handling (G5): if the session is mid-turn, enqueue or notify.
        if session.lock.locked():
            if self._max_queue <= 0:
                await sender.send(
                    OutboundMessage(
                        chat_id=msg.chat_id,
                        text="\u23f3 your previous message is still being processed.",
                    )
                )
                return
            # bounded enqueue: wait for the lock (queueing), do not drop.
        async with session.lock:
            await self._drive_turn(session, adapter, sender, sink, msg)

    async def _drive_turn(
        self,
        session: ActiveSession,
        adapter: PlatformAdapter,
        sender: _OutboundSender,
        sink: OutboundRenderer,
        msg: InboundMessage,
    ) -> None:
        vision = self._vision_probe(session.engine)
        engine_input = normalize_inbound(msg, vision=vision)
        async for ev in session.engine.run(
            engine_input.prompt, engine_input.attachments
        ):
            for out in sink.render(ev):
                if out.edit_message_id:
                    try:
                        await adapter.edit(out.chat_id, out.edit_message_id, out.text)
                    except RateLimitError:
                        await sender.send(out)  # fall back to send + retry queue
                else:
                    await sender.send(out)

    def _try_resolve_approval(
        self, approvals: ApprovalRegistry, msg: InboundMessage
    ) -> bool:
        text = msg.text.strip().lower()
        pending = approvals.pending_ids()
        if not pending:
            return False
        if text in {"yes", "y", "allow", "approve"}:
            decision = Decision.allow(rule="user-approved")
        elif text in {"no", "n", "deny", "reject"}:
            decision = Decision.deny(reason="denied by user")
        else:
            return False
        # Resolve the oldest pending approval owned by this user.
        for call_id in pending:
            if approvals.resolve_by_user(call_id, decision, user_id=msg.user_id):
                return True
        return False

    async def _build_engine(self, session_id: str) -> EngineHandle:
        wiring = self._pending_session_wiring.pop(session_id, None)
        if wiring is None:
            # Should not happen in the normal path; build a minimal sink/perm.
            registry = self._approvals.setdefault(session_id, ApprovalRegistry())
            sink: EventSink = _NullSink()
            perm = GatewayPermissionPort(sink, registry, ask_timeout=self._ask_timeout)
        else:
            sink, perm = wiring
        return await self._engine_factory(session_id, sink, perm)

    # external resolve hook for button-based platforms
    def resolve_approval(
        self, session_id: str, call_id: str, decision: Decision, *, user_id: str = ""
    ) -> bool:
        approvals = self._approvals.get(session_id)
        if approvals is None:
            return False
        if user_id:
            return approvals.resolve_by_user(call_id, decision, user_id=user_id)
        return approvals.resolve(call_id, decision)


class _RendererSink:
    """EventSink bridging the engine's APPROVAL events to the renderer + sender.

    Approval events are rendered to an outbound message and sent immediately so
    the user sees the request; all other events flow through the renderer in the
    drive loop, so this sink only handles APPROVAL (avoids double-rendering)."""

    def __init__(self, renderer: OutboundRenderer, sender: _OutboundSender) -> None:
        self._renderer = renderer
        self._sender = sender

    async def emit(self, event: StreamEvent) -> None:
        if event.type is EventType.APPROVAL:
            payload = event.payload
            assert isinstance(payload, ApprovalPayload)
            out = self._renderer.render_approval(payload)
            await self._sender.send(out)


class _NullSink:
    async def emit(self, event: StreamEvent) -> None:  # pragma: no cover
        return None


__all__ = [
    "GatewayRunner",
    "BackoffPolicy",
    "RateLimitError",
]
