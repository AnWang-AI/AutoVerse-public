"""Unit: rich diff rendering — MiMo ``<diff>`` parity (diff_render.py).

Guards the feature set that closed the gap with MiMo-Code's native ``<diff>``
component: line numbers, syntax highlighting, word-level emphasis, a split view
on wide terminals, and a post-edit diagnostics block — all emitted as ANSI in a
string pipeline, with a deterministic plain fallback (ansi=False) for tests.
"""

from __future__ import annotations

from autoverse.frontends.repl.diff_render import render_diff
from autoverse.frontends.repl.renderer import RenderContext
from autoverse.frontends.repl.tool_renderer import get_tool_renderer
from autoverse.frontends.repl import tool_renderers_builtin  # noqa: F401  (registers)
from autoverse.types.events import ToolEndPayload

_DIFF = (
    "@@ -1,3 +1,4 @@\n"
    " def hello(name):\n"
    '-    return "hi " + name\n'
    '+    greeting = "hello " + name\n'
    "+    return greeting\n"
)


def _end(name: str, **kw: object) -> ToolEndPayload:
    base: dict[str, object] = dict(tool_use_id="t", tool_name=name, output="")
    base.update(kw)
    return ToolEndPayload(**base)  # type: ignore[arg-type]


# ── line numbers + signs (unified, plain) ─────────────────────────────────────
def test_unified_has_line_numbers_and_signs() -> None:
    rows = render_diff(_DIFF, "/app/greet.py", RenderContext(ansi=False, width=90))
    text = "\n".join(rows)
    assert "@@ -1,3 +1,4 @@" in text          # hunk header kept verbatim
    assert 'return "hi " + name' in text       # del row content present
    assert "return greeting" in text           # add row content present
    assert "- " in text and "+ " in text       # sign separated from code by a space
    # New line numbers run 1 (ctx) then 2,3 on the two added lines.
    last = rows[-1]
    assert last.lstrip().startswith("3")        # gutter shows the new line number


def test_plain_is_deterministic_no_ansi() -> None:
    rows = render_diff(_DIFF, "/app/greet.py", RenderContext(ansi=False, width=90))
    assert all("\x1b[" not in r for r in rows)  # no escape codes in plain mode


# ── syntax highlighting ───────────────────────────────────────────────────────
def test_syntax_highlighting_changes_output() -> None:
    """A known lexer must colour code differently than an unknown extension."""
    py = render_diff(_DIFF, "/app/greet.py", RenderContext(ansi=True, width=90))
    unknown = render_diff(_DIFF, "/app/greet.zzz", RenderContext(ansi=True, width=90))
    assert "\x1b[" in "\n".join(py)             # styled
    assert py != unknown                        # lexer actually tokenised the code


# ── word-level emphasis ───────────────────────────────────────────────────────
def test_word_level_emphasis_background() -> None:
    diff = "@@ -1,1 +1,1 @@\n-foobar baz\n+foobar qux\n"
    rows = render_diff(diff, "a.txt", RenderContext(ansi=True, width=90))
    text = "\n".join(rows)
    # Emphasis backgrounds (theme diff_*_emph_bg) tint only the changed span.
    assert "48;2;94;43;46" in text   # diff_del_emph_bg #5e2b2e
    assert "48;2;43;85;68" in text   # diff_add_emph_bg #2b5544


# ── split view on wide terminals ──────────────────────────────────────────────
def test_split_view_on_wide_terminal() -> None:
    rows = render_diff(_DIFF, "/app/greet.py", RenderContext(ansi=False, width=160))
    text = "\n".join(rows)
    assert "│" in text                          # side-by-side separator present
    # A context line shows on BOTH sides (old line 1 left, new line 1 right).
    ctx_row = next(r for r in rows if "hello(name)" in r)
    assert ctx_row.count("hello(name)") == 2


def test_unified_view_on_narrow_terminal() -> None:
    rows = render_diff(_DIFF, "/app/greet.py", RenderContext(ansi=False, width=90))
    # Narrow → unified → no side-by-side separator column.
    assert "│" not in "\n".join(rows)


# ── diagnostics block (via the edit renderer) ─────────────────────────────────
def test_diagnostics_block_rendered_under_diff() -> None:
    p = _end(
        "edit_file",
        output="Edited",
        metadata={
            "file_path": "/a.py",
            "diff": _DIFF,
            "added": 2,
            "removed": 1,
            "diagnostics": [
                {"line": 2, "column": 5, "severity": "warning",
                 "message": "unused variable 'greeting'", "source": "ruff"},
                {"line": 9, "column": 1, "severity": "error",
                 "message": "undefined name 'foo'", "source": "ruff"},
            ],
        },
    )
    out = get_tool_renderer("edit_file")(p, RenderContext(width=90))
    assert "⚠ Diagnostics" in out
    assert "1 error" in out and "1 warning" in out
    assert "2:5 unused variable 'greeting'" in out
    assert "9:1 undefined name 'foo'" in out


def test_no_diagnostics_no_block() -> None:
    p = _end(
        "edit_file",
        output="Edited",
        metadata={"file_path": "/a.py", "diff": _DIFF, "added": 2, "removed": 1,
                  "diagnostics": []},
    )
    out = get_tool_renderer("edit_file")(p, RenderContext(width=90))
    assert "Diagnostics" not in out


def test_diagnostics_caps_long_lists() -> None:
    diags = [
        {"line": i, "column": 1, "severity": "warning", "message": f"w{i}"}
        for i in range(1, 11)
    ]
    p = _end(
        "edit_file",
        output="Edited",
        metadata={"file_path": "/a.py", "diff": _DIFF, "diagnostics": diags},
    )
    out = get_tool_renderer("edit_file")(p, RenderContext(width=90))
    assert "… +5 more" in out  # 10 findings, 5 shown
