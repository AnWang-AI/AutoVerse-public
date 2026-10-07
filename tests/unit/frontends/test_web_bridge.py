from autoverse.frontends.web.bridge import SessionBridge
from autoverse.frontends.web.markup_html import markup_to_html
from autoverse.frontends.web.web_log import WebLog


def test_pinned_turn_footer_stays_after_later_blocks() -> None:
    bridge = SessionBridge()
    bridge.append_block({"kind": "user", "text": "hello"}, "user")
    bridge.append_block({"kind": "turn_meta", "text": "model", "live": True}, "footer")
    bridge.pin_tail("footer")

    bridge.append_block({"kind": "markdown", "md": "answer"}, "answer")
    bridge.apply_block(
        {"op": "upsert", "id": "agent:1", "block": {"kind": "subagent", "job": "1"}}
    )

    assert [item["id"] for item in bridge.blocks] == [
        "user", "answer", "agent:1", "footer",
    ]

    bridge.apply_block(
        {
            "op": "update",
            "id": "footer",
            "block": {"kind": "turn_meta", "text": "model · 1.0s", "live": False},
        }
    )
    bridge.release_tail("footer")
    bridge.append_block({"kind": "user", "text": "next"}, "next")

    assert [item["id"] for item in bridge.blocks] == [
        "user", "answer", "agent:1", "footer", "next",
    ]


def test_snapshot_includes_server_clock_for_live_footer_resume() -> None:
    bridge = SessionBridge()

    assert bridge.snapshot()["server_now_ms"] > 0


def test_snapshot_is_bounded_and_older_blocks_are_cursor_paged() -> None:
    bridge = SessionBridge("session-1")
    for index in range(205):
        bridge.append_block({"kind": "line", "html": str(index)}, f"b{index}")

    snapshot = bridge.snapshot()

    assert len(snapshot["blocks"]) == 200
    assert snapshot["has_more"] is True
    assert snapshot["before_cursor"] == "5"

    older = bridge.history_page(snapshot["before_cursor"])
    assert [item["id"] for item in older["blocks"]] == [
        "b0", "b1", "b2", "b3", "b4",
    ]
    assert older["has_more"] is False
    assert older["before_cursor"] is None


def test_snapshot_cursor_stays_on_turn_boundary_and_keeps_two_latest_turns() -> None:
    bridge = SessionBridge("session-turns")
    for turn in range(4):
        bridge.append_block({"kind": "user", "text": f"q{turn}"}, f"u{turn}")
        for block in range(90):
            bridge.append_block(
                {"kind": "line", "html": f"{turn}:{block}"},
                f"b{turn}:{block}",
            )

    snapshot = bridge.snapshot()

    ids = [item["id"] for item in snapshot["blocks"]]
    assert ids[0] == "u2"
    assert "u3" in ids
    assert snapshot["before_cursor"] == str(2 * 91)
    older = bridge.history_page(snapshot["before_cursor"])
    assert older["blocks"][0]["id"] in {"u0", "u1"}
    assert older["blocks"][0]["block"]["kind"] == "user"


def test_single_oversized_block_is_summarized_until_explicit_expansion() -> None:
    bridge = SessionBridge("session-large")
    bridge.append_block({"kind": "user", "text": "inspect"}, "u1")
    bridge.append_block(
        {"kind": "toolblock", "html": "x" * (160 * 1024)}, "large-tool"
    )

    snapshot = bridge.snapshot()
    item = next(item for item in snapshot["blocks"] if item["id"] == "large-tool")

    assert item["block"]["kind"] == "oversize"
    assert item["block"]["byte_size"] > 128 * 1024
    assert item["block"]["detail_url"].endswith("/blocks/large-tool")
    assert bridge.block_detail("large-tool")["block"]["html"].startswith("xxx")


def test_history_dense_snapshot_stays_bounded_with_semantic_turn_handles() -> None:
    bridge = SessionBridge("dense")
    for turn in range(20):
        bridge.append_block({"kind": "user", "text": f"turn {turn}"}, f"u{turn}")
        for block in range(97):
            bridge.append_block(
                {"kind": "line", "html": f"{turn}:{block}:" + ("x" * 1400)},
                f"b{turn}:{block}",
            )
        bridge.append_block(
            {"kind": "markdown", "md": f"final answer {turn}"}, f"a{turn}"
        )
        bridge.append_block(
            {"kind": "turn_meta", "text": "model · 1.0s", "live": False},
            f"m{turn}",
        )

    snapshot = bridge.snapshot()

    assert len(snapshot["blocks"]) <= 200
    assert snapshot["has_more"] is True
    kinds = [item["block"]["kind"] for item in snapshot["blocks"]]
    assert kinds == [
        "user", "deferred_execution", "markdown", "turn_meta",
        "user", "deferred_execution", "markdown", "turn_meta",
    ]
    assert snapshot["blocks"][0]["id"] == "u18"
    assert snapshot["blocks"][2]["id"] == "a18"
    detail_url = snapshot["blocks"][1]["block"]["detail_url"]
    assert detail_url.startswith("/api/sessions/dense/ranges/")
    start, end = (int(part) for part in detail_url.rsplit("/", 2)[-2:])
    expanded = bridge.range_detail(start, end)
    assert expanded["blocks"][0]["id"] == "b18:0"
    assert expanded["blocks"][-2]["id"] == "a18"
    assert expanded["blocks"][-1]["id"] == "m18"
    assert all(
        not str(item["id"]).startswith("b17:")
        for item in snapshot["blocks"]
    )


def test_snapshot_externalizes_base64_images_as_lazy_resources() -> None:
    bridge = SessionBridge("session with spaces")
    bridge.append_block(
        {
            "kind": "user",
            "text": "look",
            "attachments": [
                {
                    "media_type": "image/png",
                    "data": "QUJD",
                    "source_type": "base64",
                }
            ],
        },
        "user:1",
    )

    attachment = bridge.snapshot()["blocks"][0]["block"]["attachments"][0]

    assert "data" not in attachment
    assert attachment["resource_url"] == (
        "/api/sessions/session%20with%20spaces/resources/user%3A1/0"
    )
    assert bridge.resource("user:1", 0) == (b"ABC", "image/png")


def test_live_block_broadcast_externalizes_image_payload() -> None:
    bridge = SessionBridge("s1")
    subscriber = bridge.subscribe()

    bridge.append_block(
        {
            "kind": "tool_images",
            "images": [{"media_type": "image/jpeg", "data": "QUJD"}],
        },
        "tool:1",
    )

    message = subscriber.get_nowait()
    image = message["block"]["images"][0]
    assert "data" not in image
    assert image["resource_url"].endswith("/resources/tool%3A1/0")


def test_edit_collapsible_uses_dedicated_diff_card_class() -> None:
    from rich.console import Group
    from rich.text import Text

    emitted: list[dict] = []
    log = WebLog(emitted.append)
    log.write_collapsible(
        "← Edit src/app.py",
        Group(Text("@@ -1 +1 @@"), Text("1 - old"), Text("1 + new"), Text("2")),
    )

    block = emitted[0]["block"]
    assert block["card_cls"] == "diff-card"
    assert '<div class="diff">' in block["body_html"]
    assert "background-color:" not in block["body_html"]
    assert '<span class="ln">2</span><span class="sg"> </span>' in block["body_html"]


def test_markup_title_preserves_shell_regex_brackets() -> None:
    html = markup_to_html(
        '[$av-text-muted]$ git diff | grep "^[-+]" · 21 lines[/$av-text-muted]'
    )

    assert "grep &quot;^" in html
    assert "[-+]" in html
    assert "21 lines" in html


def test_single_line_tool_write_carries_tool_semantic_class() -> None:
    """Inline tool lines (web_search / read / grep / …) are written with the
    ``tool-line`` marker class so the emitted block carries ``cls: inline tool``
    and the frontend's turn-history fold can count them as tool calls."""
    from rich.text import Text

    emitted: list[dict] = []
    log = WebLog(emitted.append)
    log.write(Text('◈ Web Search "ds 更新" (8 results)'), classes="tool-line")

    block = emitted[0]["block"]
    assert block["kind"] == "line"
    assert block["cls"] == "inline tool"


def test_multi_line_tool_write_stays_toolblock() -> None:
    """Block tools keep their existing ``toolblock`` rendering — the new
    ``tool-line`` marker must not affect the multi-line path."""
    from rich.text import Text

    emitted: list[dict] = []
    log = WebLog(emitted.append)
    log.write(Text("$ pytest\n1 passed"), classes="tool-block")

    block = emitted[0]["block"]
    assert block["kind"] == "toolblock"
    assert "$ pytest\n1 passed" in block["html"]


def test_subagent_block_emits_semantic_role_and_plain_text() -> None:
    """WebUI themes role text themselves instead of replaying dark-theme hex."""
    from rich.text import Text

    emitted: list[dict] = []
    log = WebLog(emitted.append)
    card = Text()
    card.append("│ Scout Task — locate TurnLoop", style="#8a9ec0")
    card.append("\n")
    card.append("└ 3 toolcalls · 7s", style="#989aa8")

    log.write_or_update_agent("job-1", card, done=True)

    block = emitted[0]["block"]
    assert block["role"] == "scout"
    assert block["head_text"] == "│ Scout Task — locate TurnLoop"
    assert block["foot_text"] == "└ 3 toolcalls · 7s"
    assert "head_html" not in block
    assert block["failed"] is False


def test_subagent_block_strips_raw_ansi_at_web_boundary() -> None:
    emitted: list[dict] = []
    log = WebLog(emitted.append)
    raw = (
        "\x1b[38;2;138;158;192m│ Scout Task — locate TurnLoop\x1b[0m\n"
        "\x1b[38;2;152;154;168m└ 3 toolcalls · 7s\x1b[0m\n"
        "unexpected third line"
    )

    log.write_or_update_agent("job-ansi", raw, done=True)

    block = emitted[0]["block"]
    assert block["head_text"] == "│ Scout Task — locate TurnLoop"
    assert block["foot_text"] == "└ 3 toolcalls · 7s"
    assert "\x1b" not in str(block)
    assert "unexpected" not in str(block)


async def test_live_subagent_event_emits_plain_semantic_web_block() -> None:
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.theme import DEFAULT_THEME
    from autoverse.frontends.repl.tui_sink import TuiEventSink
    from autoverse.types import make_event
    from autoverse.types.events import SubagentActivityPayload

    bridge = SessionBridge()
    ctx = RenderContext(theme=DEFAULT_THEME, ansi=True, width=80)
    sink = TuiEventSink(WebLog(bridge.emit, DEFAULT_THEME), ctx)

    await sink.emit(
        make_event(
            SubagentActivityPayload(
                job_id="job-live",
                subagent_type="scout",
                activity="done",
                status="done",
                description="locate TurnLoop",
                tool_count=3,
                elapsed_ms=7000.0,
            )
        )
    )

    block = next(
        item["block"] for item in bridge.blocks if item["id"] == "agent:job-live"
    )
    assert block["role"] == "scout"
    assert block["head_text"] == "│ Scout Task — locate TurnLoop"
    assert block["foot_text"] == "└ 3 toolcalls · 7s"
    assert "\x1b" not in str(block)
