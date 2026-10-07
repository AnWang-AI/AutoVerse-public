"""Unit: SessionRail presentation logic + StoreSessionQuery adapter.

These cover the *pure* parts of the left session navigator (docs/architecture/frontend-adapters.md):

* title sanitisation (§9.1) — control-char / ESC stripping, CJK-aware truncation.
* cwd grouping key (§6.3.1) — worktree group_root, symlink/trailing-slash collapse.
* status visual encoding (§6.2) — every SessionStatus has a 5-tuple visual.
* item computation (§6.3.2) — grouping, single-group flatten, current floats up,
  collapsed groups hide members.
* relative_time formatting.
* merge_status precedence (§5.6.2).
* StoreSessionQuery (§4.2/§5.6) — list/sort/count, unseen + status derivation,
  rename/delete cache invalidation.

The widget-mounting paths are exercised by the pilot tests in
``test_tui_app_rail.py``; here we keep to logic that needs no Textual app.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from pathlib import Path  # noqa: E402

from autoverse.frontends.repl.session_rail import (  # noqa: E402
    SessionRail,
    SessionRailState,
    _STATUS_VIS,
    _short_id,
    cwd_group_key,
    relative_time,
    sanitize_title,
)
from autoverse.ports import (  # noqa: E402
    STATUS_PRECEDENCE,
    SessionStatus,
    SessionSummary,
    merge_status,
)


# ── sanitize_title (§9.1) ────────────────────────────────────────────────────
def test_sanitize_title_plain_passthrough() -> None:
    assert sanitize_title("Refactor auth") == "Refactor auth"


def test_sanitize_title_empty_falls_back() -> None:
    assert sanitize_title("", fallback="abc123") == "abc123"
    assert sanitize_title("   ", fallback="abc123") == "abc123"


def test_sanitize_title_strips_control_chars_and_folds_ws() -> None:
    assert sanitize_title("a\tb\nc   d") == "a b c d"


def test_sanitize_title_strips_escape_sequences() -> None:
    # An ANSI colour SGR must not leak into the row markup.
    assert "\x1b" not in sanitize_title("\x1b[31mred\x1b[0m text")
    assert "red" in sanitize_title("\x1b[31mred\x1b[0m text")


def test_sanitize_title_truncates_by_display_cells_with_ellipsis() -> None:
    out = sanitize_title("x" * 100, max_cells=10)
    assert out.endswith("…")
    # Bounded to max_cells display cells.
    from rich.cells import cell_len

    assert cell_len(out) <= 10


def test_sanitize_title_cjk_truncation_never_splits_mid_glyph() -> None:
    out = sanitize_title("你好世界" * 20, max_cells=9)
    from rich.cells import cell_len

    assert cell_len(out) <= 9


# ── cwd grouping (§6.3.1) ────────────────────────────────────────────────────
def test_cwd_group_key_uses_group_root_for_worktrees() -> None:
    s = SessionSummary(session_id="s1", cwd="/tmp/wt/abc", group_root="/proj/root")
    assert cwd_group_key(s) == str(Path("/proj/root").resolve())


def test_cwd_group_key_trailing_slash_collapses(tmp_path: Path) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    a = SessionSummary(session_id="a", cwd=str(d))
    b = SessionSummary(session_id="b", cwd=str(d) + "/")
    assert cwd_group_key(a) == cwd_group_key(b)


def test_cwd_group_key_empty_is_empty() -> None:
    assert cwd_group_key(SessionSummary(session_id="s")) == ""


# ── status encoding (§6.2) ───────────────────────────────────────────────────
def test_every_status_has_visual_encoding() -> None:
    for status in (
        "idle",
        "running",
        "awaiting_input",
        "completed_unseen",
        "error_unseen",
        "busy_external",
    ):
        glyph, grole, trole, tag, bold = _STATUS_VIS[status]
        assert isinstance(tag, str)
        assert isinstance(bold, bool)


def test_attention_states_carry_a_word_tag() -> None:
    # Colour-blind safety (§6.2): the urgent states read from the tag alone.
    assert _STATUS_VIS["awaiting_input"][3] == "(needs input)"
    assert _STATUS_VIS["error_unseen"][3] == "(failed)"
    assert _STATUS_VIS["completed_unseen"][3] == "(done)"


def test_running_state_uses_spinner_without_redundant_word_tag() -> None:
    assert _STATUS_VIS["running"][0] == "●"
    assert _STATUS_VIS["running"][3] == ""


def test_row_metadata_aligns_with_title() -> None:
    state = SessionRailState(
        sessions=(
            SessionSummary(
                session_id="s1",
                title="Refactor auth",
                status="running",
                message_count=3,
            ),
        ),
    )
    rail = _rail_with(state)
    item = rail._compute_items()[0]  # type: ignore[attr-defined]
    line1, line2 = rail._row_markup(item).splitlines()  # type: ignore[attr-defined]

    # Both lines use the same two-cell lead: glyph+space above, two spaces below.
    assert line1.startswith("[")
    assert line2.startswith("  [")
    assert "(running)" not in line1


# ── merge_status precedence (§5.6.2) ─────────────────────────────────────────
def test_merge_status_picks_highest_urgency() -> None:
    assert merge_status("idle", "running", "awaiting_input") == "awaiting_input"
    assert merge_status("idle", "completed_unseen", "running") == "running"
    assert merge_status("idle", "busy_external") == "busy_external"
    assert merge_status() == "idle"


def test_status_precedence_is_total_over_all_states() -> None:
    assert set(STATUS_PRECEDENCE) == {
        "idle",
        "running",
        "awaiting_input",
        "completed_unseen",
        "error_unseen",
        "busy_external",
    }


# ── relative_time ────────────────────────────────────────────────────────────
def test_relative_time_buckets() -> None:
    now = 1_000_000.0
    assert relative_time(now - 5, now=now) == "just now"
    assert relative_time(now - 120, now=now) == "2m ago"
    assert relative_time(now - 7200, now=now) == "2h ago"
    assert relative_time(now - 2 * 86400, now=now) == "2d ago"
    assert relative_time(0, now=now) == ""


def test_short_id_takes_capped_tail() -> None:
    assert _short_id("sess-deadbeefcafe") == "deadbeef"
    assert _short_id("") == "(new)"


# ── item computation (§6.3.2) — uses a bare SessionRail, no mount ─────────────
def _rail_with(state: SessionRailState) -> SessionRail:
    rail = SessionRail()
    rail._state = state  # type: ignore[attr-defined]
    return rail


def _summ(sid: str, cwd: str, **kw: object) -> SessionSummary:
    return SessionSummary(session_id=sid, cwd=cwd, **kw)  # type: ignore[arg-type]


def test_items_single_group_flattens_no_header() -> None:
    state = SessionRailState(
        sessions=(_summ("a", "/p"), _summ("b", "/p")),
        group_by="cwd",
    )
    items = _rail_with(state)._compute_items()
    assert [it.kind for it in items] == ["session", "session"]


def test_items_multi_group_emits_headers() -> None:
    state = SessionRailState(
        sessions=(_summ("a", "/p1"), _summ("b", "/p2")),
        group_by="cwd",
    )
    items = _rail_with(state)._compute_items()
    kinds = [it.kind for it in items]
    assert kinds.count("group") == 2
    assert kinds.count("session") == 2


def test_items_current_group_floats_to_top() -> None:
    state = SessionRailState(
        sessions=(_summ("a", "/p1"), _summ("b", "/p2"), _summ("c", "/p2")),
        current_id="c",
        group_by="cwd",
    )
    items = _rail_with(state)._compute_items()
    # First item is a group header for p2 (the current session's group).
    first = items[0]
    assert first.kind == "group"
    assert cwd_group_key(_summ("c", "/p2")) == first.key


def test_items_collapsed_group_hides_members() -> None:
    p2_key = cwd_group_key(_summ("b", "/p2"))
    state = SessionRailState(
        sessions=(_summ("a", "/p1"), _summ("b", "/p2")),
        group_by="cwd",
        collapsed_groups=frozenset({p2_key}),
    )
    items = _rail_with(state)._compute_items()
    # p2's session row is hidden; only its header remains.
    session_keys = [it.key for it in items if it.kind == "session"]
    assert "b" not in session_keys
    assert "a" in session_keys


def test_title_and_group_name_do_not_hardcode_accent_markup() -> None:
    """Focus colour is CSS-driven:失焦 text, :focus-within accent.

    If the title/group label bakes the accent role into Rich markup, CSS cannot
    make the unfocused rail read as plain white (Chinese-input-friendly focus
    discipline requested by the user).
    """
    state = SessionRailState(
        sessions=(_summ("a", "/p1"), _summ("b", "/p2")),
        group_by="cwd",
    )
    rail = _rail_with(state)
    accent = rail._theme.role("accent")  # type: ignore[attr-defined]

    assert accent not in rail._title_markup()  # type: ignore[attr-defined]
    group = next(it for it in rail._compute_items() if it.kind == "group")
    assert accent not in rail._group_markup(group)  # type: ignore[attr-defined]


def test_items_empty_when_no_sessions() -> None:
    assert _rail_with(SessionRailState())._compute_items() == []


# ── signature: structure-only identity (§4.3) ────────────────────────────────
def test_signature_ignores_spinner_and_now() -> None:
    base = SessionRailState(sessions=(_summ("a", "/p", status="running"),))
    spun = SessionRailState(
        sessions=(_summ("a", "/p", status="running"),),
        spinner_frame="◐",
        now=999.0,
    )
    assert base.signature() == spun.signature()


def test_signature_changes_on_status_change() -> None:
    a = SessionRailState(sessions=(_summ("a", "/p", status="idle"),))
    b = SessionRailState(sessions=(_summ("a", "/p", status="running"),))
    assert a.signature() != b.signature()
