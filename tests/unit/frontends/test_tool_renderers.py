"""Unit: built-in per-tool renderers (diff / todo / bash fold / agent / generic).

These guard the aesthetic contract of §3.9.3.1: dedicated renderers must show the
diff, the full todo list, fold long bash output, render an agent completion card,
and encode status by colour rather than a ``[ok]``/``[err]`` text tag. All run
without a TTY (plain strings; ansi=False).
"""

from __future__ import annotations

from autoverse.frontends.repl.collapse import collapse_tool_output
from autoverse.frontends.repl.renderer import RenderContext
from autoverse.frontends.repl.tool_renderer import get_tool_renderer
from autoverse.frontends.repl import tool_renderers_builtin  # noqa: F401  (registers)
from autoverse.types.events import ToolEndPayload


def _end(name: str, **kw: object) -> ToolEndPayload:
    base: dict[str, object] = dict(tool_use_id="t", tool_name=name, output="")
    base.update(kw)
    return ToolEndPayload(**base)  # type: ignore[arg-type]


def test_collapse_by_lines() -> None:
    out = "\n".join(str(i) for i in range(20))
    preview, overflow = collapse_tool_output(out, max_lines=5, max_chars=10_000)
    assert overflow is True
    assert preview.count("\n") == 4  # 5 lines


def test_collapse_by_chars() -> None:
    preview, overflow = collapse_tool_output("x" * 100, max_lines=50, max_chars=20)
    assert overflow is True
    assert len(preview) <= 20


def test_collapse_no_overflow() -> None:
    preview, overflow = collapse_tool_output("short", max_lines=5, max_chars=100)
    assert overflow is False
    assert preview == "short"


def test_diff_renderer_shows_diff_mimo_header_no_footer() -> None:
    """MiMo parity: ``← Edit {path}`` header + diff rows; NO ``+N -N`` footer."""
    diff = "@@ -1,2 +1,3 @@\n-old\n+new1\n+new2\n"
    out = get_tool_renderer("edit_file")(
        _end("edit_file", output="Edited", metadata={"file_path": "/a.py", "diff": diff,
                                                      "added": 2, "removed": 1}),
        RenderContext(width=90),
    )
    assert out.startswith("← Edit /a.py")
    assert "@@ -1,2 +1,3 @@" in out
    # A space now separates the +/- sign from the code (readability).
    assert "+ new1" in out and "- old" in out
    # MiMo's <diff> carries no textual +N/-N summary footer.
    assert "+2 -1" not in out
    # No blank diff rows from the trailing newline.
    assert "\n\n" not in out


def test_write_renderer_uses_wrote_header() -> None:
    """A new-file Write renders MiMo's ``# Wrote {path}`` header (Edit uses ``←``)."""
    diff = "@@ -0,0 +1,1 @@\n+hello\n"
    out = get_tool_renderer("write_file")(
        _end("write_file", output="Wrote", metadata={"file_path": "/n.py", "diff": diff,
                                                      "created": True, "added": 1, "removed": 0}),
        RenderContext(width=90),
    )
    assert out.startswith("# Wrote /n.py")


def test_inline_read_uses_path_label_no_duration() -> None:
    """MiMo InlineTool: ``→ Read {path}`` from metadata; no ``· 12ms`` suffix."""
    out = get_tool_renderer("read_file")(
        _end("read_file", output="...code...", duration_ms=12.0,
             metadata={"file_path": "src/app.py", "lines": "1-120"}),
        RenderContext(width=90),
    )
    assert out == "→ Read src/app.py · lines 1-120"
    assert "ms" not in out  # inline tools show no duration (MiMo parity)


def test_inline_glob_grep_match_counts() -> None:
    g = get_tool_renderer("glob")(
        _end("glob", output="a\nb", metadata={"pattern": "src/**/*.py", "count": 47}),
        RenderContext(width=90),
    )
    assert g == '✱ Glob "src/**/*.py" (47 matches)'
    r = get_tool_renderer("grep")(
        _end("grep", output="x", metadata={"pattern": "build_engine", "matches": 1}),
        RenderContext(width=90),
    )
    assert r == '✱ Grep "build_engine" (1 match)'


def test_todo_renderer_shows_full_list_with_icons() -> None:
    # todo_write attaches the list as structured metadata (list of dicts).
    todos = [
        {"status": "completed", "content": "done item"},
        {"status": "in_progress", "content": "active item"},
        {"status": "pending", "content": "pending item"},
    ]
    out = get_tool_renderer("todo_write")(
        _end("todo_write", output="Updated 3 todos.", metadata={"todos": todos}),
        RenderContext(width=90),
    )
    assert "Todos · 1/3" in out
    assert "✓ done item" in out
    assert "• active item" in out  # MiMo/opencode in-progress marker is `•`
    assert "○ pending item" in out


def test_bash_failure_folds_long_output() -> None:
    long_out = "\n".join(f"line {i}" for i in range(40))
    out = get_tool_renderer("bash")(
        _end("bash", output=long_out, is_error=True, error_kind="exit_nonzero",
             error_message="exit 1", duration_ms=2400.0,
             metadata={"command": "npm test"}),
        RenderContext(width=90),
    )
    # Failure renders as a block; the left bar is drawn by the ``tool-block`` CSS
    # class (sink/app), not a ``┃`` glyph in the text.
    assert "┃" not in out          # no baked-in gutter glyph
    assert out.startswith("$ npm test")  # MiMo: `$ {command}` head
    assert "truncated" in out      # fold note (10-line cap)
    # MiMo: a hard error renders a collapsed ``Error (N lines)`` line, not red-tint.
    assert "Error (1 line)" in out


def test_bash_success_with_output_is_block() -> None:
    out = get_tool_renderer("bash")(
        _end("bash", output="ok\nmore", duration_ms=30.0, metadata={"command": "ls"}),
        RenderContext(width=90),
    )
    assert "┃" not in out
    assert out.startswith("$ ls")  # MiMo Bash always shows `$ command`
    assert "ok" in out


def test_bash_success_no_output_is_inline() -> None:
    out = get_tool_renderer("bash")(
        _end("bash", output="", duration_ms=10.0, metadata={"command": "git status"}),
        RenderContext(width=90),
    )
    assert out == "$ git status"
    assert "\n" not in out


def test_agent_completion_card() -> None:
    """MiMo Task form: ``│ {Titlecase} Task — {desc}`` + ``└ {N} toolcalls · {dur}``."""
    out = get_tool_renderer("agent")(
        _end("agent", output="some verbose summary line",
             duration_ms=7300.0,
             metadata={"subagent_type": "builder", "description": "implement X",
                       "tool_count": 8}),
        RenderContext(width=90),
    )
    assert "│ Builder Task — implement X" in out
    # Footer is the structured metric pair, NOT the sub-agent's text output.
    assert "└ 8 toolcalls · 7s" in out
    assert "verbose summary" not in out


def test_generic_renderer_no_status_tag() -> None:
    """Generic card must not print the old [ok]/[err] text tag (MiMo GenericTool)."""
    out = get_tool_renderer("KubectlApply")(
        _end("KubectlApply", output="configured"),
        RenderContext(width=90),
    )
    assert "[ok]" not in out and "[err]" not in out
    assert out == "● KubectlApply"


def test_generic_benign_failure_is_struck_through_not_error_line() -> None:
    """Unknown/MCP tools share the same benign classification as builtins."""
    out = get_tool_renderer("McpSearch")(
        _end(
            "McpSearch",
            output="no matches",
            is_error=True,
            error_kind="no_match",
            error_message="no matches",
        ),
        RenderContext(ansi=True, width=90),
    )
    assert out.startswith("\x1b[9")
    assert "● McpSearch" in out
    assert "Error (" not in out


def test_benign_failure_is_struck_through_not_red() -> None:
    """Denied / recoverable failures render struck-through (9m), not a red block."""
    out = get_tool_renderer("edit_file")(
        _end("edit_file", output="", is_error=True, error_kind="no_match",
             error_message="Could not find the search string",
             metadata={"file_path": "a.py"}),
        RenderContext(ansi=True, width=90),
    )
    assert out.startswith("\x1b[9")  # SGR 9 = strikethrough leads the codes
    assert "Error (" not in out  # benign → no red Error line


def test_subagent_completion_vs_activity() -> None:
    """SUBAGENT_ACTIVITY: 'done' → completion footer; live → activity line (MiMo Task)."""
    from autoverse.frontends.repl.renderer import render_event
    from autoverse.types import make_event
    from autoverse.types.events import SubagentActivityPayload

    ctx = RenderContext(width=90)
    # Completion footer uses structured metrics (`└ {N} toolcalls · {elapsed}`),
    # not the free-text detail (renderer._render_subagent "done" branch).
    done = render_event(make_event(SubagentActivityPayload(
        job_id="t1", subagent_type="builder", activity="done", status="done",
        tool_count=12, elapsed_ms=8500.0)), ctx)
    assert "│ Builder Task" in done and "└ 12 toolcalls" in done

    live = render_event(make_event(SubagentActivityPayload(
        job_id="t1", subagent_type="scout", activity="grep: auth", detail="1.2s")), ctx)
    assert "│ Scout Task" in live and "↳ grep: auth" in live


def test_diff_has_tinted_background() -> None:
    """+/- diff rows must carry a 24-bit background (MiMo-parity tint)."""
    diff = "@@ -1,2 +1,3 @@\n-old\n+new1\n"
    out = get_tool_renderer("edit_file")(
        _end("edit_file", output="Edited",
             metadata={"file_path": "a.py", "diff": diff, "added": 1, "removed": 1}),
        RenderContext(ansi=True, width=90),
    )
    assert "\x1b[" in out and "48;2;" in out  # background SGR present on +/- rows
    # Header is muted (MiMo InlineTool/BlockTool title fg), not amber-bold.
    # Derive the expected SGR from the theme's text_muted role (its hex may
    # shift with palette tuning; the contract is "muted fg", not a fixed hex).
    from autoverse.frontends.repl.theme import DEFAULT_THEME as _t
    _hex = _t.role("text_muted")
    _r, _g, _b = int(_hex[1:3], 16), int(_hex[3:5], 16), int(_hex[5:7], 16)
    assert f"38;2;{_r};{_g};{_b}m← Edit a.py" in out


def test_theme_style_bg_and_bold() -> None:
    from autoverse.frontends.repl.theme import DEFAULT_THEME as t
    s = t.style("x", "diff_add", ansi=True, bg="diff_add_bg", bold=True)
    assert s.startswith("\x1b[1;38;2;") and "48;2;" in s
    assert t.style("x", "accent") == "x"  # plain when ansi=False


def test_theme_amber_accent_and_new_roles() -> None:
    from autoverse.frontends.repl.theme import DEFAULT_THEME as t
    assert t.role("accent") == "#e8b339"  # warm amber, not AI-blue
    for role in ("success", "warning", "error", "text_dim", "diff_add_bg", "border_subtle"):
        assert t.role(role).startswith("#")


# ── AskUserQuestion: foldable Q&A card (default-hidden, expand to review) ─────

def test_ask_renderer_card_shows_question_and_answer_labels() -> None:
    p = _end(
        "AskUserQuestion",
        output="User answered…",
        metadata={
            "questions": [
                {"id": "q1", "prompt": "Pick a colour",
                 "labels": {"r": "Red", "b": "Blue"}},
            ],
            "answer": {"answers": [{"id": "q1", "selected": ["b"]}]},
        },
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(width=90))
    lines = out.split("\n")
    assert lines[0].startswith("? Asked the user")     # curated title head
    assert "Q: Pick a colour" in out
    assert "→ Blue" in out                              # option *label*, not id
    assert "\n" in out                                  # multi-line → folds


def test_ask_renderer_multi_select_joins_labels() -> None:
    p = _end(
        "AskUserQuestion",
        output="User answered…",
        metadata={
            "questions": [
                {"id": "q1", "prompt": "Pick many",
                 "labels": {"a": "Alpha", "b": "Beta", "c": "Gamma"}},
            ],
            "answer": {"answers": [{"id": "q1", "selected": ["a", "c"]}]},
        },
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(width=90))
    assert "→ Alpha, Gamma" in out


def test_ask_renderer_no_answer_shows_placeholder() -> None:
    """A *non-cancelled* question with empty selection shows (no answer)."""
    p = _end(
        "AskUserQuestion",
        output="User answered…",
        metadata={
            "questions": [
                {"id": "q1", "prompt": "Anything?", "labels": {"x": "X"}},
            ],
            "answer": {"answers": [{"id": "q1", "selected": []}]},
        },
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(width=90))
    assert "(no answer)" in out
    assert "(dismissed)" not in out


def test_ask_renderer_cancelled_shows_dismissed() -> None:
    """When the user dismissed every question (Esc), the card says 'dismissed',
    not 'answered' — and each answer row reads (dismissed), not (no answer)."""
    p = _end(
        "AskUserQuestion",
        output="User dismissed the questions.",
        metadata={
            "questions": [
                {"id": "q1", "prompt": "Pick a colour",
                 "labels": {"r": "Red", "b": "Blue"}},
            ],
            "answer": {"answers": [{"id": "q1", "selected": []}], "cancelled": True},
            "cancelled": True,
        },
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(width=90))
    assert "Question dismissed" in out
    assert "(dismissed)" in out
    assert "answered" not in out
    assert "(no answer)" not in out


def test_ask_renderer_cancelled_no_questions_is_inline() -> None:
    """Dismissed with no structured questions → terse one-liner."""
    p = _end(
        "AskUserQuestion",
        output="User dismissed the questions.",
        metadata={"cancelled": True},
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(width=90))
    assert "Question dismissed" in out


def test_ask_renderer_unavailable_shows_questions_and_error() -> None:
    """A benign 'no interactive UI' error now shows the questions that were asked
    + the error message (struck-through, not a red Error card). The user can see
    *what* was asked and *why* it failed instead of a bare label."""
    p = _end(
        "AskUserQuestion",
        output="No interactive question UI is configured. State the decision "
               "needed and pause for the user's reply.",
        is_error=True,
        error_kind="ask_user_unavailable",
        error_message="No interactive question UI is configured. State the decision "
                       "needed and pause for the user's reply.",
        metadata={
            "questions": [
                {"id": "q1", "prompt": "Pick a colour",
                 "labels": {"r": "Red", "b": "Blue"}},
            ],
            "title": "",
        },
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(ansi=True, width=90))
    assert "Question unavailable" in out       # not the old bare "Asked the user"
    assert "Q: Pick a colour" in out           # question context preserved
    assert "No interactive question UI" in out # error message visible
    assert "\n" in out                         # multi-line → foldable card
    assert "Error (" not in out                # benign → no red Error line


def test_ask_renderer_hard_error_shows_questions_and_error_line() -> None:
    """A hard error (callback crash) shows the questions + a red Error line."""
    p = _end(
        "AskUserQuestion",
        output="AskUserQuestion raised: RuntimeError(callback broke)",
        is_error=True,
        error_kind="tool_error",
        error_message="AskUserQuestion raised: RuntimeError(callback broke)",
        metadata={
            "questions": [
                {"id": "q1", "prompt": "Pick a colour",
                 "labels": {"r": "Red", "b": "Blue"}},
            ],
            "title": "",
        },
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(ansi=True, width=90))
    assert "Question failed" in out
    assert "Q: Pick a colour" in out
    assert "Error (" in out                    # hard error → red Error line
    assert "\n" in out


def test_ask_renderer_hard_error_no_questions_is_inline() -> None:
    """Hard error with no question metadata → inline line with error info."""
    p = _end(
        "AskUserQuestion",
        output="RuntimeError: callback broke",
        is_error=True,
        error_kind="tool_error",
        error_message="RuntimeError: callback broke",
    )
    out = get_tool_renderer("AskUserQuestion")(p, RenderContext(width=90))
    assert "Question failed" in out



# ── generate_image: inline line shows path and the model used ─────────────────

def test_generate_image_shows_path_and_model() -> None:
    p = _end(
        "generate_image",
        output="Generated image: /tmp/cat.png",
        metadata={"path": "/tmp/cat.png", "model": "bytedance-seed/seedream-4.5"},
    )
    out = get_tool_renderer("generate_image")(p, RenderContext(width=90))
    assert out.startswith("⊡ Image → /tmp/cat.png · bytedance-seed/seedream-4.5")


def test_generate_image_without_model_omits_suffix() -> None:
    p = _end(
        "generate_image",
        output="Generated image: /tmp/cat.png",
        metadata={"path": "/tmp/cat.png"},
    )
    out = get_tool_renderer("generate_image")(p, RenderContext(width=90))
    assert out.startswith("⊡ Image → /tmp/cat.png")
    assert "·" not in out


# ── read_image: foldable vision-delegation card (title + question + answer) ───

def test_read_image_card_shows_source_model_and_answer() -> None:
    p = _end(
        "read_image",
        output="A red bicycle leaning on a brick wall.",
        metadata={
            "model": "minimax/minimax-m3",
            "source": "shot.png",
            "source_type": "path",
            "question": "What is in the picture?",
            "chars": 38,
        },
    )
    out = get_tool_renderer("read_image")(p, RenderContext(width=90))
    lines = out.split("\n")
    assert lines[0].startswith("◉ Read image shot.png · minimax/minimax-m3")  # title
    assert "Q: What is in the picture?" in out
    assert "Answer · 38 chars" in out
    assert "A red bicycle leaning on a brick wall." in out
    assert "\n" in out  # multi-line → auto-folds


def test_read_image_card_multi_lists_each_source() -> None:
    p = _end(
        "read_image",
        output="The second screenshot adds a dark-mode toggle.",
        metadata={
            "model": "minimax/minimax-m3",
            "sources": ["before.png", "after.png", "https://x/c.png"],
            "count": 3,
            "question": "what changed?",
            "chars": 46,
        },
    )
    out = get_tool_renderer("read_image")(p, RenderContext(width=90))
    lines = out.split("\n")
    assert lines[0].startswith("◉ Read 3 images · minimax/minimax-m3")  # plural title
    assert "• before.png" in out
    assert "• after.png" in out
    assert "• https://x/c.png" in out
    assert "Q: what changed?" in out
    assert "The second screenshot adds a dark-mode toggle." in out


def test_read_image_in_always_collapse_set() -> None:
    """The sink must treat read_image as an always-fold (collapsed) card."""
    from autoverse.frontends.repl.tui_sink import TuiEventSink

    assert "read_image" in TuiEventSink._ALWAYS_COLLAPSE_TOOLS


def test_read_image_error_stays_inline_one_line() -> None:
    p = _end(
        "read_image",
        output="Image file not found: nope.png",
        is_error=True,
        error_kind="not_found",
        error_message="Image file not found: nope.png",
        metadata={"source": "nope.png", "model": "minimax/minimax-m3"},
    )
    out = get_tool_renderer("read_image")(p, RenderContext(width=90))
    # Inline label (icon + source); hard-error body is appended by the sink, not here.
    assert out.startswith("◉ Read image nope.png")


# ── session replay must rebuild rich tool cards from persisted metadata ────────
# Regression for docs/architecture/frontend-adapters.md §5.6.3: a backgrounded turn renders only via
# render_history_blocks on switch-back, so dropping ToolResultBlock.metadata made
# every tool come back as a bare label (Read with no path, $ bash with no cmd).


def _replay_text(messages: list) -> str:
    from autoverse.frontends.repl.renderer import render_history_blocks

    blocks = render_history_blocks(messages, RenderContext(width=90))
    return "\n".join(b.text for b in blocks)


def test_replay_recovers_inline_tool_label_from_metadata() -> None:
    from autoverse.types.messages import (
        Message,
        ToolResultBlock,
        ToolUseBlock,
    )

    messages = [
        Message(role="assistant", content=(ToolUseBlock("t1", "read_file", {"file_path": "/a.py"}),)),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_use_id="t1",
                    content="contents",
                    metadata={"file_path": "/a.py", "lines": "1-20"},
                ),
            ),
        ),
    ]
    out = _replay_text(messages)
    assert "Read /a.py" in out
    assert "lines 1-20" in out


def test_replay_recovers_bash_command_from_metadata() -> None:
    from autoverse.types.messages import Message, ToolResultBlock, ToolUseBlock

    messages = [
        Message(role="assistant", content=(ToolUseBlock("t1", "bash", {"command": "ls -la"}),)),
        Message(
            role="user",
            content=(
                ToolResultBlock(tool_use_id="t1", content="", metadata={"command": "ls -la"}),
            ),
        ),
    ]
    assert "$ ls -la" in _replay_text(messages)


def test_replay_recovers_error_kind_for_benign_classification() -> None:
    """A benign failure persisted with error_kind folds struck-through, not a red
    Error card, on replay — mirroring the live ToolEndPayload (dispatcher)."""
    from autoverse.types.messages import Message, ToolResultBlock, ToolUseBlock

    messages = [
        Message(role="assistant", content=(ToolUseBlock("t1", "grep", {"pattern": "x"}),)),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_use_id="t1",
                    content="no matches found",
                    is_error=True,
                    metadata={"pattern": "x", "error_kind": "no_match"},
                ),
            ),
        ),
    ]
    out = _replay_text(messages)
    # Benign → struck-through inline line, NOT a hard "Error (N lines)" card.
    assert "Error (" not in out
