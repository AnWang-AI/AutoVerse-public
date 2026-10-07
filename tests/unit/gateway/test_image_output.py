"""Gateway: generate_image output is flushed as an outbound image (§11.7).

The renderer queues the tool-produced image path on TOOL_END and emits it on the
final flush — as an image attachment where the platform supports it, otherwise a
text note so the information is never silently dropped (P5).
"""

from __future__ import annotations

from autoverse.frontends.gateway.adapter import OutboundRenderer
from autoverse.frontends.gateway.types import PlatformCapabilities
from autoverse.types import EventType, StreamEvent, ToolEndPayload, TurnEndPayload, make_event


def _events(path: str = "/tmp/cat.png") -> list[StreamEvent]:
    end = make_event(
        ToolEndPayload(
            tool_use_id="c1",
            tool_name="generate_image",
            output=f"Generated image: {path}",
            metadata={"path": path},
        )
    )
    assert end.type is EventType.TOOL_END
    return [end, make_event(_turn_end())]


def _turn_end() -> TurnEndPayload:
    return TurnEndPayload(turn_index=0, iterations=1, stop_cause="completed")


def test_image_capable_platform_emits_image_attachment() -> None:
    r = OutboundRenderer(PlatformCapabilities(images=True, rich_blocks=True), chat_id="ch")
    out = [m for ev in _events() for m in r.render(ev)]
    img_msgs = [m for m in out if m.image_paths]
    assert img_msgs and img_msgs[0].image_paths == ("/tmp/cat.png",)


def test_text_only_platform_degrades_to_path_note() -> None:
    r = OutboundRenderer(PlatformCapabilities(images=False), chat_id="ch")
    out = [m for ev in _events() for m in r.render(ev)]
    assert any("/tmp/cat.png" in m.text for m in out)
    assert not any(m.image_paths for m in out)


def test_errored_generate_image_not_emitted() -> None:
    r = OutboundRenderer(PlatformCapabilities(images=True), chat_id="ch")
    end = make_event(
        ToolEndPayload(
            tool_use_id="c1",
            tool_name="generate_image",
            output="boom",
            is_error=True,
            metadata={"path": "/tmp/cat.png"},
        )
    )
    out = [m for ev in [end, make_event(_turn_end())] for m in r.render(ev)]
    assert not any(m.image_paths for m in out)
