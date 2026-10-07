"""Unit: render registry covers all EventType; tool registry default fallback."""

from __future__ import annotations

from autoverse.frontends.repl.renderer import (
    DEFAULT_RENDERER,
    RENDERERS,
    RenderContext,
    render_event,
)
from autoverse.frontends.repl.tool_renderer import (
    TOOL_RENDERERS,
    get_tool_renderer,
    register_tool_renderer,
)
from autoverse.types import EventType, make_event
from autoverse.types.events import TextDeltaPayload, ToolEndPayload


def test_every_event_type_has_renderer() -> None:
    for et in EventType:
        assert et in RENDERERS


def test_default_renderer_is_callable() -> None:
    out = DEFAULT_RENDERER(make_event(TextDeltaPayload(text="x")), RenderContext())
    assert isinstance(out, str)


def test_text_delta_renders_text() -> None:
    out = render_event(make_event(TextDeltaPayload(text="hello world")), RenderContext())
    assert "hello world" in out


def test_tool_default_used_when_unregistered() -> None:
    r = get_tool_renderer("Nope")
    out = r(ToolEndPayload(tool_use_id="t", tool_name="Nope", output="o"), RenderContext())
    assert "Nope" in out


def test_register_then_get() -> None:
    backup = dict(TOOL_RENDERERS)
    try:
        register_tool_renderer("Zed", lambda p, c: "ZED")
        assert get_tool_renderer("Zed")(
            ToolEndPayload(tool_use_id="t", tool_name="Zed", output="o"), RenderContext()
        ) == "ZED"
    finally:
        TOOL_RENDERERS.clear()
        TOOL_RENDERERS.update(backup)


def test_compaction_label_maps_kinds_to_human_phrases() -> None:
    from autoverse.frontends.repl.renderer import compaction_label

    assert compaction_label("micro") == "Folded tool output"
    assert compaction_label("overflow") == "Context summarised"
    assert compaction_label("meta") == "Context summarised"
    assert compaction_label("truncate") == "Context truncated"
    assert compaction_label("overflow_truncate") == "Context truncated"
    # unknown kind degrades to a safe phrase, never raises / leaks the kind.
    assert compaction_label("weird") == "Context compacted"


def test_compacting_renders_divider_string_without_kind_or_tokens() -> None:
    from autoverse.types.events import CompactingPayload

    out = render_event(
        make_event(CompactingPayload(kind="overflow", tokens_before=999, tokens_after=10)),
        RenderContext(),
    )
    assert "Context summarised" in out
    assert "──" in out                       # boundary divider glyphs
    assert "overflow" not in out             # internal kind not surfaced
    assert "999" not in out and "→" not in out  # no token math
