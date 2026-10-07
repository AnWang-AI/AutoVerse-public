"""Sidebar — a persistent session dashboard.

A right-hand panel showing Context (tokens / % of window / TPS / cost), CWD,
the current Todo list, active sub-Agents, and a brand footer. It is **pure
presentation** (P3): all data arrives via :class:`SidebarState`, assembled by
the app from the event-collecting :class:`~autoverse.frontends.repl.tui_sink.TuiEventSink`
and app-held facts (cwd / model / version). Panels with no data are skipped.

The dashboard panels (Session/Context/Directory/Todo) render as a single
``Static`` (a Textual ``Content`` of console markup). The **Agents** panel is
different: each sub-agent row is its own clickable :class:`AgentRow` widget so
the user can click it to drill into that sub-agent's context (MiMo route-driven
parity) — clicking posts :class:`Sidebar.AgentClicked` carrying the row's
``job_id``, which the app maps to a :class:`SubAgentRecord`.

Visual language matches the rest of the TUI: bold ``text`` titles, ``text_muted``
data rows, the single amber accent, no boxes (cockpit style). Width is fixed at
42 columns by the app's CSS; auto-shown on wide terminals and toggled with
``Ctrl+G``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.cells import cell_len, set_cell_size
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from autoverse.frontends.repl.renderer import SUBAGENT_ROLE as _AGENT_ROLE
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme

# Per-subagent-type accent imported VERBATIM from the renderer (single source of
# truth) so a running row tints identically to the same agent's line in the main
# conversation. Terminal rows pick a status colour from _STATUS_GLYPH instead.

# Lifecycle status → (leading glyph, theme role override). An empty role means
# "use the per-type accent" (running). Terminal states get an explicit colour so
# a completed ✓ / failed ✗ reads at a glance (MiMo actor lastOutcome parity).
_STATUS_GLYPH: dict[str, tuple[str, str]] = {
    "running": ("●", ""),
    "pending": ("○", "text_muted"),
    "done": ("✓", "success"),
    "failed": ("✗", "error"),
    "cancelled": ("⊘", "text_muted"),
}

# Fallback content width (cells) for the dashboard text column when the live
# widget size isn't available yet (pre-mount / first paint). Derived from the
# CSS: Sidebar ``width: 42`` (border-box) − ``border-left`` (1) − ``padding 1 2``
# (2+2) − the VerticalScroll scrollbar (1) ≈ 36. Using the conservative value
# guarantees pre-wrapped rows fit whether or not the scrollbar is shown, so
# Textual never *re-wraps* them (which would drop continuation lines to column 0
# and break the hanging indent).
_SIDEBAR_CONTENT_WIDTH = 34
_TODO_MAX_LINES = 2
_TODO_HANG = "  "


@dataclass(frozen=True)
class SidebarExtension:
    """One loaded skill/plugin summary for the right sidebar."""

    kind: str
    name: str
    state: str = "enabled"
    source: str = ""
    diagnostics_count: int = 0


@dataclass(frozen=True)
class SidebarState:
    """Everything the sidebar renders — assembled by the app each tick."""

    model: str = ""
    version: str = "0.1.0"
    session_id: str = ""
    cwd: str = ""
    context_window: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    tps: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_hit_rate: float = 0.0    # 0.0–1.0; fraction of prompt served from cache
    todos: tuple[tuple[str, str], ...] = ()          # (status, text)
    # (path, status, added, removed). status ∈ changed|deleted. Files mutated by
    # this session's write_file/edit_file/delete_file tools (MiMo File.status
    # parity, but event-sourced not git). path is relative to cwd (app does it).
    changed_files: tuple[tuple[str, str, int, int], ...] = ()
    # (job_id, subagent_type, activity, status, turn, elapsed_ms). status ∈
    # running|done|failed|cancelled|"" — drives the row symbol + colour. job_id
    # lets a clicked row map back to its SubAgentRecord (drill-down).
    agents: tuple[tuple[str, str, str, str, int, float], ...] = ()
    # (tier_name, model_id) for the three sub-agent tiers (deep/balanced/fast).
    # model_id == "" means that tier is unset → falls back to the main model.
    model_tiers: tuple[tuple[str, str], ...] = ()
    # Memory files loaded from cwd (e.g. ("CLAUDE.md", "AGENTS.md")). Display-only;
    # the actual memory is injected into the system prompt via MemoryLoader.
    memory_files: tuple[str, ...] = ()
    # Loaded skills/plugins published by the shared extension admin service.
    extensions: tuple[SidebarExtension, ...] = ()
    panels: tuple[str, ...] = field(
        default=(
            "context", "tiers", "cwd", "memory", "extensions", "todo",
            "changed", "agents",
        )
    )


def _kfmt(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _dur(ms: float) -> str:
    """Compact elapsed: 8.5s / 2m / 1h (sidebar is width-constrained)."""
    s = ms / 1000.0
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.0f}h"


def _esc(text: str) -> str:
    # Escape Textual content-markup brackets in dynamic text (paths, todos).
    return text.replace("\\", "\\\\").replace("[", "\\[")


def _clip_cells(text: str, width: int) -> str:
    if cell_len(text) <= width:
        return text
    if width <= 1:
        return "…"
    return set_cell_size(text, width - 1).rstrip() + "…"


def _split_cells(text: str, width: int) -> list[str]:
    """Split a long token into chunks that each fit ``width`` display cells."""
    if cell_len(text) <= width:
        return [text]
    if text.isascii():
        return [_clip_cells(text, width)]
    chunks: list[str] = []
    cur: list[str] = []
    used = 0
    for ch in text:
        w = max(1, cell_len(ch))
        if cur and used + w > width:
            chunks.append("".join(cur))
            cur = []
            used = 0
        cur.append(ch)
        used += w
    if cur:
        chunks.append("".join(cur))
    return chunks


def _wrap_cells(text: str, width: int) -> list[str]:
    """Wrap text by terminal display cells, preserving word boundaries when possible."""
    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        if not word:
            continue
        for part in _split_cells(word, width):
            candidate = part if not cur else f"{cur} {part}"
            if cell_len(candidate) <= width:
                cur = candidate
                continue
            if cur:
                lines.append(cur)
            cur = part
    if cur:
        lines.append(cur)
    return lines


def _todo_progress_bar(theme: Theme, done: int, total: int, width: int) -> str:
    """A thin, single-cell-tall progress ruler under the Todo header.

    Filled portion uses the amber accent, the remainder a dim hairline — so the
    eye gets an at-a-glance sense of "how far along" without reading the count.
    Heavy/light box-drawing glyphs (``━``/``─``) keep it to one row and align
    with the dashboard's flat, box-free language.
    """
    bar_width = max(4, width)
    filled = round(done / total * bar_width) if total else 0
    filled = max(0, min(bar_width, filled))
    done_part = "━" * filled
    rest_part = "─" * (bar_width - filled)
    return (
        f"[{theme.role('accent')}]{done_part}[/]"
        f"[{theme.role('text_dim')}]{rest_part}[/]"
    )


def _todo_row_markup(
    theme: Theme,
    status: str,
    text: str,
    *,
    width: int = _SIDEBAR_CONTENT_WIDTH,
) -> str:
    """Console markup for one sidebar todo row with readable hanging wrap.

    The sidebar is width-constrained. Letting Textual wrap ``○ long task`` on its
    own makes continuation rows start at column 0, which visually detaches them
    from the list item. We pre-wrap here so every continuation aligns under the
    task text and very long rows stay bounded.

    ``width`` MUST be the dashboard's *real* content width (cells). Each emitted
    line is at most ``width`` cells, so Textual never re-wraps a pre-wrapped row
    — that re-wrap is exactly what dropped continuation lines to column 0 and
    broke the hanging indent.
    """
    if status == "completed":
        symbol, role, bold = "✓", "success", False
    elif status in ("in_progress", "active"):
        symbol, role, bold = "●", "accent", True
    else:
        symbol, role, bold = "○", "text_muted", False

    clean = " ".join(str(text).split()).strip()
    if not clean:
        clean = "(empty)"
    # The symbol + its trailing space sets the hanging indent: every wrapped
    # continuation aligns under the first character of the task text, never the
    # symbol. ``_TODO_HANG`` is sized to match (two cells) so the two paths agree.
    row_width = max(8, width)
    prefix_width = cell_len(f"{symbol} ")
    hang_width = cell_len(_TODO_HANG)
    indent_width = max(prefix_width, hang_width)
    text_width = max(4, row_width - indent_width)
    wrapped = _wrap_cells(clean, text_width) or [clean]

    clipped = [_clip_cells(part, text_width) for part in wrapped]
    truncated = len(clipped) > _TODO_MAX_LINES
    lines = clipped[:_TODO_MAX_LINES]
    if truncated:
        lines[-1] = _clip_cells(lines[-1].rstrip("…") + "…", text_width)

    plain = [f"{symbol} {lines[0]}"]
    plain.extend(f"{_TODO_HANG}{part}" for part in lines[1:])

    tag = f"bold {theme.role(role)}" if bold else theme.role(role)
    return "\n".join(f"[{tag}]{_esc(line)}[/]" for line in plain)


def _agent_row_markup(
    theme: Theme,
    sub: str,
    activity: str,
    status: str,
    turn: int,
    elapsed_ms: float,
) -> str:
    """Console markup for one agent row (shared by the row widget)."""
    sym, status_role = _STATUS_GLYPH.get(status or "running", ("●", ""))
    role = theme.role(status_role or _AGENT_ROLE.get(sub, "accent"))
    meta: list[str] = []
    if turn:
        meta.append(f"{turn}t")
    if elapsed_ms >= 1000:
        meta.append(_dur(elapsed_ms))
    suffix = (
        f"  [{theme.role('text_dim')}]{_esc(' · '.join(meta))}[/]" if meta else ""
    )
    return f"[{role}]{sym} \\[{_esc(sub)}] {_esc(activity)}[/]{suffix}"


class AgentRow(Static):
    """One clickable sub-agent row. Clicking posts :class:`Sidebar.AgentClicked`.

    The row's text is refreshed **in place** via :meth:`set_markup` (not by
    re-mounting the widget) so its identity stays stable across the 10 Hz sidebar
    tick — otherwise a real mouse click (down+up spanning a tick) would land on a
    widget that was destroyed and recreated mid-click and be lost.
    """

    def __init__(self, job_id: str, markup: str, **kwargs: object) -> None:
        super().__init__(Content.from_markup(markup), **kwargs)  # type: ignore[arg-type]
        self.job_id = job_id

    def set_markup(self, markup: str) -> None:
        """Update the row's rendered text without replacing the widget."""
        self.update(Content.from_markup(markup))

    def on_click(self) -> None:
        self.post_message(Sidebar.AgentClicked(self.job_id))


class Sidebar(Vertical):
    """Session dashboard panel. Call :meth:`set_state` to refresh.

    A composing container: dashboard panels render as a single ``Static``; the
    Agents panel is a list of clickable :class:`AgentRow` widgets; the brand
    line is a pinned footer.
    """

    class AgentClicked(Message):
        """Posted when a sub-agent row is clicked. Carries the row's ``job_id``."""

        def __init__(self, job_id: str) -> None:
            super().__init__()
            self.job_id = job_id

    def __init__(self, *, theme: Theme | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._theme = theme or DEFAULT_THEME
        self._state = SidebarState()
        # In-place agent-row tracking: the ordered job_ids currently mounted and
        # the running-count baked into the header. When these are unchanged the
        # tick updates row text in place instead of re-mounting (preserves the
        # clickable AgentRow identities — see AgentRow docstring).
        self._agent_order: tuple[str, ...] = ()
        self._agent_running: int = -1
        # job_id → last markup pushed to its row. Lets the fast-path tick skip
        # the ``set_markup`` (and its Content parse + widget refresh) when the
        # row's rendered text is unchanged — e.g. the live elapsed renders to the
        # same whole-second label for ~10 consecutive ticks.
        self._agent_markup: dict[str, str] = {}

    # ── composition ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        # Dashboard + agents live in a scroll region so a tall session (long todo
        # / changed-files / many sub-agents) can be scrolled instead of clipping
        # or shoving the brand off-screen. The brand stays pinned at the bottom.
        with VerticalScroll(id="sidebar-scroll"):
            yield Static(self._render_dashboard(), id="sidebar-dash")
            yield Vertical(id="sidebar-agents")
        yield Static(self._render_brand(), id="sidebar-brand")

    def on_mount(self) -> None:
        self._sync()

    def set_state(self, state: SidebarState) -> None:
        """Refresh from new state, doing the minimum work the diff allows.

        Called on the app's 10 Hz tick, so it must be cheap when little changed:
        if the state is identical, do nothing; if only the ``agents`` tuple moved
        (the common case — a live elapsed advancing), skip the dashboard re-render
        and just update the agent rows. Only a real dashboard-field change rebuilds
        the (string-heavy) dashboard markup."""
        old = self._state
        if state == old:
            return
        self._state = state
        from dataclasses import replace

        # Compare everything EXCEPT the agents tuple — those rows are handled
        # incrementally by ``_sync_agents`` (and their elapsed ticks every frame).
        if replace(state, agents=()) != replace(old, agents=()):
            self._render_static_panels()
        self._sync_agents()

    def _sync(self) -> None:
        """Push the full current state into the three regions (used on mount)."""
        self._render_static_panels()
        self._sync_agents()

    def _render_static_panels(self) -> None:
        """Re-render the dashboard + brand Statics (no-op before mount)."""
        try:
            self.query_one("#sidebar-dash", Static).update(self._render_dashboard())
            self.query_one("#sidebar-brand", Static).update(self._render_brand())
        except Exception:  # pragma: no cover - before mount
            return

    # ── agents panel (clickable rows) ─────────────────────────────────────────
    def _sync_agents(self) -> None:
        try:
            box = self.query_one("#sidebar-agents", Vertical)
        except Exception:  # pragma: no cover - before mount
            return
        s = self._state
        t = self._theme
        show = "agents" in s.panels and bool(s.agents)
        if not show:
            if self._agent_order:  # had rows → clear once
                box.remove_children()
                self._agent_order = ()
                self._agent_running = -1
                self._agent_markup.clear()
            return

        running = sum(1 for a in s.agents if (a[3] or "running") == "running")
        order = tuple(a[0] for a in s.agents)

        # Fast path (the 10 Hz tick): the SAME agents in the SAME order with the
        # same running count → update each row's text in place. This is the whole
        # point — re-mounting here would destroy the clickable AgentRow widgets
        # every 100 ms and swallow real mouse clicks (down+up across a tick).
        if order == self._agent_order and running == self._agent_running:
            rows = {r.job_id: r for r in box.query(AgentRow)}
            for job, sub, activity, status, turn, elapsed_ms in s.agents:
                row = rows.get(job)
                if row is None:
                    continue
                markup = _agent_row_markup(t, sub, activity, status, turn, elapsed_ms)
                # Skip the update (Content parse + widget refresh) when the row's
                # rendered text is unchanged — the live elapsed renders to the same
                # whole-second label for most consecutive ticks.
                if self._agent_markup.get(job) == markup:
                    continue
                self._agent_markup[job] = markup
                row.set_markup(markup)
            return

        # Structure changed (agent dispatched / finished / reordered) — rebuild.
        # This is rare (not every tick), so churning widgets here is harmless.
        box.remove_children()
        self._agent_markup.clear()
        box.mount(
            Static(Content.from_markup(f"[bold {t.role('text')}]Agents  {running} live[/]"))
        )
        for job, sub, activity, status, turn, elapsed_ms in s.agents:
            markup = _agent_row_markup(t, sub, activity, status, turn, elapsed_ms)
            self._agent_markup[job] = markup
            box.mount(AgentRow(job, markup))
        self._agent_order = order
        self._agent_running = running

    def _content_width(self) -> int:
        """Real drawable width (cells) of the dashboard text column.

        Reads the live ``#sidebar-dash`` Static size when mounted (it already
        nets out the scrollbar, since it lives inside the VerticalScroll); falls
        back to the CSS-derived constant before mount / first paint. This is what
        the Todo pre-wrap uses so its rows are never re-wrapped by Textual."""
        try:
            w = self.query_one("#sidebar-dash", Static).content_size.width
            if w > 0:
                return w
        except Exception:  # pragma: no cover - before mount
            pass
        return _SIDEBAR_CONTENT_WIDTH

    # ── dashboard rendering (Static markup) ───────────────────────────────────
    def _render_dashboard(self) -> Content:
        """Assemble the non-agent panels as a Textual ``Content``.

        Textual 8.x renders a ``Content`` reliably (a bare Rich ``Text`` is not
        promoted to a Visual in the widget render path). Markup uses the theme's
        24-bit hex roles: ``[bold #hex]title[/]`` / ``[#hex]row[/]``.
        """
        s = self._state
        t = self._theme
        e = _esc
        cw = self._content_width()

        def title(x: str) -> str:
            return f"[bold {t.role('text')}]{e(x)}[/]"

        def muted(x: str) -> str:
            return f"[{t.role('text_muted')}]{e(x)}[/]"

        panels: list[list[str]] = []

        # Session id pinned to the very top — a truncated, dimmed identifier so
        # `/resume` targets and shared-session references read at a glance.
        if s.session_id:
            sid = s.session_id if len(s.session_id) <= 18 else s.session_id[:18] + "…"
            panels.append([
                title("Session"),
                f"[{t.role('text_dim')}]{e(sid)}[/]",
            ])

        if "context" in s.panels:
            rows = [title("Context"), muted(f"{s.tokens_used:,} tokens")]
            if s.context_window:
                pct = round(s.tokens_used / s.context_window * 100)
                rows.append(muted(f"{pct}% of {_kfmt(s.context_window)} window"))
            if s.tps > 0:
                rows.append(muted(f"{s.tps:.0f} tok/s"))
            # Prompt-cache hit rate (only when there has been cache activity).
            if s.cache_read_tokens or s.cache_write_tokens:
                pct = round(s.cache_hit_rate * 100)
                rows.append(muted(
                    f"cache {pct}% hit · "
                    f"{_kfmt(s.cache_read_tokens)} read / "
                    f"{_kfmt(s.cache_write_tokens)} write"
                ))
            rows.append(
                muted(f"${s.cost_usd:.4f} spent" if s.cost_known else "cost unavailable")
            )
            if s.model:
                rows.append(f"[{t.role('text_dim')}]{e(s.model)}[/]")
            panels.append(rows)

        # Model-tiers panel: the per-tier sub-agent models (deep/balanced/fast),
        # configured via /tiers. Unset tiers render as "(main)" — they fall back
        # to the main agent model. Hidden when no tiers are wired through.
        if "tiers" in s.panels and s.model_tiers:
            rows = [title("Sub-agent models")]
            for name, mid in s.model_tiers:
                shown = e(mid) if mid else "(main)"
                rows.append(
                    f"[{t.role('text_muted')}]{name:<8}[/] "
                    f"[{t.role('text_dim')}]{shown}[/]"
                )
            panels.append(rows)

        if "cwd" in s.panels and s.cwd:
            home = str(Path.home())
            disp = s.cwd.replace(home, "~") if home else s.cwd
            panels.append([title("Directory"), muted(disp)])

        # Memory panel: files loaded from cwd that are injected into the system
        # prompt (e.g. CLAUDE.md, AGENTS.md). Hidden when no files are loaded.
        if "memory" in s.panels and s.memory_files:
            rows = [title("Memory")]
            for fname in s.memory_files:
                rows.append(muted(fname))
            panels.append(rows)

        if "extensions" in s.panels and s.extensions:
            rows = [title(f"Extensions  {len(s.extensions)}")]
            for item in s.extensions:
                kind = "Skill" if item.kind == "skill" else "Plugin"
                state = item.state or "enabled"
                ext_suffix: list[str] = []
                if state != "enabled":
                    ext_suffix.append(state)
                if item.diagnostics_count:
                    ext_suffix.append(f"!{item.diagnostics_count}")
                tail = f" [{', '.join(ext_suffix)}]" if ext_suffix else ""
                rows.append(
                    f"[{t.role('text_muted')}]{kind:<6}[/] "
                    f"[{t.role('text_dim')}]{e(item.name)}{e(tail)}[/]"
                )
            panels.append(rows)

        # Todo panel (MiMo parity): show only while there's still an open item —
        # once every todo is completed (or the list is empty) the panel hides,
        # so finished work doesn't linger in the dashboard.
        if "todo" in s.panels and any(st != "completed" for st, _ in s.todos):
            done = sum(1 for st, _ in s.todos if st == "completed")
            total = len(s.todos)
            rows = [
                f"{title('Todo')}  [{t.role('text_dim')}]{done}/{total}[/]",
            ]
            for st, txt in s.todos:
                rows.append(_todo_row_markup(t, st, txt, width=cw))
            panels.append(rows)

        # Changed-files panel (MiMo File.status parity): files this session's
        # write/edit/delete tools touched. Each row shows the path with +added /
        # -removed counts; deleted files render struck-through. Hidden when none.
        #
        # Colour discipline: the path is a neutral status label, NOT an alert —
        # so it stays in the text tiers (text_muted for changed, text_dim +
        # strike for deleted), never `error` red. Red/green is reserved for the
        # +N/-N magnitudes, and those use the *diff* palette (diff_add/diff_del)
        # so the counts tint identically to the same change in the diff viewer
        # (diff_render's _KIND_ROLES) — not the status success/error greens/reds.
        if "changed" in s.panels and s.changed_files:
            rows = [title(f"Changed  {len(s.changed_files)}")]
            for path, status, added, removed in s.changed_files:
                if status == "deleted":
                    rows.append(f"[{t.role('text_dim')}][s]{e(path)}[/s][/]")
                    continue
                counts = []
                if added:
                    counts.append(f"[{t.role('diff_add')}]+{added}[/]")
                if removed:
                    counts.append(f"[{t.role('diff_del')}]-{removed}[/]")
                suffix = ("  " + " ".join(counts)) if counts else ""
                rows.append(f"[{t.role('text_muted')}]{e(path)}[/]{suffix}")
            panels.append(rows)

        markup = "\n\n".join("\n".join(rows) for rows in panels)
        return Content.from_markup(markup)

    def _render_brand(self) -> Content:
        # Brand wordmark: "Auto" recedes in dim grey while "Verse" carries the
        # single amber accent, so the eye lands on the distinctive half.
        t = self._theme
        return Content.from_markup(
            f"[bold {t.role('accent')}]◆ [/]"
            f"[bold {t.role('text_dim')}]Auto[/]"
            f"[bold {t.role('accent')}]Verse[/] "
            f"[{t.role('text_dim')}]v{_esc(self._state.version)}[/]"
        )


__all__ = ["Sidebar", "SidebarExtension", "SidebarState", "AgentRow"]
