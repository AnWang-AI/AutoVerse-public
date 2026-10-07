"""Unit tests for BackoffPolicy + outbound retry queue (docs/reference/extensions.md)."""

from __future__ import annotations

from autoverse.frontends.gateway.runner import BackoffPolicy, _OutboundSender, RateLimitError
from autoverse.frontends.gateway.types import OutboundMessage


def test_backoff_grows_and_caps() -> None:
    b = BackoffPolicy(base_seconds=0.1, factor=2.0, max_seconds=1.0)
    assert b.delay(0) == 0.1
    assert b.delay(1) == 0.2
    assert b.delay(2) == 0.4
    assert b.delay(10) == 1.0  # capped


async def test_sender_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    class FlakyAdapter:
        name = "fake"
        bot_id = "default"

        async def send(self, msg: OutboundMessage) -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError(retry_after=0.0)
            return "mid-1"

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    sender = _OutboundSender(
        adapter=FlakyAdapter(),
        backoff=BackoffPolicy(base_seconds=0.01, max_retries=5),
        sleep=fake_sleep,
    )
    await sender.send(OutboundMessage(chat_id="c", text="hi"))
    assert attempts["n"] == 3
    assert sender.sent == ["mid-1"]
    assert len(slept) == 2  # two retries before success


async def test_sender_parks_on_exhausted_then_resends_no_loss() -> None:
    # Always 429 first -> message parked; later adapter recovers -> resent (G8).
    state = {"fail": True}

    class Adapter:
        name = "fake"
        bot_id = "default"

        async def send(self, msg: OutboundMessage) -> str:
            if state["fail"]:
                raise RateLimitError(retry_after=0.0)
            return f"mid:{msg.text}"

    async def fake_sleep(s: float) -> None:
        return None

    sender = _OutboundSender(
        adapter=Adapter(),
        backoff=BackoffPolicy(base_seconds=0.0, max_retries=1),
        sleep=fake_sleep,
    )
    await sender.send(OutboundMessage(chat_id="c", text="first"))
    assert len(sender.retry_queue) == 1  # parked, not lost
    assert sender.sent == []

    state["fail"] = False
    await sender.send(OutboundMessage(chat_id="c", text="second"))
    # retry queue drained before new msg -> both delivered, in order
    assert sender.sent == ["mid:first", "mid:second"]
    assert sender.retry_queue == []
