"""SessionRail — the left-hand session navigator (docs/architecture/frontend-adapters.md §4.3 / §6).

A persistent, clickable list of sessions (browse / new / switch / delete /
rename), grouped by working directory. It is **pure presentation** (P3): all
data arrives via :class:`SessionRailState` (assembled by the app from the
read-only :class:`~autoverse.ports.SessionListQuery`), and every user gesture
leaves as a ``Message`` for the app to act on — the rail itself holds no domain
truth and never imports ``core`` / ``EngineBuilder`` / a ``Store`` (§2 P3).

Visual language is **verbatim** the rest of the TUI (§6): semantic theme roles,
the geometric/dingbat glyph family already used by the right ``Sidebar``
(``● ✓ ✗ ◆`` — *never* emoji), ``·`` separators, three text tiers. Three
orthogonal highlights (§6.1) ride independent channels: ``-current`` (which
session is loaded in ``#log``), ``-cursor`` (keyboard caret, only painted while
the rail has focus via ``:focus-within``), and ``:hover``.

Attention states use a leading glyph + title colour + concise text tag. The
routine running state stays deliberately quiet: its animated glyph already
communicates activity, so repeating ``(running)`` only adds visual noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from rich.cells import cell_len, set_cell_size
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.ports import SessionStatus, SessionSummary

# Status → (leading glyph, glyph theme-role, title theme-role, text tag, bold).
# Glyphs are the §6 geometric/dingbat family (ASCII-safe, NOT emoji); ``◆`` is
# the attention glyph added for awaiting_input (§6.2). An empty glyph means the
# row leads with indentation only (idle-other / busy). ``running`` uses the live
# spinner frame supplied by the app, so its glyph here is a placeholder.
_STATUS_VIS: dict[SessionStatus, tuple[str, str, str, str, bool]] = {
    "idle": ("", "", "text_muted", "", False),
    "running": ("●", "accent", "text", "", False),
    "awaiting_input": ("◆", "warning", "warning", "(needs input)", True),
    "completed_unseen": ("✓", "success", "text", "(done)", True),
    "error_unseen": ("✗", "error", "error", "(failed)", True),
    "busy_external": ("", "", "warning", "(busy)", False),
}

# ASCII fallbacks for the non-ASCII glyphs (low-colour / restricted terminals,
# §6 n---). The app may opt into these via ``SessionRailState.ascii``.
_ASCII_GLYPH = {"●": "*", "◆": "!", "✓": "v", "✗": "x", "▾": "v", "▸": ">"}


def sanitize_title(raw: str, *, max_cells: int = 30, fallback: str = "") -> str:
    """Make a user-authored title safe + bounded for a single rail row (§9.1).

    Strips control chars / ESC sequences, folds runs of whitespace to one space,
    then truncates to *max_cells* **display cells** (CJK-aware, never mid-glyph)
    with an ellipsis. Empty after cleaning → *fallback* (the caller passes a short
    id). Persistence keeps the raw value; this is the display-layer defence."""
    if not raw:
        return fallback
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        code = ord(ch)
        if ch == "\x1b":  # drop an ANSI/escape sequence's introducer + a CSI run
            i += 1
            while i < n and raw[i] not in "\x07\x1b" and not (0x40 <= ord(raw[i]) <= 0x7E):
                i += 1
            i += 1  # skip the final byte
            continue
        if code < 0x20 or code == 0x7F:  # other control chars → whitespace
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    cleaned = " ".join("".join(out).split())
    if not cleaned:
        return fallback
    if cell_len(cleaned) > max_cells:
        # set_cell_size pads/truncates by display width; leave room for the "…".
        cleaned = set_cell_size(cleaned, max(1, max_cells - 1)).rstrip() + "…"
    return cleaned


def cwd_group_key(summary: SessionSummary) -> str:
    """Normalised grouping key for a session's working directory (§6.3.1).

    Worktree sessions carry a ``group_root`` (their originating project root) so
    each ephemeral box does not become its own group. Otherwise the cwd is
    canonicalised — realpath (symlinks), trailing slash stripped — so ``/foo``
    and ``/foo/`` and a symlink to it collapse into one stable key. Pure derived
    (P1): recomputable, never persisted."""
    base = summary.group_root or summary.cwd
    if not base:
        return ""
    try:
        resolved = str(Path(base).resolve())
    except OSError:
        resolved = base.rstrip("/") or base
    return resolved


def _group_label(key: str) -> tuple[str, str]:
    """(basename, home-folded full path) for a group header (§6.3.3)."""
    if not key:
        return ("(no directory)", "")
    base = Path(key).name or key
    home = str(Path.home())
    full = key.replace(home, "~", 1) if home and key.startswith(home) else key
    return (base, full)


def relative_time(ts: float, *, now: float) -> str:
    """Compact relative time: ``just now`` / ``2m ago`` / ``3h ago`` / ``5d ago``."""
    if ts <= 0:
        return ""
    delta = max(0.0, now - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _esc(text: str) -> str:
    """Escape Textual content-markup brackets in dynamic text."""
    return text.replace("\\", "\\\\").replace("[", "\\[")


@dataclass(frozen=True)
class SessionRailState:
    """Everything the rail renders — assembled by the app on each refresh.

    ``sessions`` is the pre-sorted summary tuple from ``SessionListQuery.list()``
    (newest first); the rail does the cwd grouping + segmentation locally (§6.3).
    ``running_id`` is redundant (truth is each row's ``SessionSummary.status``,
    §5.6.7) and kept only as a quick locator."""

    sessions: tuple[SessionSummary, ...] = ()
    current_id: str = ""
    running_id: str = ""
    group_by: str = "cwd"
    collapsed_groups: frozenset[str] = frozenset()
    spinner_frame: str = "●"        # live frame for running rows (app supplies)
    ascii: bool = False             # restricted-terminal ASCII glyph fallback
    now: float = 0.0                # reference time for relative_time (testable)

    def signature(self) -> tuple[object, ...]:
        """Structural identity (ignores spinner_frame/now) — when unchanged the
        rail updates row text in place instead of remounting (click-safe, §4.3)."""
        return (
            tuple((s.session_id, s.status, s.unseen, s.title) for s in self.sessions),
            self.current_id,
            self.group_by,
            self.collapsed_groups,
            self.ascii,
        )


@dataclass
class _Item:
    """One visible rail line — a group header or a session row."""

    kind: str                 # "group" | "session"
    key: str = ""             # group_key (header) or session_id (row)
    summary: "SessionSummary | None" = None
    collapsed: bool = False
    rollup: str = ""          # group header attention rollup markup
    count: int = 0


class SessionRow(Static):
    """One clickable session row. Clicking posts :class:`SessionRail.SessionClicked`.

    Refreshed **in place** (``set_markup``) across refreshes so its identity stays
    stable and a real click (down+up spanning a refresh) is not swallowed — same
    discipline as ``sidebar.AgentRow``."""

    def __init__(self, session_id: str, markup: str, **kwargs: object) -> None:
        super().__init__(Content.from_markup(markup), **kwargs)  # type: ignore[arg-type]
        self.session_id = session_id

    def set_markup(self, markup: str) -> None:
        self.update(Content.from_markup(markup))

    def on_click(self) -> None:
        self.post_message(SessionRail.SessionClicked(self.session_id))


class GroupHeader(Static):
    """A clickable directory header — clicking toggles its group (§6.3.3)."""

    def __init__(self, group_key: str, markup: str, **kwargs: object) -> None:
        super().__init__(Content.from_markup(markup), **kwargs)  # type: ignore[arg-type]
        self.group_key = group_key

    def set_markup(self, markup: str) -> None:
        self.update(Content.from_markup(markup))

    def on_click(self) -> None:
        self.post_message(SessionRail.GroupToggled(self.group_key))


class NewSessionRow(Static):
    """The pinned ``+ New session`` action row."""

    def on_click(self) -> None:
        self.post_message(SessionRail.NewSessionClicked())


class SessionRail(Vertical):
    """Left session navigator. Call :meth:`set_state` to refresh.

    Focusable (``ctrl+s`` focuses it, §7.1); while focused ``↑/↓`` move the
    keyboard cursor and ``enter/n/d/r/h/esc`` act on it (§7.2): ``h`` collapses
    the rail, ``esc`` just returns focus. Moving the cursor never switches — only
    ``enter``/click does (P4 explicit)."""

    can_focus = True

    # ── messages to the app ────────────────────────────────────────────────
    class SessionClicked(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class NewSessionClicked(Message):
        pass

    class GroupToggled(Message):
        def __init__(self, group_key: str) -> None:
            super().__init__()
            self.group_key = group_key

    class DeleteRequested(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class RenameRequested(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class FocusReturned(Message):
        """Posted on Esc — the app returns focus to the input area (§7.2)."""

    class HideRequested(Message):
        """Posted on ``h`` — the app collapses the rail and returns focus (§4.1)."""

    def __init__(self, *, theme: Theme | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._theme = theme or DEFAULT_THEME
        self._state = SessionRailState()
        self._items: list[_Item] = []         # current visible items (in order)
        self._sig: tuple[object, ...] | None = None
        # The cursor is a derived index into the *session rows + group headers*
        # of ``_items``; tracked by key so list refreshes re-locate it without
        # jumping (§8). -1 = unset.
        self._cursor_key: str = ""

    # ── composition ────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static(self._title_markup(), id="rail-title", classes="rail-title")
        yield NewSessionRow(
            Content.from_markup(self._new_markup()), id="rail-new", classes="rail-new"
        )
        yield Static(
            Content.from_markup(f"[{self._theme.role('border')}]{'─' * 26}[/]"),
            id="rail-rule",
        )
        yield VerticalScroll(id="rail-list")

    def on_mount(self) -> None:
        self._rebuild()

    # ── state ───────────────────────────────────────────────────────────────
    def set_state(self, state: SessionRailState) -> None:
        """Refresh from new state; cheap when little changed (diff, §4.3)."""
        old = self._state
        if state == old:
            return
        self._state = state
        new_sig = state.signature()
        if new_sig == self._sig and self._items:
            # Structure unchanged → update row/header text in place (spinner,
            # relative time) without remounting (preserves click identities).
            self._refresh_in_place()
            return
        self._sig = new_sig
        self._rebuild()

    def tick_spinner(self, frame: str) -> None:
        """Advance only the running rows' spinner in place (cheap, no re-list).

        Called from the app's 10 Hz tick while a turn runs so the current
        session's ``running`` glyph animates without re-querying the Store
        (§4.2.1 cost discipline). No-op when nothing is running."""
        if not any(s.status == "running" for s in self._state.sessions):
            return
        self._state = replace(self._state, spinner_frame=frame)
        self._refresh_in_place()

    # ── item computation (grouping + segmentation, §6.3.2) ───────────────────
    def _compute_items(self) -> list[_Item]:
        s = self._state
        if not s.sessions:
            return []
        if s.group_by != "cwd":
            # Flat list (time/none axes degrade to a single ungrouped column).
            return [_Item("session", x.session_id, x) for x in s.sessions]

        # Group by normalised cwd, preserving the (already updated_at-sorted)
        # order so each group's first-seen position reflects its newest session.
        groups: dict[str, list[SessionSummary]] = {}
        order: list[str] = []
        for x in s.sessions:
            key = cwd_group_key(x)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(x)

        # ≤1 group → flatten (no redundant single header, §6.3.6).
        if len(order) <= 1:
            return [_Item("session", x.session_id, x) for x in s.sessions]

        # Group containing the current session floats to the top (§6.3.2).
        cur_key = ""
        for x in s.sessions:
            if x.session_id == s.current_id:
                cur_key = cwd_group_key(x)
                break
        if cur_key and cur_key in order:
            order.remove(cur_key)
            order.insert(0, cur_key)

        items: list[_Item] = []
        for key in order:
            members = groups[key]
            collapsed = key in s.collapsed_groups
            items.append(
                _Item(
                    "group",
                    key,
                    collapsed=collapsed,
                    rollup=self._rollup(members),
                    count=len(members),
                )
            )
            if not collapsed:
                items.extend(_Item("session", m.session_id, m) for m in members)
        return items

    def _rollup(self, members: list[SessionSummary]) -> str:
        """Highest-urgency attention count for a group header (§6.3.3)."""
        from autoverse.ports import STATUS_PRECEDENCE

        for status in STATUS_PRECEDENCE:
            if status in ("idle", "busy_external"):
                continue
            n = sum(1 for m in members if m.status == status)
            if n:
                glyph, role, *_ = _STATUS_VIS[status]
                g = self._glyph(glyph)
                return f"[{self._theme.role(role)}]{g} {n}[/]"
        return ""

    # ── rendering ─────────────────────────────────────────────────────────────
    def _rebuild(self) -> None:
        try:
            box = self.query_one("#rail-list", VerticalScroll)
        except Exception:  # pragma: no cover - before mount
            return
        self._items = self._compute_items()
        box.remove_children()
        self._sync_title()
        if not self._items:
            box.mount(
                Static(
                    Content.from_markup(
                        f"[{self._theme.role('text_dim')}]还没有会话，回车开始[/]"
                    ),
                    id="rail-empty",
                )
            )
            return
        # Keep the cursor on a still-present key, else fall back to the current
        # session, else the first row (§8 re-locate by key).
        keys = [it.key for it in self._items]
        if self._cursor_key not in keys:
            self._cursor_key = (
                self._state.current_id if self._state.current_id in keys else keys[0]
            )
        prev_kind: str | None = None
        for it in self._items:
            w = self._build_widget(it)
            # Blank line under a group header, before its first session (only the
            # first child — a collapsed header has none, so no stray gap). Headers
            # carry their own top margin in CSS, so groups stay visually distinct.
            if it.kind == "session" and prev_kind == "group":
                w.add_class("-group-first")
            box.mount(w)
            prev_kind = it.kind

    def _build_widget(self, it: _Item):  # type: ignore[no-untyped-def]
        if it.kind == "group":
            w = GroupHeader(it.key, self._group_markup(it))
            if it.key == self._cursor_key:
                w.add_class("-cursor")
            return w
        row = SessionRow(it.key, self._row_markup(it))
        if it.summary is not None and it.summary.session_id == self._state.current_id:
            row.add_class("-current")
        if it.key == self._cursor_key:
            row.add_class("-cursor")
        return row

    def _refresh_in_place(self) -> None:
        try:
            box = self.query_one("#rail-list", VerticalScroll)
        except Exception:  # pragma: no cover - before mount
            return
        self._sync_title()
        rows = {r.session_id: r for r in box.query(SessionRow)}
        for it in self._items:
            if it.kind == "session":
                r = rows.get(it.key)
                if r is not None:
                    r.set_markup(self._row_markup(it))

    def _sync_title(self) -> None:
        try:
            self.query_one("#rail-title", Static).update(
                Content.from_markup(self._title_markup())
            )
        except Exception:  # pragma: no cover - before mount
            pass

    def _title_markup(self) -> str:
        t = self._theme
        # Attention rollup in the title: count of awaiting_input sessions (§6.2).
        n = sum(1 for s in self._state.sessions if s.status == "awaiting_input")
        suffix = (
            f"  [{t.role('warning')}]{self._glyph('◆')} {n}[/]" if n else ""
        )
        return f"[bold]Sessions[/]{suffix}"

    def _new_markup(self) -> str:
        return f"[{self._theme.role('text_muted')}]+ New session[/]"

    def _group_markup(self, it: _Item) -> str:
        t = self._theme
        chev = self._glyph("▾" if not it.collapsed else "▸")
        base, _full = _group_label(it.key)
        # Line 1: chevron + project name + attention rollup (the urgent glyph that
        # belongs next to the name). Line 2: the plain session count, dim, indented
        # under the name — moving it off line 1 keeps the header clean (§6.3.3).
        line1 = f"[{t.role('text_muted')}]{chev}[/] {_esc(base)}"
        if it.rollup:
            line1 = f"{line1}  {it.rollup}"
        word = "session" if it.count == 1 else "sessions"
        line2 = f"  [{t.role('text_dim')}]{it.count} {word}[/]"
        return f"{line1}\n{line2}"

    def _row_markup(self, it: _Item) -> str:
        t = self._theme
        s = it.summary
        assert s is not None
        status = s.status
        # The current session is marked by the ``-current`` left bar + element
        # background (§6.1①); no separate glyph is drawn for an idle current
        # session. Only a more urgent live state overlays a status glyph.
        glyph, grole, trole, tag, bold = _STATUS_VIS.get(
            status, _STATUS_VIS["idle"]
        )
        # Running uses the live spinner frame (§6.2).
        if status == "running":
            glyph = self._state.spinner_frame or glyph
        lead = f"[{t.role(grole)}]{self._glyph(glyph)}[/] " if glyph else "  "

        title = sanitize_title(
            s.title, max_cells=24, fallback=_short_id(s.session_id)
        )
        weight = "bold " if bold else ""
        title_mk = f"[{weight}{t.role(trole)}]{_esc(title)}[/]"
        tag_mk = f"  [{t.role(trole)}]{tag}[/]" if tag else ""
        line1 = f"{lead}{title_mk}{tag_mk}"

        # Line 2: "N prompts · relative-time" (all dim, §6). Its two-cell lead
        # matches the glyph/blank lead above, so metadata starts on exactly the
        # same visual axis as the title. The count segment is padded to keep the
        # time column aligned across rows. The model name is omitted (it belongs
        # in the #sidebar dashboard and rarely differs per project).
        rel = relative_time(s.updated_at, now=self._state.now)
        noun = "prompt" if s.message_count == 1 else "prompts"
        count_label = f"{s.message_count} {noun}"
        if rel:
            meta = f"{count_label.ljust(11)}· {rel}"
        else:
            meta = count_label
        line2 = f"  [{t.role('text_dim')}]{_esc(meta)}[/]"
        return f"{line1}\n{line2}"

    def _glyph(self, glyph: str) -> str:
        if self._state.ascii:
            return _ASCII_GLYPH.get(glyph, glyph)
        return glyph

    # ── keyboard navigation (only while focused, §7.2) ───────────────────────
    def _nav_keys(self) -> list[str]:
        return [it.key for it in self._items]

    def _move_cursor(self, delta: int) -> None:
        keys = self._nav_keys()
        if not keys:
            return
        try:
            idx = keys.index(self._cursor_key)
        except ValueError:
            idx = 0
        idx = max(0, min(len(keys) - 1, idx + delta))  # stop at ends, no wrap
        self._set_cursor(keys[idx])

    def _set_cursor(self, key: str) -> None:
        if key == self._cursor_key:
            return
        self._cursor_key = key
        # Toggle the -cursor class in place (no remount → no click loss).
        try:
            box = self.query_one("#rail-list", VerticalScroll)
        except Exception:  # pragma: no cover
            return
        for w in box.children:
            wkey = getattr(w, "session_id", None) or getattr(w, "group_key", None)
            w.set_class(wkey == key, "-cursor")
            if wkey == key:
                try:
                    w.scroll_visible(animate=False)
                except Exception:  # pragma: no cover - before layout
                    pass

    def _cursor_item(self) -> "_Item | None":
        for it in self._items:
            if it.key == self._cursor_key:
                return it
        return None

    def focus_default(self) -> None:
        """Place the cursor on the current session (or first row) and focus (§7.1)."""
        keys = self._nav_keys()
        if keys:
            self._set_cursor(
                self._state.current_id if self._state.current_id in keys else keys[0]
            )
        self.focus()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        key = event.key
        if key in ("up", "k"):
            self._move_cursor(-1)
            event.stop()
        elif key in ("down", "j"):
            self._move_cursor(1)
            event.stop()
        elif key == "enter":
            it = self._cursor_item()
            if it is None:
                return
            if it.kind == "group":
                self.post_message(SessionRail.GroupToggled(it.key))
            else:
                self.post_message(SessionRail.SessionClicked(it.key))
            event.stop()
        elif key in ("left", "right", "space") and (
            (it := self._cursor_item()) is not None and it.kind == "group"
        ):
            self.post_message(SessionRail.GroupToggled(it.key))
            event.stop()
        elif key == "n":
            self.post_message(SessionRail.NewSessionClicked())
            event.stop()
        elif key == "d":
            it = self._cursor_item()
            if it is not None and it.kind == "session":
                self.post_message(SessionRail.DeleteRequested(it.key))
            event.stop()
        elif key == "r":
            it = self._cursor_item()
            if it is not None and it.kind == "session":
                self.post_message(SessionRail.RenameRequested(it.key))
            event.stop()
        elif key == "h":
            self.post_message(SessionRail.HideRequested())
            event.stop()
        elif key == "escape":
            self.post_message(SessionRail.FocusReturned())
            event.stop()


def _short_id(session_id: str) -> str:
    """Last segment of a ``sess-<uuid>`` id, capped — the empty-title fallback."""
    tail = session_id.rsplit("-", 1)[-1] if session_id else ""
    return (tail[:8] or session_id[:8]) or "(new)"


__all__ = [
    "SessionRail",
    "SessionRailState",
    "SessionRow",
    "GroupHeader",
    "NewSessionRow",
    "sanitize_title",
    "cwd_group_key",
    "relative_time",
]
