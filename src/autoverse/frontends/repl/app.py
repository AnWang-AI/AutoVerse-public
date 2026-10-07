"""AutoVerseApp — the full-screen Textual TUI frontend.

This is the v3 frontend shell: a Textual ``App`` (alternate buffer) composed of a
``RichLog`` output region, a :class:`StatusBar`, and a :class:`CommandInput`. It
is a thin consumption-side adapter over the engine — it contains no domain logic
(P3). All rendering reuses the existing event/tool renderer registry via
:class:`~autoverse.frontends.repl.tui_sink.TuiEventSink`; the framework layer is
unchanged.

Lifecycle:
* The engine is built (by the CLI) with ``event_sink=TuiEventSink``, so every
  ``StreamEvent`` core emits is written to the ``RichLog`` from inside the worker.
* User submissions go through a :class:`MessageQueue`; ``_drain_next`` is a
  ``@work(exclusive=True)`` worker that runs one turn at a time, keeping the
  input box live so the user can queue more input mid-turn (§3.4).
* Ctrl+C escalates via :class:`InterruptHandler` (interrupt → cancel, §3.5).
* A periodic ``set_interval`` tick advances the spinner/elapsed/activity line.

``textual`` is an optional dependency; this module is only imported on the TUI
path (TTY + ``textual`` installed, not ``--print`` / ``--no-tui``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, Sequence, cast

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical

from autoverse.frontends.repl.agent_view import AgentView, _fmt_elapsed
from autoverse.frontends.repl.commands import CommandContext, CommandResult, dispatch
from autoverse.frontends.repl.conversation_log import ConversationLog
from autoverse.frontends.repl.dialogs import (
    ChoiceItem,
    CompletionMenu,
    ConfirmScreen,
    InlineChoiceMenu,
    PromptScreen,
    SelectOption,
    SelectScreen,
    TextScreen,
    Toast,
)
from autoverse.frontends.repl.input_queue import (
    InputMode,
    InputModeStack,
    MessageQueue,
    Priority,
)
from autoverse.frontends.repl.interrupt import InterruptHandler
from autoverse.frontends.repl.plan_approval import approved_plan_prompt
from autoverse.frontends.repl.redirect_sink import RedirectSink
from autoverse.frontends.repl.renderer import RenderContext, render_history_blocks
from autoverse.frontends.repl.session_rail import (
    SessionRail,
    SessionRailState,
    cwd_group_key,
    sanitize_title,
)
from autoverse.frontends.repl.sidebar import Sidebar, SidebarExtension, SidebarState
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.frontends.repl.tui_sink import TuiEventSink
from autoverse.frontends.repl.widgets import (
    CommandInput,
    PromptMeta,
    PromptTail,
    QueuedPanel,
    StatusBar,
)
from autoverse.infra.observ.logging import log_context
from autoverse.types import EventType, StreamEvent
from autoverse.types.events import ErrorPayload, make_event

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.text import Text

    from autoverse.app.context import AppContext
    from autoverse.frontends.repl.commands import CommandRegistry, CommandSpec
    from autoverse.ports import EngineHandle, EventSink, SessionListQuery, SessionStatus
    from autoverse.types.tools import Decision, ToolCall

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# An async callable that builds the engine given the TUI's EventSink. The CLI
# supplies this so the engine is wired with the live RichLog sink created in
# ``on_mount`` (the engine cannot exist before its sink's RichLog widget does).
EngineFactory = Callable[["EventSink"], Awaitable["EngineHandle"]]

# Switch factory (docs/architecture/frontend-adapters.md §5.1): build a fresh engine bound to a given
# ``session_id`` (empty ⇒ a new session). Takes the rebuilt sink because the app
# owns the ``#log``-backed sink and recreates it per switch (§5.2 ⑤). The CLI
# injects this (it may import ``EngineBuilder``; the frontend may not, P3).
EngineSwitcher = Callable[[str, "EventSink"], Awaitable["EngineHandle"]]

# Rail auto-shows on terminals at least this wide (kept below the right sidebar's
# threshold so a medium terminal can still show the navigator, §4.1).
_RAIL_COLS = 100
_RAIL_WIDTH = 34

# Bound on the in-process live-engine pool (docs/architecture/frontend-adapters.md §5.6.3): the
# foreground session plus up to this many backgrounded engines stay hot; the
# oldest *idle* background engine is evicted (aclose) past the cap.
_MAX_LIVE_SESSIONS = 4
_LOG = logging.getLogger(__name__)


class _Settable(Protocol):
    def set(self) -> None: ...


@dataclass
class _LiveSession:
    """One in-process live engine (foreground or backgrounded, §5.6.3).

    ``redirect`` is the stable sink baked into the engine: its target is the live
    foreground :class:`TuiEventSink` while this session is current, ``None`` while
    backgrounded. ``task`` is the in-flight turn's :class:`asyncio.Task` (or None
    when idle); ``detached`` is signalled to release the foreground drain when the
    user switches away mid-turn. ``pending_relay`` parks an approval/question that
    fired while the session was backgrounded, surfaced on switch-in (the §5.6.3
    "接力"). ``last_used`` orders the LRU eviction of idle background engines.
    """

    engine: "EngineHandle"
    redirect: RedirectSink
    interrupt: InterruptHandler
    task: "asyncio.Task[None] | None" = None
    detached: "asyncio.Event | None" = None
    pending_relay: "list[Callable[[], None]]" = field(default_factory=list)
    last_used: float = 0.0


class InputFrame(Vertical):
    """Clickable prompt frame that focuses the real text input."""

    def on_click(self) -> None:
        try:
            self.query_one("#input", CommandInput).focus()
        except Exception:  # pragma: no cover - defensive during teardown
            pass


class ToastHost(Vertical):
    """Overlay anchor that keeps transient notices inside the main pane."""


class AutoVerseApp(App[int]):
    """Full-screen Textual REPL over an :class:`EngineHandle`."""

    # The spinner runs at the full tick rate; the heavier live refreshes (turn
    # footer, sub-agent live cards, sidebar) only run every Nth tick. 2 → 5 Hz,
    # which is indistinguishable for elapsed text but halves the render work.
    _HEAVY_TICK_EVERY = 2

    CSS = """
    Screen { layout: horizontal; background: $av-bg; }
    #main { width: 1fr; height: 100%; }
    /* Content gutter: left pad 3 (clean left margin) + top pad 1. The output
       region is a VerticalScroll of per-block widgets (enables fold/expand). */
    #log { height: 1fr; border: none; padding: 1 2 0 3; background: $av-bg; scrollbar-size-vertical: 1; }
    /* When the in-place AgentView takes over, #log is hidden — it must leave
       the layout entirely (display:none), otherwise its height:1fr keeps
       reserving space and the AgentView is pushed below it. */
    #log.-hidden { display: none; }
    /* Vertical rhythm = EXACTLY one blank row between blocks. Each top-level
       child carries a single bottom margin; Markdown's internal block margins
       (which would otherwise compound into 2-3 blank rows around answers) are
       zeroed via a descendant selector so the only spacing is our 1-row margin. */
    /* Assistant reply text is indented 3 cols (border 1 + pad 2) so its TEXT
       lines up with the user-echo's text — not with the echo's left accent bar.
       The .user-echo / .tool-block class selectors below are more specific and
       keep their own left bar + padding, so only un-classed reply blocks shift. */
    #log > Static { background: $av-bg; margin: 0 0 1 3; }
    /* Sub-agent cards are clickable (drill into the sub-agent's context). Hint
       it on hover: a faint panel wash + pointer cursor, mirroring the sidebar
       AgentRow:hover affordance. The card is a #log > Static (AgentCard). */
    #log > AgentCard:hover { background: $av-panel; }
    #log > Markdown { background: $av-bg; margin: 0 0 1 3; padding: 0; }
    /* The startup banner (wordmark · hint · rule) is flush with the left gutter,
       not indented like reply text — it is a header, not part of an exchange. */
    #log > .banner { margin: 0 0 0 0; }
    #log Markdown { padding: 0; }
    #log Markdown MarkdownBlock { margin: 0; padding: 0; }
    #toast-host {
        layer: overlay; dock: bottom; width: 100%; height: auto;
        align-horizontal: right; background: transparent;
    }
    /* Markdown body theming (taste-skill: one warm palette, no AI-blue). The
       Textual theme above drives heading colours; these pin the remaining
       components onto the charcoal+amber scale so the assistant answer — the
       highest-frequency surface — never falls back to Textual's cool defaults.
       Fenced code: a charcoal panel with muted text instead of black/grey;
       inline code: amber on a faint panel wash (Textual default is warning-
       tinted). Bullets, blockquote rule and HR use the muted/border greys. */
    #log Markdown MarkdownFence {
        background: $av-panel; color: $av-code; margin: 1 0; padding: 1 2;
    }
    #log Markdown MarkdownBlock > .code_inline { background: $av-border-subtle; color: $av-accent; }
    #log Markdown MarkdownBullet { color: $av-text-dim; }
    #log Markdown MarkdownBlockQuote { background: $av-panel; border-left: outer $av-text-dim; }
    #log Markdown MarkdownHorizontalRule { border-bottom: solid $av-border; }
    #log Markdown MarkdownH1 { border: none; }
    #log Markdown MarkdownTable { background: $av-panel; }
    #log Markdown MarkdownTH { color: $av-text; text-style: bold; }
    #log Markdown MarkdownTD { color: $av-code; }
    /* Collapsible foldable cards: flat, no top rule/border, gutter-aligned title,
       amber arrow on hover; body indented 2 under the title, no extra padding. */
    #log > Collapsible { background: $av-bg; border: none; padding: 0; margin: 0 0 1 3; }
    #log > Collapsible > CollapsibleTitle { color: $av-text-muted; padding: 0; background: $av-bg; }
    #log > Collapsible > CollapsibleTitle:hover { color: $av-accent; }
    #log > Collapsible > Contents { padding: 0 0 0 2; margin: 0; background: $av-bg; }
    /* Force the collapsed body OUT of layout so a freshly-mounted card never
       flashes its full body for a frame before Textual's own display:none — kept
       identical to the #agentview rules so both surfaces fold the same way. */
    #log > Collapsible > Contents.-collapsed { padding: 0; display: none; height: 0; }
    /* Clicking a fold's title focuses CollapsibleTitle (can_focus=True), which
       puts the parent Collapsible into :focus-within. Textual's default then
       applies `background-tint: $foreground 5%` — a faint grey wash that PERSISTS
       after the mouse leaves (it tracks focus, not hover) until focus moves
       elsewhere. We override the background but never the *tint*, so neutralise it
       here (0% alpha = no wash) for every collapsed/expanded card in the log. */
    #log > Collapsible:focus-within { background-tint: $av-bg 0%; }
    /* User echo (MiMo parity): a real left accent bar + grey background block,
       not a baked-in ▌ glyph. The bar spans the padded block height; the grey
       wash reads as a card separating each exchange. */
    #log > .user-echo {
        background: $av-panel; color: $av-text;
        border-left: heavy $av-accent; padding: 1 2; margin: 0 0 1 0;
    }
    /* The echoed message keeps the interaction mode it was SENT under: the app
       stamps a ``-mode-*`` class at echo time so the card's left bar is frozen
       to that mode's colour (normal=amber, plan=green, worktree=lavender) and
       does NOT follow later Tab mode changes (docs/guides/using-cli-and-tui.md §3.10). */
    #log > .user-echo.-mode-normal { border-left: heavy $av-warning; }
    #log > .user-echo.-mode-plan { border-left: heavy $av-success; }
    #log > .user-echo.-mode-worktree { border-left: heavy $av-mode-worktree; }
    /* Tool blocks (bash/diff/etc): left grey bar + panel background (MiMo
       BlockTool). Applies to both plain Static blocks and folded cards. The
       folded-card title/contents carry no top/bottom padding so the card hugs
       its rows tightly (block separation comes from the Collapsible's margin). */
    /* Indented 3 cols (left margin) so tool blocks travel with the reply text —
       their grey left bar lines up under the assistant answer / prompt text.
       Vertical padding is 0 (only horizontal 2): the rows hug the grey box top
       and bottom — matching the collapsible variant below — so multi-line tool
       cards (bash/diff/job list) don't carry an extra blank row top+bottom on
       top of the 1-row inter-block margin. */
    #log > .tool-block {
        background: $av-panel;
        border-left: solid $av-border; padding: 0 2; margin: 0 0 1 3;
    }
    /* Transient "running" tool card: a single muted line (spinner + target)
       that appears while a slow tool executes and is removed when its result
       card is written. Aligns with plain content (no grey wash / left bar) so
       it reads as an ephemeral status line, not a committed block. */
    #log > .tool-running { background: $av-bg; padding: 0; margin: 0 0 1 3; }
    /* Tool cards are flat by default (no grey block, no border) and align exactly
       like plain content / a normal Collapsible — same margin & padding, no extra
       left inset. The grey wash appears ONLY on hover; backgrounds don't affect
       layout, so the hover highlight never shifts anything. */
    #log > Collapsible.tool-block { background: $av-bg; border: none; padding: 0; margin: 0 0 1 3; }
    #log > Collapsible.tool-block > CollapsibleTitle { background: $av-bg; color: $av-text-muted; padding: 0; }
    #log > Collapsible.tool-block > Contents { background: $av-bg; padding: 0 0 1 2; }
    #log > Collapsible.tool-block > Contents.-collapsed { padding: 0; display: none; height: 0; }
    #log > Collapsible.tool-block:hover { background: $av-panel; }
    #log > Collapsible.tool-block:hover > CollapsibleTitle { background: $av-panel; color: $av-accent; }
    #log > Collapsible.tool-block:hover > Contents { background: $av-panel; }
    /* AgentView: an in-place sub-agent context view, a sibling of #log inside
       #main. Hidden by default; the app toggles -hidden on #log/#agentview to
       swap views (returning to Main leaves #log untouched → scroll preserved). */
    #agentview { height: 1fr; padding: 1 2 0 3; background: $av-bg; }
    #agentview.-hidden { display: none; }
    #agentview-title { height: auto; margin: 0 0 1 0; color: $av-text; }
    #agentview-body { height: 1fr; }
    #agentview-body > Static { background: $av-bg; margin: 0 0 1 0; }
    /* Task echo: the sub-agent's "user prompt", styled identically to the main
       conversation's #log > .user-echo (amber left bar + grey wash) so the
       drill-down reads exactly like the main exchange. Overrides the plain
       background above for this one block. */
    #agentview-body > .user-echo {
        background: $av-panel; color: $av-text;
        border-left: heavy $av-accent; padding: 1 2; margin: 0 0 1 0;
    }
    /* Foldable Thought / long-tool cards — MUST mirror #log > Collapsible above,
       otherwise a Collapsible in the drill-down falls back to Textual's default
       (a boxed/bordered card) instead of the flat, borderless look the main
       conversation uses. The .tool-block variant adds the grey wash + left bar. */
    #agentview-body > Collapsible { background: $av-bg; border: none; padding: 0; margin: 0 0 1 0; }
    #agentview-body > Collapsible > CollapsibleTitle { color: $av-text-muted; padding: 0; background: $av-bg; }
    #agentview-body > Collapsible > CollapsibleTitle:hover { color: $av-accent; }
    #agentview-body > Collapsible > Contents { padding: 0 0 0 2; margin: 0; background: $av-bg; }
    /* Force the collapsed body OUT of layout (height 0 + hidden) so a freshly
       mounted Thought/tool card never flashes its full (often very long) body
       for a frame before Textual applies its own display:none — the cause of
       the "long thinking flashes on entry" symptom in the sub-agent view. */
    #agentview-body > Collapsible > Contents.-collapsed { padding: 0; display: none; height: 0; }
    /* Same focus-within grey-wash neutralisation as the main #log (see above):
       clicking a fold here also leaves a persistent tint until focus moves on. */
    #agentview-body > Collapsible:focus-within { background-tint: $av-bg 0%; }
    #agentview-body > Collapsible.tool-block { background: $av-bg; border: none; padding: 0; margin: 0 0 1 0; }
    #agentview-body > Collapsible.tool-block > CollapsibleTitle { background: $av-bg; color: $av-text-muted; padding: 0; }
    #agentview-body > Collapsible.tool-block > Contents { background: $av-bg; padding: 0 0 1 2; }
    #agentview-body > Collapsible.tool-block > Contents.-collapsed { padding: 0; display: none; height: 0; }
    #agentview-body > Collapsible.tool-block:hover { background: $av-panel; }
    #agentview-body > Collapsible.tool-block:hover > CollapsibleTitle { background: $av-panel; color: $av-accent; }
    #agentview-body > Collapsible.tool-block:hover > Contents { background: $av-panel; }
    #agentview-body > Markdown { background: $av-bg; margin: 0 0 1 0; padding: 0; }
    #agentview-body Markdown { padding: 0; }
    #agentview-body Markdown MarkdownBlock { margin: 0; padding: 0; }
    /* Sub-agent transcript markdown mirrors the main #log theming so a drill-
       down reads identically (same warm palette, no cool fallback). */
    #agentview-body Markdown MarkdownFence {
        background: $av-panel; color: $av-code; margin: 1 0; padding: 1 2;
    }
    #agentview-body Markdown MarkdownBlock > .code_inline { background: $av-border-subtle; color: $av-accent; }
    #agentview-body Markdown MarkdownBullet { color: $av-text-dim; }
    #agentview-body Markdown MarkdownBlockQuote { background: $av-panel; border-left: outer $av-text-dim; }
    #agentview-body Markdown MarkdownHorizontalRule { border-bottom: solid $av-border; }
    #agentview-body Markdown MarkdownH1 { border: none; }
    #agentview-body Markdown MarkdownTable { background: $av-panel; }
    #agentview-body Markdown MarkdownTH { color: $av-text; text-style: bold; }
    #agentview-body Markdown MarkdownTD { color: $av-code; }
    #agentview-body > .agentview-meta { height: auto; color: $av-text-muted; margin: 0 0 1 0; }
    #agentview-body > .agentview-meta.-empty { display: none; height: 0; margin: 0; }
    #agentview-footer { height: auto; margin-top: 1; }
    #agentview-footer > Static { width: auto; margin: 0 2 0 0; color: $av-text-muted; }
    #agentview-footer > Static:hover { color: $av-accent; }
    #agentview-pos { color: $av-text-muted; }
    StatusBar { height: auto; padding: 0 2 0 3; background: $av-bg; color: $av-text-muted; }
    /* Input region (MiMo session prompt): a left accent bar that is ALWAYS
       shown (independent of focus, so a click elsewhere never makes it vanish)
       + a grey input surface. The frame holds the text area and, below it, a
       muted meta line; a one-row tail sits just outside/below the frame. */
    #input-wrap {
        height: auto; margin: 0 1 0 3;
        background: $av-element;
        border-left: heavy $av-accent; padding: 1 2 0 2;
    }
    /* The left bar recolours to match the active interaction-mode badge
       (docs/guides/using-cli-and-tui.md §3.10), so the prompt's vertical line reads the same
       mode as the badge on the meta row: normal=amber, plan=green,
       worktree=lavender. The app toggles exactly one ``-mode-*`` class on
       every mode change (see _apply_input_mode_class). */
    #input-wrap.-mode-normal { border-left: heavy $av-warning; }
    #input-wrap.-mode-plan { border-left: heavy $av-success; }
    #input-wrap.-mode-worktree { border-left: heavy $av-mode-worktree; }
    /* When focus moves into the session rail, the accent "jumps" to the rail's
       cursor row, so the input's left bar dims (§6.1 — only one accent at a
       time). Defined last so it wins the specificity tie over -mode-*. */
    #input-wrap.-dim,
    #input-wrap.-dim.-mode-normal,
    #input-wrap.-dim.-mode-plan,
    #input-wrap.-dim.-mode-worktree { border-left: heavy $av-text-dim; }
    /* While the in-place AgentView is up there is nothing to type into, so the
       input frame + its spacer collapse and the StatusBar footer is repurposed
       to show the OPEN sub-agent's live status (spinner / phase / elapsed). */
    #input-wrap.-hidden { display: none; }
    CommandInput {
        height: 1; min-height: 1; max-height: 6;
        background: $av-element; border: none; padding: 0;
        scrollbar-size-vertical: 1;
    }
    CommandInput:focus { border: none; }
    /* The meta line sits on the 4th grey row: a one-row top pad separates it
       from the text area, so the grey frame reads as input + a footer line. */
    #input-meta { height: 2; padding: 1 0 0 0; background: $av-element; color: $av-text-muted; }
    /* The status bar is pinned to the very bottom of the screen, with a single
       blank row above it separating it from the input frame so the prompt
       breathes: input frame → blank → status. */
    #input-spacer { height: 1; background: $av-bg; }
    #input-spacer.-hidden { display: none; }
    /* Queued-message panel: a grey-barred block pinned just above the input
       frame. Grey accent (not the input's amber) marks it as "pending / not
       yet run" while echoing the input frame's left-bar rhythm. */
    #queued-panel {
        height: auto; margin: 0 1 0 3;
        background: $av-panel; color: $av-text-muted;
        border-left: solid $av-border; padding: 0 2;
    }
    #queued-panel.-hidden { display: none; }
    Sidebar {
        width: 42; height: 100%;
        background: $av-panel; color: $av-text-muted;
        padding: 1 2; border-left: solid $av-border-subtle;
    }
    Sidebar.-hidden { display: none; }
    /* ── Session Rail (left navigator, docs/architecture/frontend-adapters.md §4.1/§6) ──────────── */
    #session-rail {
        width: 34; height: 100%;
        background: $av-panel; color: $av-text-muted;
        padding: 1 1; border-right: solid $av-border-subtle;
    }
    #session-rail.-hidden { display: none; }
    #session-rail > .rail-title { color: $av-text; text-style: bold; padding: 0 1; }
    #session-rail:focus-within > .rail-title { color: $av-accent; }
    #session-rail > .rail-new { color: $av-text-muted; padding: 0 1; }
    #session-rail > #rail-rule { color: $av-border; }
    #rail-list {
        height: 1fr; scrollbar-size-vertical: 1;
        scrollbar-color: $av-text-dim;
        scrollbar-color-hover: $av-text-muted;
        scrollbar-color-active: $av-text-muted;
        scrollbar-background: $av-panel;
    }
    /* Keep the rail compact: group headers start at col 2 and session rows only
       one column deeper. The title and metadata then share one clean left axis. */
    #rail-list > SessionRow { padding: 0 1 0 2; }
    /* Every group header floats one blank line clear of what precedes it — the
       top rule for the first header, the previous group's last row for the rest. */
    #rail-list > GroupHeader { color: $av-text; padding: 0 1 0 2; margin-top: 1; }
    #session-rail:focus-within #rail-list > GroupHeader { color: $av-accent; }
    /* Blank line under a group header, before its first session row. */
    #rail-list > SessionRow.-group-first { margin-top: 1; }
    /* ① current session — persistent, focus-independent ("I am here"). A gray
       left bar marks it even while focus sits in the input area (startup): the
       accent bar means "keyboard is here", this dim bar means "this is current".
       padding-left 1 (bar col 0 + 1) keeps content at col 2 → no shift vs peers.
       When the rail is focused and the cursor lands on this same row, the higher-
       specificity :focus-within .-cursor rule below repaints the bar to accent. */
    #session-rail SessionRow.-current {
        border-left: heavy $av-text-dim; padding: 0 1 0 1;
        background: $av-element;
    }
    /* ② keyboard cursor — only while the rail has focus (auto via :focus-within),
       reusing the user-echo left accent bar vocabulary (§6.1). */
    /* Cursor rows keep their content column (no shift): bar(col 0) + padding-left
       lands content where the non-cursor row already had it. Group → col 2,
       session → col 2, while the row's own glyph lead preserves hierarchy. */
    #session-rail:focus-within GroupHeader.-cursor {
        border-left: heavy $av-accent; padding: 0 1 0 1;
        background: $av-element; text-style: bold;
    }
    #session-rail:focus-within SessionRow.-cursor {
        border-left: heavy $av-accent; padding: 0 1 0 1;
        background: $av-element; text-style: bold;
    }
    /* ③ hover — orthogonal to the keyboard cursor. */
    #session-rail SessionRow:hover,
    #session-rail GroupHeader:hover,
    #session-rail NewSessionRow:hover { background: $av-element; }
    /* Dashboard + agents share a scroll region (1fr) so a tall session scrolls
       instead of clipping; the brand line stays pinned to the bottom. The agents
       panel expands to fill the region; each agent row is a clickable widget
       (amber on hover, MiMo parity). */
    #sidebar-scroll { height: 1fr; scrollbar-size-vertical: 1; }
    #sidebar-dash { height: auto; }
    #sidebar-agents { height: auto; margin-top: 1; }
    #sidebar-agents > AgentRow:hover { color: $av-accent; }
    #sidebar-brand { height: auto; }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True, show=False),
        Binding("ctrl+d", "quit_after_turn", "Quit", show=False),
        Binding("ctrl+g", "toggle_sidebar", "Sidebar", show=False),
        # Focus the left session rail (docs/architecture/frontend-adapters.md §7.1): smart three-state
        # toggle (hidden→show+focus / shown→focus / focused→hide+back to input).
        # ctrl+s is safe in Textual's raw mode (IXON off, no XOFF freeze), avoids
        # tmux/screen prefixes (ctrl+b / ctrl+a), and works without switching out
        # of a Chinese IME.
        Binding("ctrl+s", "focus_rail", "Sessions", show=False),
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+r", "view_subagents", "Subagents", show=False),
        # Cancel a queued message. priority=True so the focused input box does
        # not swallow it; gated by check_action to only fire when the queue is
        # non-empty (otherwise the key passes through to normal text editing).
        Binding("ctrl+x", "cancel_queued", "Cancel queued", priority=True, show=False),
        # Pull the most recently queued message back into the input box to edit
        # it. priority=True
        # so the focused input box does not swallow it; check_action only enables
        # it when the queue is non-empty AND the input box is empty, so it never
        # clobbers an in-progress draft and otherwise falls through to editing.
        Binding("ctrl+e", "edit_last_queued", "Edit queued", priority=True, show=False),
        # Active only while the in-place AgentView is shown (see check_action,
        # which returns False otherwise → the key falls through to normal use).
        # priority=True so the focused input box does not swallow Esc / the arrow
        # keys (left/right would otherwise just move the text cursor) — same
        # pattern as ctrl+x above.
        Binding("escape", "agent_main", "Main", show=False, priority=True),
        Binding("left", "agent_prev", "Prev", show=False, priority=True),
        Binding("right", "agent_next", "Next", show=False, priority=True),
        # Cycle the normal/plan/worktree interaction mode (docs/guides/using-cli-and-tui.md §3.10).
        # Tab cycles forward (normal→plan→worktree→normal); Shift+Tab cycles back.
        # priority=True so the focused input box does not swallow Tab; check_action
        # only intercepts when the input is empty, otherwise Tab falls through to
        # TextArea indentation.
        Binding("tab", "cycle_mode('forward')", "Cycle mode", show=False, priority=True),
        Binding(
            "shift+tab", "cycle_mode('backward')", "Cycle mode", show=False, priority=True
        ),
    ]

    # Sidebar auto-shows on terminals wider than this (MiMo uses 120).
    _WIDE_COLS = 120

    def __init__(
        self,
        engine_factory: EngineFactory,
        app_ctx: object,
        registry: "CommandRegistry",
        theme: Theme | None = None,
        *,
        cwd: str = "",
        model: str = "",
        context_window: int = 0,
        version: str = "0.1.0",
        presentation: str = "fullscreen",
        mounted: "_Settable | None" = None,
        session_query: "SessionListQuery | None" = None,
        switch_engine: "EngineSwitcher | None" = None,
    ) -> None:
        # Set BEFORE super().__init__(): Textual parses ``App.CSS`` during base
        # init, which calls ``get_theme_variable_defaults`` -> ``_palette_vars``
        # -> ``self._av_theme``. If the theme weren't set yet the $av-* vars
        # would be unresolved and the CSS would fail to parse on startup.
        self._av_theme = theme or DEFAULT_THEME
        super().__init__()
        self._engine_factory = engine_factory
        self._engine: "EngineHandle | None" = None
        self._app_ctx = app_ctx
        self._av_registry = registry
        self._queue = MessageQueue()
        # Per-turn routing (agent, model, subtask) for prompt-template commands,
        # keyed by the queued prompt text and consumed when that turn runs
        # (docs/reference/commands.md §4.4). Only ``model`` is honoured per-turn (via the
        # public switch_model facade); agent/subtask need engine support the
        # EngineHandle does not expose and are surfaced as a notice, not dropped.
        self._turn_routing: dict[str, tuple[str | None, str | None, bool]] = {}
        self._mode_stack = InputModeStack()
        # Inline choice menu (AskUserQuestion / permission) state. The menu widget
        # itself holds no async machinery; the app owns the result Future and
        # resolves it from forwarded key handlers. ``_choice_awaiting_custom`` is
        # True while the user types a free-text "Other" answer into the input box.
        self._choice_future: "asyncio.Future[list[str] | None] | None" = None
        self._choice_awaiting_custom = False
        self._choice_pending_selected: list[str] = []
        self._interrupt: InterruptHandler | None = None
        self._turn_started_at = 0.0
        self._turn_paused_elapsed = 0.0
        self._turn_pause_started_at = 0.0
        # Per-phase timer: reset whenever the StatusBar phase actually changes
        # (thinking → generating → tool → …). The footer already shows the WHOLE
        # turn's elapsed, so the bottom status line shows the finer-grained "how
        # long has the CURRENT operation been running" instead (no duplication).
        self._phase_started_at = 0.0
        self._phase_paused_elapsed = 0.0
        self._phase_pause_started_at = 0.0
        # The same idea for a drilled-into sub-agent: the AgentView title + inline
        # footer already show the sub-agent's WHOLE elapsed, so the bottom status
        # line times only the CURRENT activity. Reset when the open sub-agent or
        # its activity changes; keyed on (job_id, activity) to detect that.
        self._agent_phase_key: tuple[str, str] | None = None
        self._agent_phase_started_at = 0.0
        # Per-turn usage snapshot (taken at turn start) so the footer renders a
        # strictly per-turn ↑↓/cache/cost view instead of the previous turn's
        # stale figures. See _begin_turn_footer / _compose_turn_footer.
        self._turn_usage_seq0 = 0
        self._turn_cost0 = 0.0
        self._turn_cache_read0 = 0
        self._turn_cache_write0 = 0
        self._turn_prompt0 = 0
        # Mode badge snapshot for the in-flight turn's footer (frozen at turn
        # start so a mid-turn Tab never rewrites the reply's recorded mode).
        self._turn_mode_badge: tuple[str, str] | None = None

        self._spinner_i = 0
        # Monotonic tick counter. The spinner runs at the full 10 Hz; the heavier
        # live refreshes (turn footer, sub-agent live cards, sidebar) are gated to
        # every Nth tick (``_HEAVY_TICK_EVERY``) — 5 Hz is visually identical for
        # elapsed text but halves the per-tick render/relayout work.
        self._tick_n = 0
        # Last turn-footer text (``.plain``) pushed to the log, so a tick whose
        # footer is unchanged skips the widget update entirely.
        self._footer_line = ""
        # Transient status-bar feedback for a long-running slash command (e.g.
        # /compact): set the moment dispatch starts so the footer shows a live
        # spinner instead of an apparent freeze, cleared when the handler ends.
        self._cmd_busy_label: str | None = None
        self._cmd_busy_since = 0.0
        self._render_ctx: RenderContext | None = None
        self._sink: TuiEventSink | None = None
        self._engine_ready = False
        # Sidebar facts + visibility ("auto" = follow width, else explicit).
        self._cwd = cwd
        self._model = model
        # Memoised alias → full-name resolution (see _full_model_name); avoids a
        # per-tick registry import. ``_full_model_src`` is the raw name the cache
        # was computed for (None ⇒ not yet resolved).
        self._full_model_src: str | None = None
        self._full_model_cache: str = ""
        # Construction-time window (startup model). The sidebar recomputes the
        # live window from the active model after a /model switch — these two
        # fields memoise that lookup (keyed on the resolved id) so the refresh
        # tick does not re-import the registry every frame. See
        # ``_current_context_window``.
        self._context_window = context_window
        self._ctx_win_src: str | None = None
        self._ctx_win_cache: int = 0
        # Resolved full id of the startup model, memoised lazily. Gates the
        # construction-time ``_context_window`` fallback so it is only ever
        # returned for the startup model, never a model switched into later.
        self._startup_model_full_cache: str | None = None
        self._version = version
        self._session_id = ""
        # Presentation form (docs/guides/using-cli-and-tui.md §7.2): "fullscreen" = alt-screen,
        # "inline" = same app via Textual's inline driver. Inline force-hides the
        # sidebar (it is noise in the more constrained contexts inline targets;
        # key facts already live in the per-turn footer — same trade-off as line).
        self._presentation = presentation
        self._sidebar_mode: str = "hidden" if presentation == "inline" else "auto"
        # Optional mounted-signal (a threading.Event-like with ``.set()``): the
        # CLI's runtime fallback net (docs/guides/using-cli-and-tui.md §8.2) uses it to tell a
        # *startup* failure (never mounted → fall to next tier) from a *session*
        # crash (already mounted → propagate, no silent re-tier).
        self._av_mounted = mounted
        # In-place sub-agent context view (MiMo route-driven parity): True while
        # #log is hidden and #agentview shows a selected sub-agent's transcript.
        self._agent_view_active = False
        # ── Session rail (docs/architecture/frontend-adapters.md) ────────────────────────────────
        # Read-only listing Port + switch factory (both CLI-injected, may be None
        # for one-shot / no-persistence runs → the rail stays hidden, §4.4/§8).
        self._session_query = session_query
        self._switch_engine = switch_engine
        # Rail visibility: "hidden" by default (collapsed), "auto" follows width,
        # "shown" displays it. Inline always force-hides (§4.1).
        self._rail_mode: str = "hidden"
        # Monotone generation token guarding overlapping switches (§5.2.1): only
        # the last-initiated switch is allowed to land.
        self._switch_gen = 0
        self._switching = False
        # UI preference: collapsed cwd groups (§6.3.4), keyed by group_key.
        # `_collapsed_groups` is the effective collapse set (user toggles live here);
        # `_seen_groups` records which groups we've already applied the "default
        # collapse non-current" rule to, so a manual expand isn't undone next refresh.
        self._collapsed_groups: frozenset[str] = frozenset()
        self._seen_groups: set[str] = set()
        # The current session's group, as of the last refresh. Switching into a
        # new group auto-expands it once; staying put lets a manual collapse stick.
        self._last_cur_group: str = ""
        # session_id → raw title, cached from the last rail refresh (for toast /
        # rename-initial / delete-confirm display; the rail itself is the truth).
        self._rail_titles: dict[str, str] = {}
        # ── Background sessions (docs/architecture/frontend-adapters.md) ─────────────────────────
        # In-process pool of live engines keyed by session id (foreground +
        # detached backgrounds). The foreground session is also in here; its
        # redirect.target is the live #log sink, the rest are detached (None).
        self._live: dict[str, _LiveSession] = {}
        # Sessions with a turn currently driving their engine (foreground OR
        # background) — the single source of truth for "busy", the rail's live
        # ``running`` overlay, and the foreground RUNNING band (_sync_fg_running).
        self._running_sessions: set[str] = set()
        # Sessions whose backgrounded turn is parked on an approval/question
        # relay (rail ``awaiting_input`` overlay until switched in, §5.6.3).
        self._awaiting_sessions: set[str] = set()
        # session_id → gate set when that session becomes foreground, so a
        # backgrounded approval/question can wait for switch-in (the "接力").
        self._fg_gates: dict[str, "asyncio.Event"] = {}

    def _require_app_ctx(self) -> "AppContext":
        return cast("AppContext", self._app_ctx)

    # ── composition / mount ────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal():
            yield SessionRail(id="session-rail", theme=self._av_theme)
            with Vertical(id="main"):
                yield ConversationLog(id="log")
                yield AgentView(theme=self._av_theme, id="agentview", classes="-hidden")
                # Pending-message panel: pinned just above the input frame so
                # queued submissions stay visible while the engine runs.
                yield QueuedPanel(id="queued-panel", classes="-hidden")
                yield CompletionMenu(
                    tuple(self._av_registry.completion_items()),
                    theme=self._av_theme,
                    id="completion",
                )
                # Inline choice menu (AskUserQuestion / permission approval):
                # docked above the input like the slash menu, driven by forwarded
                # keys, resolved via an asyncio.Future the app owns (gap12/B1).
                yield InlineChoiceMenu(theme=self._av_theme, id="choice")
                # Input frame (MiMo prompt parity): the text area + a muted meta
                # line live inside a left-accent-barred grey frame; the status
                # bar sits directly below it as the footer.
                with InputFrame(id="input-wrap"):
                    yield CommandInput(
                        id="input",
                        completion_names=tuple(self._av_registry.completion_names()),
                    )
                    yield PromptMeta(id="input-meta")
                # A blank row between the input frame and the bottom status bar
                # so the prompt breathes: input frame → blank → status.
                yield PromptTail(id="input-spacer")
                yield StatusBar(id="status")
                yield ToastHost(id="toast-host")
            yield Sidebar(id="sidebar", theme=self._av_theme)

    def _palette_vars(self) -> dict[str, str]:
        """Build the Textual theme ``variables`` dict from our palette roles.

        Shared by :meth:`get_theme_variable_defaults` (so the App ``CSS`` —
        which references ``$av-*`` — parses on startup, BEFORE the theme is
        registered in ``on_mount``) and :meth:`_install_textual_theme` (the
        live theme, which can re-skin on ``Theme.load``). ``av-*`` are our own
        exact palette roles; using them instead of Textual's derived
        ``$panel``/``$primary`` avoids the colour shift Textual's generator
        applies (it rounds #e8b339 → #e7b339, makes ``$text-muted`` ``auto 60%``).
        """
        t = self._av_theme
        steps = t.steps

        def role(name: str, fallback: str) -> str:
            return t.role(name) or fallback

        accent = role("accent", "#e8b339")
        text = role("text", steps[-1])
        muted = role("text_muted", steps[-3])
        return {
            # Markdown headings: amber for the top levels, plain text below;
            # weight (not colour) carries the rest of the hierarchy.
            "markdown-h1-color": accent,
            "markdown-h1-background": "transparent",
            "markdown-h2-color": accent,
            "markdown-h2-background": "transparent",
            "markdown-h3-color": text,
            "markdown-h4-color": text,
            "markdown-h5-color": muted,
            "markdown-h6-color": muted,
            # Palette roles for the app CSS (see docstring).
            "av-bg": role("bg", steps[0]),
            "av-panel": role("bg_panel", steps[1]),
            "av-element": role("bg_element", steps[2]),
            "av-text": text,
            "av-text-muted": muted,
            "av-text-dim": role("text_dim", steps[-4]),
            "av-code": steps[10],
            "av-accent": accent,
            "av-warning": role("warning", "#e0a458"),
            # Mode-badge palette (docs/guides/using-cli-and-tui.md §3.10): the input frame's left
            # bar + the per-turn footer recolour to match the active interaction
            # mode badge — normal=warning (amber), plan=success (green),
            # worktree=mode_worktree (lavender).
            "av-success": role("success", "#74c09a"),
            "av-mode-worktree": role("mode_worktree", "#b5a4cc"),
            "av-tool-err": role("tool_err", "#d16b6b"),
            "av-border": role("border", steps[5]),
            "av-border-subtle": role("border_subtle", steps[3]),
        }

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Provide ``$av-*`` defaults so the App CSS parses before the theme is
        registered (Textual calls this during the startup CSS parse)."""
        return self._palette_vars()

    def _install_textual_theme(self) -> None:
        """Register + activate a Textual theme derived from our palette (P1/P2).

        Textual's built-in default theme drives the variables that style the
        native ``Markdown`` widget (``$markdown-h1-color`` …) and the dark
        syntax-highlight used inside fenced code blocks — both resolve to a
        cool blue/cyan scheme that clashes with our warm-charcoal/amber theme
        (taste-skill: one palette, no AI-blue). Mapping our semantic roles onto
        a Textual ``Theme`` makes the Markdown answer region — the highest-
        frequency surface — actually obey the theme, and keeps the fence syntax
        colours on the same warm scale as everything else. The TUI is dark-only
        (``dark=True`` fixed; the light knob was removed).
        """
        try:
            from textual.theme import Theme as TextualTheme
        except Exception:  # pragma: no cover - textual always provides this
            return
        t = self._av_theme
        steps = t.steps

        def role(name: str, fallback: str) -> str:
            return t.role(name) or fallback

        accent = role("accent", "#e8b339")
        theme = TextualTheme(
            name="autoverse",
            primary=accent,
            secondary=role("accent_soft", accent),
            accent=accent,
            warning=role("warning", "#e0a458"),
            error=role("error", "#d16b6b"),
            success=role("success", "#74c09a"),
            foreground=role("text", steps[-1]),
            background=role("bg", steps[0]),
            surface=role("bg_panel", steps[1]),
            panel=role("bg_element", steps[2]),
            dark=True,
            variables=self._palette_vars(),
        )
        try:
            self.register_theme(theme)
            self.theme = "autoverse"
        except Exception:  # pragma: no cover - defensive: never break mount
            pass

    def on_mount(self) -> None:
        self._install_textual_theme()
        log = self.query_one("#log", ConversationLog)
        self._render_ctx = RenderContext(
            theme=self._av_theme, ansi=True, width=max(40, self._main_width()),
        )
        self._sink = TuiEventSink(log, self._render_ctx)
        self._write_welcome(log)
        self.query_one("#input", CommandInput).focus()
        self._apply_sidebar_visibility()
        self._refresh_sidebar()
        self._apply_rail_visibility()
        self._refresh_session_rail()
        # Idle status context: model + cwd basename (handy on narrow terminals
        # where the sidebar is hidden).
        self._refresh_status_context()
        self.set_interval(1 / 10, self._tick)
        # Signal the CLI fallback net that this tier has mounted successfully
        # (so a later crash is treated as a session error, not a startup one).
        if self._av_mounted is not None:
            try:
                self._av_mounted.set()
            except Exception:  # pragma: no cover - defensive: never break mount
                pass
        self._build_engine()

    @work(exclusive=False)
    async def _build_engine(self) -> None:
        """Build the engine wired to the live RichLog sink, then enable input."""
        assert self._sink is not None
        # Wire the engine through a re-targetable RedirectSink (§5.6.3) instead of
        # the bare TuiEventSink, so a later switch can detach this session's turn
        # to the background without it painting the reused #log.
        redirect = RedirectSink(target=self._sink)
        self._engine = await self._engine_factory(redirect)
        self._interrupt = InterruptHandler(self._engine)
        # Capture the live session id for the sidebar header (the engine resolves
        # it at build time — a fresh uuid for a new session, or the resumed id).
        self._session_id = str(getattr(self._engine, "session_id", "") or "")
        # Register the foreground session in the live-engine pool.
        self._live[self._session_id] = _LiveSession(
            engine=self._engine, redirect=redirect, interrupt=self._interrupt,
            last_used=time.monotonic(),
        )
        # Adopt the engine's DI container so slash commands (/trace /secrets
        # /mcp …) reach the live subsystems (A12). The factory builds the engine
        # after the app mounts, so app_ctx can only be wired here.
        if self._app_ctx is None:
            self._app_ctx = getattr(self._engine, "app", None)
        # Wire the interactive AskUserQuestion UI (gap12/B1), session-scoped so a
        # backgrounded session's question relays correctly (§5.6.3).
        self._bind_ask_user(self._engine, self._session_id)
        # Register user-invocable skills as prompt-template commands (G6,
        # docs/reference/commands.md §4.5). The SkillRegistry only exists after the engine
        # assembles its extension sources, so this is the earliest wiring point.
        self._register_skill_commands()
        self._engine_ready = True
        # Now that the session id is known, repaint the sidebar header.
        self._refresh_sidebar()
        # Mark the freshly-loaded session as viewed (clears its unseen bit, §5.6.5)
        # and repaint the rail with the live current_id highlight.
        self._mark_viewed(self._session_id)
        self._refresh_session_rail()

    # ── background-session helpers (docs/architecture/frontend-adapters.md §5.6.3) ───────────────
    def _bind_ask_user(self, engine: "EngineHandle", sid: str) -> None:
        """Point the engine's ``ask_user`` hook at a session-scoped wrapper.

        The AskUserQuestion tool reads ``app.extra["ask_user"]``; binding the
        session id lets :meth:`_ask_user` relay a backgrounded session's question
        instead of hijacking the foreground inline menu (§5.6.3)."""
        app_ctx = getattr(engine, "app", None)
        if app_ctx is not None and hasattr(app_ctx, "extra"):
            app_ctx.extra["ask_user"] = (
                lambda payload, _sid=sid: self._ask_user(payload, session_id=_sid)
            )

    def _sync_fg_running(self) -> None:
        """Keep the foreground RUNNING band in step with the current session.

        RUNNING is on while the *current* session has a live turn (foreground or
        a switched-into background one), off otherwise — so input gating, the
        status line and the footer reflect the session you're actually looking at
        rather than the worker that happens to be draining (§5.6.3)."""
        if self._session_id in self._running_sessions:
            self._mode_stack.enter(InputMode.RUNNING)
        else:
            self._mode_stack.leave(InputMode.RUNNING)

    async def _gate_foreground(self, sid: str) -> None:
        """Block an approval/question until *sid* is the foreground session.

        A backgrounded turn that needs the user (approval / AskUserQuestion) can't
        seize the single inline menu, so it parks here marked ``awaiting_input``
        until the user switches in (§5.6.3 "接力"). Returns immediately for the
        foreground session (the common path)."""
        if not sid or sid == self._session_id:
            return
        self._awaiting_sessions.add(sid)
        self._refresh_session_rail()
        gate = asyncio.Event()
        self._fg_gates[sid] = gate
        # Re-check after registering: a switch may have landed in between (no
        # preemption between the sync lines above, but the rail refresh yields).
        if sid == self._session_id:
            self._awaiting_sessions.discard(sid)
            return
        await gate.wait()
        self._awaiting_sessions.discard(sid)

    def _resume_pending_relay(self, sid: str) -> None:
        """Release a parked approval/question for the now-foreground *sid*."""
        self._awaiting_sessions.discard(sid)
        gate = self._fg_gates.pop(sid, None)
        if gate is not None and not gate.is_set():
            gate.set()

    def _evict_idle_sessions(self) -> None:
        """Cap the live-engine pool, retiring the oldest *idle* background engine.

        Foreground, running, and relay-parked sessions are never evicted; only an
        idle backgrounded engine past :data:`_MAX_LIVE_SESSIONS` is ``aclose``d
        (flush + release), demoting it to a cold on-disk session (§5.6.3)."""
        while len(self._live) > _MAX_LIVE_SESSIONS:
            victims = [
                sid for sid in self._live
                if sid != self._session_id
                and sid not in self._running_sessions
                and sid not in self._awaiting_sessions
            ]
            if not victims:
                return
            victim = min(victims, key=lambda s: self._live[s].last_used)
            sess = self._live.pop(victim)
            self._evict_engine(sess.engine)

    @work(exclusive=False, group="evict")
    async def _evict_engine(self, engine: "EngineHandle") -> None:
        try:
            await engine.aclose("evict")
        except Exception:  # noqa: BLE001 — flush failure is non-fatal
            pass

    def _register_skill_commands(self) -> None:
        sources = getattr(self._app_ctx, "extension_sources", ())
        skills = next((s for s in sources if getattr(s, "name", None) == "skills"), None)
        if skills is None:
            return
        try:
            from autoverse.frontends.repl.builtin_cmds import (
                register_skill_template_commands,
            )

            register_skill_template_commands(self._av_registry, skills)
            # Refresh completion items so newly-registered skills autocomplete.
            self.query_one("#completion", CompletionMenu).set_items(
                tuple(self._av_registry.completion_items())
            )
        except Exception:  # pragma: no cover - a bad skill must not block startup
            pass

    # ── inline slash completion (§8 I3) ───────────────────────────────────────
    def on_command_input_content_changed(
        self, event: CommandInput.ContentChanged
    ) -> None:
        if event.input.id != "input":
            return
        # While the user is typing a free-text "Other" answer, the input is a
        # plain answer box — never surface slash completion (even if they type a
        # leading "/").
        if self._choice_awaiting_custom:
            self._clear_log_overlay_space()
            return
        menu = self.query_one("#completion", CompletionMenu)
        # Reposition only when it shows: the menu floats just above the input
        # frame, which grows 1→6 lines as the user types (see MiMo's anchor-
        # relative ``top = anchor.y - height``).
        if menu.update_for(event.value):
            menu.set_bottom_offset(self._completion_bottom_offset())
            self._reserve_log_overlay_space(10)
        else:
            self._clear_log_overlay_space()

    def _completion_bottom_offset(self) -> int:
        """Rows between the screen bottom and the top of the input frame, so the
        bottom-docked completion menu floats flush above the input.

        Bottom region (bottom→top): ``#status`` (1) + ``#input-spacer`` (1) +
        ``#input-wrap`` (pad-top 1 + ``CommandInput`` rows + ``#input-meta`` 2).
        ``CommandInput`` grows 1→6 lines, so reading its current line count
        keeps the menu glued above it as it resizes.
        """
        try:
            inp = self.query_one("#input", CommandInput)
            rows = max(1, min(6, inp.document.line_count))
        except Exception:  # pragma: no cover - defensive: input not mounted yet
            rows = 1
        return rows + 5

    def _reserve_log_overlay_space(self, rows: int) -> None:
        """Keep the latest log block above bottom-docked input overlays."""
        try:
            self.query_one("#log", ConversationLog).reserve_bottom_space(rows)
        except Exception:  # pragma: no cover - defensive: log not mounted
            pass

    def _clear_log_overlay_space(self) -> None:
        try:
            self.query_one("#log", ConversationLog).clear_bottom_space()
        except Exception:  # pragma: no cover - defensive: log not mounted
            pass

    def on_key(self, event: object) -> None:
        """Forward navigation keys to the completion menu while it is visible."""
        menu = self.query_one("#completion", CompletionMenu)
        if not menu.visible_now:
            return
        key = getattr(event, "key", None)
        stop = getattr(event, "stop", None)
        if key == "down":
            menu.move_down()
            if callable(stop):
                stop()
        elif key == "up":
            menu.move_up()
            if callable(stop):
                stop()
        elif key == "tab":
            # Tab completes the highlighted command into the box (does NOT run);
            # lets the user then add arguments before pressing Enter.
            name = menu.current_name()
            if name is not None:
                inp = self.query_one("#input", CommandInput)
                inp.value = f"{name} "
                inp.cursor_position = len(inp.value)
                menu.hide()
                self._clear_log_overlay_space()
                if callable(stop):
                    stop()
        elif key == "escape":
            menu.hide()
            self._clear_log_overlay_space()
            if callable(stop):
                stop()
        # NB: Enter is intentionally NOT intercepted here — Textual's Input posts
        # ``Submitted`` for Enter before this bubbles up, so interception is
        # unreliable. Enter-on-visible-menu is handled in
        # ``on_command_input_entered`` (accept highlighted command + run).

    def _best_completion(self, raw: str) -> str | None:
        """Best fuzzy command-name match for ``/word`` (e.g. ``/mode`` → ``/model``)."""
        from autoverse.frontends.repl.dialogs.fuzzy import fuzzy_filter

        query = raw[1:]
        names = [n for n, _s, _h in self._av_registry.completion_items()]
        matches = fuzzy_filter(query, names, key=lambda n: n.lstrip("/"))
        return matches[0] if matches else None

    # ── input handling ──────────────────────────────────────────────────────
    def on_command_input_entered(self, event: CommandInput.Entered) -> None:
        # Free-text answer for an inline-choice "Other" row takes precedence over
        # normal command/message handling (gap12/B1): consume it and resolve the
        # pending question instead of submitting it as a turn.
        if self._choice_awaiting_custom:
            self._submit_choice_custom(event.value)
            return
        raw = event.value.strip()
        if not raw:
            return
        # Enter while typing a command word (`/word`, no space, not an exact
        # command) accepts the best fuzzy completion and runs it. We recompute
        # from ``raw`` rather than reading the menu's highlight, because clearing
        # the input box fires Input.Changed → hides the menu *before* this
        # handler runs (the highlight is gone by now).
        menu = self.query_one("#completion", CompletionMenu)
        menu.hide()
        self._clear_log_overlay_space()
        if raw.startswith("/") and " " not in raw and self._av_registry.get(raw[1:]) is None:
            picked = self._best_completion(raw)
            if picked is not None:
                raw = picked
        if not self._engine_ready:
            self._log_line("[dim]starting engine…[/dim]")
            self._enqueue(raw, priority="next")
            self._refresh_queue_panel()
            return
        if raw.startswith("/"):
            word = raw.lstrip("/").split(" ", 1)[0]
            spec = self._av_registry.get(word)
            running = self._mode_stack.current() is InputMode.RUNNING
            if running and (spec is None or not spec.run_while_busy):
                self._enqueue(raw, priority="next")
                self._refresh_queue_panel()
                return
            self._run_command(raw)
            return
        if self._mode_stack.current() is InputMode.RUNNING:
            self._enqueue(raw, priority="next")
            self._refresh_queue_panel()
        else:
            self._enqueue(raw, priority="now")
            self._refresh_queue_panel()
            self._drain_next()

    @work(exclusive=True)
    async def _run_command(self, raw: str) -> None:
        # Long-running handlers (e.g. /compact, which awaits an LLM summary) would
        # otherwise leave the UI looking frozen between Enter and the handler's
        # own output. Surface an immediate, transient status-bar spinner from the
        # command's ``busy_label`` so the keypress is acknowledged at once; it is
        # cleared in ``finally`` and never written to the scrollback (the handler
        # still owns any persistent record, e.g. the compaction divider).
        word = raw.lstrip("/").split(" ", 1)[0]
        spec = self._av_registry.get(word)
        busy = spec.busy_label if spec is not None else None
        self._log_command_start(spec)
        if busy:
            self._cmd_busy_label = busy
            self._cmd_busy_since = time.monotonic()
            self._set_phase(busy)
        try:
            result = await dispatch(raw, self._command_ctx(), self._av_registry)
        finally:
            if busy:
                self._cmd_busy_label = None
                self._set_phase("idle")
        if result.should_clear:
            self._clear_log()
        self._log_command_result(result)
        # A command may have changed live state (e.g. /model); re-sync the
        # model shown in the status line + sidebar.
        self._refresh_status_context()
        self._refresh_sidebar()
        if result.prompt is not None:
            # Prompt-template command: submit the expanded prompt as a turn.
            self._record_routing(result)
            self._enqueue(result.prompt, priority="now")
            self._drain_next()
        if result.should_quit:
            self.exit(0)

    def _record_routing(self, result: CommandResult) -> None:
        """Remember a prompt-template command's per-turn routing so the turn
        runner can apply it (docs/reference/commands.md §4.4)."""
        if result.prompt is None:
            return
        if result.model or result.agent or result.subtask:
            self._turn_routing[result.prompt] = (
                result.agent, result.model, result.subtask
            )

    def _apply_turn_routing(
        self, routing: "tuple[str | None, str | None, bool] | None"
    ) -> str | None:
        """Apply per-turn routing before a prompt-template turn; return the model
        to restore afterwards (or None).

        ``model`` is honoured per-turn via the public ``switch_model`` facade and
        restored after the turn. ``agent`` / ``subtask`` need an engine capability
        the EngineHandle does not expose (§3.3 red line), so they are surfaced as
        a one-line notice instead of being silently ignored.
        """
        if routing is None or self._engine is None:
            return None
        agent, model, subtask = routing
        prev_model: str | None = None
        if model:
            prev_model = self._current_model()
            try:
                self._engine.switch_model(model)
            except Exception:  # pragma: no cover - defensive
                prev_model = None
            self._refresh_status_context()
        if agent or subtask:
            self.toast(
                "agent/subtask routing isn't honoured for inline commands yet — "
                "ran on the current agent.",
                variant="info",
            )
        return prev_model

    def _restore_turn_routing(self, prev_model: str | None) -> None:
        if prev_model and self._engine is not None:
            try:
                self._engine.switch_model(prev_model)
            except Exception:  # pragma: no cover - defensive
                pass
            self._refresh_status_context()

    @work(exclusive=True)
    async def _drain_next(self) -> None:
        """Consume the queue one turn at a time (exclusive = serial)."""
        while True:
            prompt = self._queue.dequeue()
            # The dequeued item leaves the panel now (it is about to run / echo);
            # an empty queue hides the panel entirely.
            self._refresh_queue_panel()
            if prompt is None:
                return
            if prompt.startswith("/"):
                word = prompt.lstrip("/").split(" ", 1)[0]
                self._log_command_start(self._av_registry.get(word))
                result = await dispatch(prompt, self._command_ctx(), self._av_registry)
                if result.should_clear:
                    self._clear_log()
                self._log_command_result(result)
                if result.prompt is not None:
                    # Prompt-template command: run its expanded prompt next.
                    self._record_routing(result)
                    self._enqueue(result.prompt, priority="now")
                    self._refresh_queue_panel()
                if result.should_quit:
                    self.exit(0)
                    return
                continue
            sid = self._session_id
            sess = self._live.get(sid)
            if sess is None or sess.engine is None or self._interrupt is None:
                self._enqueue(prompt, priority="now")
                self._refresh_queue_panel()
                return
            self._echo_user(prompt)
            sess.interrupt.reset()
            self._turn_started_at = time.monotonic()
            self._turn_paused_elapsed = 0.0
            self._turn_pause_started_at = 0.0
            self._phase_paused_elapsed = 0.0
            self._phase_pause_started_at = 0.0
            prev_model = self._apply_turn_routing(self._turn_routing.pop(prompt, None))
            # Launch the turn as a *detachable* task (§5.6.3): it keeps running if
            # the user switches away mid-turn. The drain awaits either the task
            # finishing OR a switch-away detaching it.
            sess.detached = asyncio.Event()
            task = asyncio.ensure_future(self._run_turn(sid, prompt, prev_model))
            sess.task = task
            self._running_sessions.add(sid)
            self._sync_fg_running()
            self._set_phase("generating")
            # Mount the live turn-footer immediately (▪ model), glued to the
            # bottom of the reply; the tick refreshes it as the turn runs.
            self._begin_turn_footer()
            # Paint the current session's rail row as running (§4.2.1 turn start
            # is a refresh trigger).
            self._refresh_session_rail()
            detach_wait = asyncio.ensure_future(sess.detached.wait())
            try:
                await asyncio.wait(
                    {task, detach_wait}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                if not detach_wait.done():
                    detach_wait.cancel()
            if not task.done():
                # Switched away mid-turn: the turn now runs in the background and
                # owns its own completion (_run_turn's finally). Stop draining
                # this (now background) session's worker; the new foreground
                # session's enqueue re-triggers a fresh drain.
                return
            # Foreground turn finished; loop to the next queued item.

    async def _run_turn(self, sid: str, prompt: str, prev_model: str | None) -> None:
        """Drive one turn for *sid*, foreground or background (§5.6.3).

        Foreground-only side effects (phase, footer, viewed-mark) are guarded on
        ``sid == self._session_id`` *at the moment they run*, so a turn that is
        switched away mid-flight finishes silently in the background and just
        repaints the rail (its terminal state is derived from the store stamp the
        engine writes on completion)."""
        sess = self._live[sid]
        engine = sess.engine
        stop_cause = ""
        saw_error = False
        try:
            async for event in engine.run(prompt):
                if event.type is EventType.TURN_END:
                    payload = getattr(event, "payload", None)
                    stop_cause = str(getattr(payload, "stop_cause", "") or "")
                elif event.type is EventType.ERROR:
                    saw_error = True
                if sid == self._session_id:
                    self._on_engine_event(event)
        except Exception as exc:  # noqa: BLE001 — a turn failure must not crash the app
            with log_context(session_id=sid, frontend="tui"):
                _LOG.exception("TUI turn failed")
            if sid == self._session_id and not saw_error:
                self._on_engine_event(
                    make_event(
                        ErrorPayload(
                            message=str(exc) or type(exc).__name__,
                            error_class=type(exc).__name__,
                            recoverable=False,
                            phase="frontend",
                        )
                    )
                )
        finally:
            self._running_sessions.discard(sid)
            self._awaiting_sessions.discard(sid)
            sess.task = None
            sess.last_used = time.monotonic()
            # Defensive: this runs as a bare task, so its completion must never
            # raise out (an unretrieved exception is detached after a switch).
            try:
                if sid == self._session_id:
                    # Foreground completion: restore routing, freeze the footer,
                    # drop the RUNNING band and re-mark viewed (we're looking).
                    self._restore_turn_routing(prev_model)
                    elapsed_ms = self._turn_elapsed_ms()
                    interrupted = sess.interrupt.count > 0
                    self._write_turn_footer(elapsed_ms, interrupted)
                    self._set_phase("idle")
                    self._sync_fg_running()
                    self._mark_viewed(sid)
                else:
                    # Background completion: the engine stamped last_activity_at →
                    # the rail derives completed_unseen / error_unseen on refresh.
                    self._sync_fg_running()
                self._refresh_session_rail()
                self._evict_idle_sessions()
                if sid == self._session_id and stop_cause == "PLAN_PROPOSED":
                    await self._handle_plan_proposed(engine)
            except Exception:  # noqa: BLE001 — completion housekeeping is best-effort
                pass

    def _on_engine_event(self, event: StreamEvent) -> None:
        # The engine was built with TuiEventSink as a downstream sink, so events
        # are already written to the RichLog. We only inspect them here to drive
        # the StatusBar phase (P2: rendering stays in the sink/renderers).
        if event.type is EventType.THINKING_DELTA:
            self._set_phase("thinking")
        elif event.type is EventType.TOOL_START:
            # A tool is now executing; the per-phase timer tracks its duration
            # while the activity line names which tool (↳ Read(…)).
            self._set_phase("tool")
        elif event.type in (EventType.TEXT_DELTA, EventType.REQUEST):
            self._set_phase("generating")

    # ── Ctrl+C / quit ────────────────────────────────────────────────────────
    def action_interrupt(self) -> None:
        if self._session_id in self._running_sessions and self._interrupt is not None:
            reason = self._interrupt.on_interrupt()
            self._log_line(f"[bold $av-accent]⚡ {reason} requested[/bold $av-accent]")
        else:
            self._log_line("[dim]idle — Ctrl+D to quit[/dim]")

    def action_quit_after_turn(self) -> None:
        self.exit(0)

    # ── periodic tick (spinner + elapsed) ─────────────────────────────────────
    def _tick(self) -> None:
        status = self._status_bar()
        if status is None:  # mount/teardown window — widget not in the tree
            return
        # Spinner runs every tick; heavier live refreshes only every Nth (5 Hz).
        self._tick_n += 1
        heavy = (self._tick_n % self._HEAVY_TICK_EVERY) == 0
        # In the in-place AgentView the StatusBar footer shows the OPEN
        # sub-agent's live status (spinner / phase / elapsed) instead of the
        # main agent's — and the transcript follows the agent live below it.
        if self._agent_view_active:
            self._refresh_agent_status(status)
            try:
                view = self.query_one("#agentview", AgentView)
                view.refresh_body()
                # Advance the title + inline-footer timers smoothly: for a RUNNING
                # subagent feed a live monotonic elapsed (rec.elapsed_ms only
                # advances at child-turn boundaries → a jumpy timer); a finished
                # one keeps its frozen value.
                live_elapsed: float | None = None
                rec = view.current
                if rec is not None and self._sink is not None:
                    live = self._sink.agent_live_state(rec.job_id)
                    if live is not None:
                        live_elapsed = live[3]  # elapsed_ms (smooth, monotonic)
                view.refresh_title_elapsed(live_elapsed)
                view.refresh_meta_elapsed(live_elapsed)
            except Exception:  # pragma: no cover - teardown race
                pass
            if self._sink is not None:
                self._sink.refresh_live_agents()
            return
        if self._cmd_busy_label is not None:
            # A long-running slash command is in flight (e.g. /compact). It does
            # not enter RUNNING mode and has no turn footer, so just animate the
            # status-bar spinner + elapsed to acknowledge the keypress live.
            self._spinner_i = (self._spinner_i + 1) % len(_SPINNER)
            status.frame = _SPINNER[self._spinner_i]
            status.elapsed = time.monotonic() - self._cmd_busy_since
            return
        if self._mode_stack.current() is InputMode.RUNNING:
            self._spinner_i = (self._spinner_i + 1) % len(_SPINNER)
            status.frame = _SPINNER[self._spinner_i]
            # Per-phase elapsed (this think/generate/tool step), NOT the whole
            # turn — the turn footer (▪ model · 12.3s · …) already owns the
            # total. _phase_started_at is reset on each real phase change.
            base = self._phase_started_at or self._turn_started_at
            status.elapsed = self._elapsed_seconds(
                base,
                self._phase_paused_elapsed,
                self._phase_pause_started_at,
            )
            # Show the latest transient activity (running tool / subagent) the
            # sink captured — this is the StatusBar "region ②" (not committed).
            status.activity = (
                f"↳ {self._sink.activity}"
                if self._sink is not None and self._sink.activity else ""
            )
            # Pulse any in-flight tool card's spinner every tick so it stays in
            # step with the StatusBar spinner (cheap — usually 0–1 cards).
            if self._sink is not None:
                self._sink.refresh_live_tools(_SPINNER[self._spinner_i])
            # Animate the running session's rail glyph in lockstep (in-place,
            # no Store re-list — §4.2.1 cost discipline).
            if self._rail_visible():
                try:
                    self.query_one("#session-rail", SessionRail).tick_spinner(
                        _SPINNER[self._spinner_i]
                    )
                except Exception:  # pragma: no cover - teardown race
                    pass
            # The heavier live refreshes — recomposing the turn footer and
            # re-rendering each running sub-agent's scrollback card — run at the
            # reduced cadence: 5 Hz is visually identical for elapsed text.
            if heavy:
                self._refresh_turn_footer()
                if self._sink is not None:
                    self._sink.refresh_live_agents()
        elif self._running_sessions and self._rail_visible():
            # Foreground is idle, but a detached background turn is still live
            # (§5.6.3 keep-alive). The StatusBar / turn footer are foreground-only
            # and stay quiet, yet that session's rail row is ``running`` and must
            # keep spinning — gating the spinner on the foreground RUNNING band
            # froze it the moment the current session went idle. Advance the
            # frame here too (exactly once per tick — this branch is mutually
            # exclusive with the RUNNING block) so background rows animate.
            self._spinner_i = (self._spinner_i + 1) % len(_SPINNER)
            try:
                self.query_one("#session-rail", SessionRail).tick_spinner(
                    _SPINNER[self._spinner_i]
                )
            except Exception:  # pragma: no cover - teardown race
                pass
        # The in-place sub-agent view path returns early above (it drives the
        # StatusBar + transcript itself), so here we are always on Main: keep the
        # sidebar's agent rows fresh (reduced cadence — the rows tick in whole
        # seconds and the dashboard is diffed, so this is cheap when idle).
        if heavy:
            self._refresh_sidebar()

    # ── in-place AgentView footer status ───────────────────────────────────
    def _refresh_agent_status(self, status: "StatusBar") -> None:
        """Feed the StatusBar from the OPEN sub-agent's live state.

        Mirrors the main-agent status line but sources spinner / phase /
        elapsed / activity from the sub-agent currently shown in the AgentView,
        so the footer reads like the main view's status bar — just for the agent
        you drilled into."""
        try:
            view = self.query_one("#agentview", AgentView)
        except Exception:  # pragma: no cover - teardown race
            return
        rec = view.current
        # Live activity/status/turn/elapsed for a still-running agent come from
        # the sink's active map; a finished agent falls back to the record's
        # frozen snapshot.
        live = (
            self._sink.agent_live_state(rec.job_id)
            if (rec is not None and self._sink is not None)
            else None
        )
        if live is not None:
            activity, sub_status, _turn, elapsed_ms = live
        elif rec is not None:
            activity, sub_status, _turn, elapsed_ms = "", rec.status, rec.turn, rec.elapsed_ms
        else:
            activity, sub_status, _turn, elapsed_ms = "", "", 0, 0.0
        # The bar is driven entirely by `agent_line` here so it stays exactly ONE
        # line in both states and never shows the main-agent ready/help/model hint
        # (irrelevant inside a subagent). Navigation already lives in the pinned,
        # clickable AgentView footer (Main/Prev/Next), so we don't repeat it.
        if sub_status != "running":
            # Done / failed / cancelled: a terse final-state line. Type / turns /
            # stats already live in the title + inline footer above.
            self._agent_phase_key = None  # so the next run restarts the op timer
            glyph = {"done": "✓", "failed": "✗", "cancelled": "⊘"}.get(sub_status, "•")
            # Title-case the terminal-state word to match the main agent's
            # readable phase labels (Thinking… / Responding… / Running tool…)
            # rendered by StatusBar — the subagent footer should read the same.
            raw_label = sub_status or "done"
            label = raw_label[0].upper() + raw_label[1:]
            elapsed = _fmt_elapsed(elapsed_ms)
            tail = f" · {elapsed}" if elapsed else ""
            status.agent_line = (
                f"[$av-text-muted]{glyph} {label}{tail}[/$av-text-muted]"
            )
            return
        # Running: a single live line — spinner + current activity + the CURRENT
        # operation's elapsed. The whole-agent elapsed already lives in the title
        # + inline footer, so here we time only the current activity: restart the
        # local monotonic timer whenever the open agent or its activity changes.
        phase_key = ((rec.job_id if rec is not None else ""), activity)
        if phase_key != self._agent_phase_key:
            self._agent_phase_key = phase_key
            self._agent_phase_started_at = time.monotonic()
        op_elapsed = time.monotonic() - self._agent_phase_started_at
        self._spinner_i = (self._spinner_i + 1) % len(_SPINNER)
        frame = _SPINNER[self._spinner_i]
        act = activity.replace("[", r"\[") if activity else ""
        act_md = f"[$av-text-muted]{act}[/$av-text-muted] " if act else ""
        status.agent_line = (
            f"[bold $av-accent]{frame}[/bold $av-accent] {act_md}"
            f"[$av-text-dim]· {op_elapsed:.1f}s[/$av-text-dim]"
        )

    # ── sidebar ──────────────────────────────────────────────────────────────
    def _main_width(self) -> int:
        """Width available to the main column (terminal minus the side panels)."""
        total = self.size.width or 80
        if self._sidebar_visible():
            total -= 42
        if self._rail_visible():
            total -= _RAIL_WIDTH
        return total

    def _sidebar_visible(self) -> bool:
        # MiMo parity: inside a sub-agent view (currentAgentID() != "main") the
        # sidebar is unconditionally hidden so the transcript fills the full
        # terminal width. Returning to Main restores the normal rules below.
        if self._agent_view_active:
            return False
        if self._sidebar_mode == "shown":
            return True
        if self._sidebar_mode == "hidden":
            return False
        return (self.size.width or 0) > self._WIDE_COLS  # auto

    def _apply_sidebar_visibility(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Sidebar)
        except Exception:  # pragma: no cover - before mount
            return
        sidebar.set_class(not self._sidebar_visible(), "-hidden")

    # ── session rail visibility / focus (docs/architecture/frontend-adapters.md §4.1/§7.1) ───────
    def _rail_visible(self) -> bool:
        # No listing Port (one-shot / no persistence) → never show (§4.4/§8).
        if self._session_query is None:
            return False
        # The drill-in AgentView takes the full width; both side panels yield.
        if self._agent_view_active:
            return False
        if self._rail_mode == "shown":
            return True
        if self._rail_mode == "hidden":
            return False
        return (self.size.width or 0) >= _RAIL_COLS  # auto

    def _apply_rail_visibility(self) -> None:
        try:
            rail = self.query_one("#session-rail", SessionRail)
        except Exception:  # pragma: no cover - before mount
            return
        rail.set_class(not self._rail_visible(), "-hidden")

    def _rail_currently_shown(self) -> bool:
        """Whether the rail widget is presently un-hidden (DOM truth)."""
        try:
            rail = self.query_one("#session-rail", SessionRail)
        except Exception:  # pragma: no cover - before mount
            return False
        return not rail.has_class("-hidden")

    def _rail_focused(self) -> bool:
        try:
            rail = self.query_one("#session-rail", SessionRail)
        except Exception:  # pragma: no cover
            return False
        focused = self.focused
        return focused is rail or (focused is not None and focused in rail.walk_children())

    def action_focus_rail(self) -> None:
        """Smart three-state focus toggle (§7.1): hidden→show+focus,
        shown→focus, focused→hide + return focus to the input area."""
        if self._session_query is None:
            self.toast("当前未启用会话持久化", variant="warning")
            return
        if self._rail_focused():
            self._hide_rail()
            return
        if not self._rail_visible():
            self._rail_mode = "shown"
            self._apply_rail_visibility()
            self._on_layout_changed()
        self._refresh_session_rail()
        try:
            self.query_one("#session-rail", SessionRail).focus_default()
        except Exception:  # pragma: no cover
            pass

    def _sync_input_accent(self) -> None:
        """Dim the input's left accent bar while the rail holds focus (§6.1)."""
        try:
            wrap = self.query_one("#input-wrap")
        except Exception:  # pragma: no cover - before mount
            return
        wrap.set_class(self._rail_focused(), "-dim")

    def on_descendant_focus(self, event: object) -> None:  # noqa: ARG002
        # Any focus change (keyboard toggle OR mouse click) re-syncs the accent.
        self._sync_input_accent()

    def on_descendant_blur(self, event: object) -> None:  # noqa: ARG002
        self._sync_input_accent()

    def _on_layout_changed(self) -> None:
        """Re-sync render width after a side-panel show/hide changed #main."""
        try:
            if self._render_ctx is not None:
                self._render_ctx.width = max(40, self._main_width())
        except Exception:  # pragma: no cover
            pass

    def _current_model(self) -> str:
        """Live model name: AppContext.model is the source of truth after a
        /model switch; fall back to the construction-time value."""
        live = getattr(self._app_ctx, "model", None)
        return str(live) if live else self._model

    def _full_model_name(self) -> str:
        """Resolve the live model to its full canonical id (alias → model_id).

        The CLI may be launched with a short alias (e.g. ``dsv4``); the meta line
        and sidebar show the full name instead. The resolution is memoised on the
        raw name so the 10 Hz refresh tick does not re-import/re-resolve every
        frame (it only changes after a ``/model`` switch). Falls back to the raw
        value if the registry is unavailable or the name is not a known alias.
        """
        name = self._current_model()
        if name == self._full_model_src:
            return self._full_model_cache
        resolved = name
        try:
            from autoverse.infra.llm.registry import resolve_alias

            resolved = resolve_alias(name)
        except Exception:  # pragma: no cover - defensive
            resolved = name
        self._full_model_src = name
        self._full_model_cache = resolved
        return resolved

    def _current_context_window(self) -> int:
        """Live context window for the *active* model.

        The model id is the single source of truth: the window is always
        recomputed from the live model (``AppContext.model`` after a ``/model``
        switch), memoised on the resolved id so the 10 Hz refresh tick does not
        re-import the registry every frame. The construction-time
        ``_context_window`` is only a fallback *and only for the startup model*:
        if the registry is unreachable for a model that was switched into, we
        return 0 rather than the startup model's (wrong) window — a "0 / —"
        readout is honest, a confidently-wrong number is not.
        """
        name = self._full_model_name()
        if name == self._ctx_win_src:
            return self._ctx_win_cache
        # Fallback only applies when the live model is still the startup model
        # the construction-time value was computed for; otherwise it would be a
        # different model's window. ``_startup_model_full`` is resolved lazily.
        fallback = self._context_window if name == self._startup_model_full() else 0
        win = fallback
        try:
            from autoverse.infra.llm.registry import get_context_window

            win = get_context_window(name)
        except Exception:  # pragma: no cover - defensive
            win = fallback
        self._ctx_win_src = name
        self._ctx_win_cache = win
        return win

    def _startup_model_full(self) -> str:
        """Full canonical id of the *startup* model (construction-time ``model``).

        Used to gate the construction-time ``_context_window`` fallback so it is
        never returned for a model that was switched into. Memoised because the
        startup model never changes after construction.
        """
        if self._startup_model_full_cache is not None:
            return self._startup_model_full_cache
        resolved = self._model
        try:
            from autoverse.infra.llm.registry import resolve_alias

            resolved = str(resolve_alias(self._model))
        except Exception:  # pragma: no cover - defensive
            resolved = self._model
        self._startup_model_full_cache = str(resolved)
        return str(resolved)

    def _model_tiers(self) -> tuple[tuple[str, str], ...]:
        """The three sub-agent tier→model rows for the sidebar (deep/balanced/fast).

        Reads the live ``config.model_tiers`` (hot-swapped by ``/tiers``); each
        configured id is resolved through the alias table for display parity with
        the main-model line. Unset tiers carry an empty id (rendered as "(main)").
        Defensive: any missing/odd config shape yields no rows (panel hides)."""
        config = getattr(self._app_ctx, "config", None)
        tiers = getattr(config, "model_tiers", None)
        if tiers is None:
            return ()
        try:
            from autoverse.infra.llm.registry import resolve_alias
        except Exception:  # pragma: no cover - defensive
            resolve_alias = None  # type: ignore[assignment]

        def _disp(raw: str) -> str:
            if not raw:
                return ""
            if resolve_alias is None:
                return raw
            try:
                return resolve_alias(raw)
            except Exception:  # pragma: no cover - defensive
                return raw

        out: list[tuple[str, str]] = []
        for name in ("deep", "balanced", "fast"):
            out.append((name, _disp(str(getattr(tiers, name, "") or ""))))
        return tuple(out)

    def _memory_files(self) -> list[str]:
        """List of memory file names loaded from cwd (e.g. ["CLAUDE.md", "AGENTS.md"])."""
        mem = getattr(self._app_ctx, "memory", None)
        if mem is None:
            return []
        blocks = getattr(mem, "blocks", ())
        return [str(b.source) for b in blocks]

    def _loaded_extensions(self) -> tuple[SidebarExtension, ...]:
        """Loaded skills/plugins for the right sidebar, via ExtensionAdminService."""
        extra = getattr(self._app_ctx, "extra", None)
        admin = extra.get("extension_admin") if isinstance(extra, dict) else None
        if admin is None:
            return ()

        out: list[SidebarExtension] = []
        for method_name in ("list_skills", "list_plugins"):
            method = getattr(admin, method_name, None)
            if method is None:
                continue
            try:
                items = method()
            except Exception:  # noqa: BLE001 - sidebar must not break the turn loop
                continue
            for item in items:
                out.append(
                    SidebarExtension(
                        kind=str(getattr(item, "kind", "")),
                        name=str(getattr(item, "display_name", "") or getattr(item, "id", "")),
                        state=str(getattr(item, "state", "") or "enabled"),
                        source=str(getattr(item, "source", "") or ""),
                        diagnostics_count=int(getattr(item, "diagnostics_count", 0) or 0),
                    )
                )
        return tuple(out)

    def _refresh_status_context(self) -> None:
        """Update the input meta line (mode · model) from state.

        The idle status footer no longer repeats model/cwd — those now live on
        the input frame's 4th row (the meta line), so the footer stays a clean
        ``ready · /help · …`` hint.
        """
        status = self._status_bar()
        if status is not None:
            status.context = ""
        # The input frame's inner meta line (MiMo shows agent · model there).
        # Left: mode · model. Right: a muted hint for the newline key (terminals
        # vary on Shift+Enter, so Ctrl+J is the reliable one). Always non-empty
        # so the inner line is visibly part of the prompt frame.
        t = self._av_theme
        try:
            meta = self.query_one("#input-meta", PromptMeta)
        except Exception:  # pragma: no cover - meta not mounted yet
            return
        # Recolour the input frame's left bar to match the active mode badge so
        # the prompt's vertical line and the badge always read the same mode.
        self._apply_input_mode_class()
        bits = []
        # Mode badge first (docs/guides/using-cli-and-tui.md §3.10): each mode gets its own colour
        # so the active interaction mode reads at a glance — normal is amber
        # (warning), plan stands out in calm green (success), worktree in
        # lavender (mode_worktree) with its branch name (isolated-write mode).
        label, role = self._mode_badge()
        bits.append(f"[bold {t.role(role)}]{label}[/]")
        # Full model name (resolved from any alias) — no cwd here; the Directory
        # lives in the sidebar only.
        model = self._full_model_name()
        if model:
            bits.append(f"[{t.role('text')}]{model}[/]")
        left = f"[{t.role('text_dim')}] · [/]".join(bits)
        hint = f"[{t.role('text_dim')}]Tab 切换模式 · Ctrl+J newline[/]"
        meta.text_line = f"{left}    {hint}" if left else hint

    def _refresh_sidebar(self) -> None:
        if not self._sidebar_visible():
            return
        try:
            sidebar = self.query_one("#sidebar", Sidebar)
        except Exception:  # pragma: no cover
            return
        s = self._sink
        tps = 0.0
        if s is not None and self._turn_started_at and self._mode_stack.current() is InputMode.RUNNING:
            elapsed = time.monotonic() - self._turn_started_at
            if elapsed > 0.5:
                tps = s.tokens_used / elapsed if s.tokens_used else 0.0
        sidebar.set_state(SidebarState(
            model=self._full_model_name(),
            version=self._version,
            session_id=self._session_id,
            cwd=str(getattr(self._app_ctx, "cwd", None) or self._cwd),
            context_window=self._current_context_window(),
            tokens_used=s.tokens_used if s else 0,
            cost_usd=s.cost_usd if s else 0.0,
            cost_known=s.cost_known if s else True,
            tps=tps,
            cache_read_tokens=s.cum_cache_read if s else 0,
            cache_write_tokens=s.cum_cache_write if s else 0,
            cache_hit_rate=s.cache_hit_rate if s else 0.0,
            todos=s.todos if s else (),
            changed_files=self._relativise_changed(s.changed_files) if s else (),
            agents=s.agents if s else (),
            model_tiers=self._model_tiers(),
            memory_files=tuple(self._memory_files()),
            extensions=self._loaded_extensions(),
        ))

    def _relativise_changed(
        self, files: tuple[tuple[str, str, int, int], ...]
    ) -> tuple[tuple[str, str, int, int], ...]:
        """Make the sink's absolute changed-file paths relative to the cwd for
        the sidebar (falls back to the absolute path if relpath fails, e.g. a
        different drive on Windows or an absolute path outside the workspace)."""
        import os

        out: list[tuple[str, str, int, int]] = []
        for path, status, added, removed in files:
            try:
                rel = os.path.relpath(path, self._cwd) if self._cwd else path
            except ValueError:
                rel = path
            out.append((rel, status, added, removed))
        return tuple(out)

    def action_toggle_sidebar(self) -> None:
        self._sidebar_mode = "hidden" if self._sidebar_visible() else "shown"
        self._apply_sidebar_visibility()
        self._refresh_sidebar()

    # ── session rail refresh (docs/architecture/frontend-adapters.md §4.2.1) ─────────────────────
    def _current_live_status(self) -> str:
        """The current session's live status from in-process truth (§5.6.3).

        ``running`` while a turn drives the engine, ``awaiting_input`` while an
        inline choice/approval is pending, else the listed (idle) state. This is
        the only live status the single-engine app can know."""
        if (self._choice_future is not None and not self._choice_future.done()) \
                or self._session_id in self._awaiting_sessions:
            return "awaiting_input"
        if self._session_id in self._running_sessions:
            return "running"
        return ""

    def _refresh_session_rail(self) -> None:
        """Schedule a rail refresh (debounced via the exclusive worker group)."""
        if self._session_query is None or not self._rail_visible():
            return
        self._refresh_rail_worker()

    @work(exclusive=True, group="rail")
    async def _refresh_rail_worker(self) -> None:
        query = self._session_query
        if query is None:
            return
        try:
            summaries = await query.list()
        except Exception:  # pragma: no cover - listing must never crash the UI
            return
        # Overlay the current session's live status + clear its unseen bit (it is
        # the one being viewed). The app holds this truth, not the Port (§5.6.7).
        from dataclasses import replace as _replace

        live = self._current_live_status()
        cur = self._session_id
        self._rail_titles = {s.session_id: s.title for s in summaries}
        rows = []
        running_id = ""
        for s in summaries:
            if s.session_id == cur:
                status = cast(
                    "SessionStatus",
                    live
                    or (
                        "idle"
                        if s.status in ("idle", "completed_unseen", "error_unseen")
                        else s.status
                    ),
                )
                s = _replace(s, status=status, unseen=False)
            # Background sessions: overlay the in-process live truth (§5.6.3) —
            # a parked relay reads awaiting_input, a live turn reads running. The
            # store-derived completed_unseen/error_unseen takes over once the
            # background turn ends and its task clears these sets.
            elif s.session_id in self._awaiting_sessions:
                s = _replace(s, status="awaiting_input")
            elif s.session_id in self._running_sessions:
                s = _replace(s, status="running")
            if s.status == "running":
                running_id = s.session_id
            rows.append(s)
        # Default-collapse every group except the one holding the current session
        # (§6.3.4). Applied once per newly-seen group so a manual expand sticks;
        # the current group is always re-expanded so switching reveals its rows.
        # Guarded on a *known* current group: at mount the first refresh runs
        # before the engine resolves `_session_id`, so cur_group would be "" and
        # we'd wrongly collapse (and burn the first-seen default of) every group,
        # including the user's own. Skip until we can place the current session.
        cur_group = next(
            (cwd_group_key(s) for s in rows if s.session_id == cur), ""
        )
        if cur_group:
            collapsed = set(self._collapsed_groups)
            for s in rows:
                g = cwd_group_key(s)
                if g not in self._seen_groups:
                    self._seen_groups.add(g)
                    if g != cur_group:
                        collapsed.add(g)
            # Auto-expand the current group only when we've just *switched* into
            # it (cur_group changed) — revealing the new session's rows. While the
            # current group stays put, leave it alone so a manual collapse sticks.
            if cur_group != self._last_cur_group:
                collapsed.discard(cur_group)
                self._last_cur_group = cur_group
            self._collapsed_groups = frozenset(collapsed)
        state = SessionRailState(
            sessions=tuple(rows),
            current_id=cur,
            running_id=running_id,
            collapsed_groups=self._collapsed_groups,
            spinner_frame=_SPINNER[self._spinner_i],
            now=time.time(),
        )
        try:
            self.query_one("#session-rail", SessionRail).set_state(state)
        except Exception:  # pragma: no cover - teardown race
            pass

    @work(group="viewed")
    async def _mark_viewed(self, session_id: str) -> None:
        """Advance the shared catalog view stamp, with a legacy Store fallback."""
        catalog = getattr(self._app_ctx, "session_catalog", None)
        if catalog is not None and session_id:
            try:
                import time as _time

                await catalog.mark_viewed(session_id, _time.time())
            except Exception:  # pragma: no cover - catalog repair handles failures
                pass
            return
        store = getattr(self._app_ctx, "store", None)
        if store is None or not session_id:
            return
        try:
            import time as _time

            now = _time.time()
            meta = await store.load_meta(session_id)
            existing = float(
                (meta.extra if isinstance(meta.extra, dict) else {}).get(
                    "last_viewed_at", 0.0
                )
                or 0.0
            )
            await store.update_meta(
                session_id, last_viewed_at=max(existing, now)
            )
        except Exception:  # pragma: no cover - a stamp failure must not break the UI
            pass

    # ── rail messages → switching (docs/architecture/frontend-adapters.md §5.2) ──────────────────
    def on_session_rail_new_session_clicked(
        self, message: "SessionRail.NewSessionClicked"
    ) -> None:
        self._do_switch("")

    def on_session_rail_session_clicked(
        self, message: "SessionRail.SessionClicked"
    ) -> None:
        self._do_switch(message.session_id)

    def on_session_rail_group_toggled(
        self, message: "SessionRail.GroupToggled"
    ) -> None:
        key = message.group_key
        if key in self._collapsed_groups:
            self._collapsed_groups = self._collapsed_groups - {key}
        else:
            self._collapsed_groups = self._collapsed_groups | {key}
        self._refresh_session_rail()

    def on_session_rail_focus_returned(
        self, message: "SessionRail.FocusReturned"
    ) -> None:
        try:
            self.query_one("#input", CommandInput).focus()
        except Exception:  # pragma: no cover
            pass

    def on_session_rail_hide_requested(
        self, message: "SessionRail.HideRequested"
    ) -> None:
        """Collapse the rail and hand focus back to the input (§4.1 `h`)."""
        self._hide_rail()

    def _hide_rail(self) -> None:
        """Pin the rail hidden, reclaim its width for #main, refocus input."""
        self._rail_mode = "hidden"
        self._apply_rail_visibility()
        self._on_layout_changed()
        try:
            self.query_one("#input", CommandInput).focus()
        except Exception:  # pragma: no cover
            pass

    def on_session_rail_delete_requested(
        self, message: "SessionRail.DeleteRequested"
    ) -> None:
        self._confirm_delete_session(message.session_id)

    def on_session_rail_rename_requested(
        self, message: "SessionRail.RenameRequested"
    ) -> None:
        self._rename_session(message.session_id)

    def _do_switch(self, session_id: str) -> None:
        """Validate + launch a session switch (§5.2). Synchronous guards run here;
        the async rebuild/replay is the worker below."""
        if self._switch_engine is None:
            return
        # ① same session → no-op, just hand focus back to input (§5.2 ①).
        if session_id and session_id == self._session_id:
            try:
                self.query_one("#input", CommandInput).focus()
            except Exception:  # pragma: no cover
                pass
            return
        # §5.6.3: a running current turn is NOT a switch barrier — it detaches to
        # the background and keeps going (the old §5.2 ② RUNNING guard is gone).
        if self._switching:
            return
        self._switch_worker(session_id)

    @work(exclusive=True, group="switch")
    async def _switch_worker(self, session_id: str) -> None:
        # ③ queued messages belong to the old session — confirm discarding them.
        if len(self._queue) > 0:
            ok = await self.confirm(
                f"有 {len(self._queue)} 条排队消息将丢弃，仍切换？",
                title="切换会话", confirm_label="切换", cancel_label="取消",
            )
            if not ok:
                return
        switch = self._switch_engine
        if switch is None:
            return
        self._switch_gen += 1
        mine = self._switch_gen
        self._switching = True
        try:
            log = self.query_one("#log", ConversationLog)
            new_ctx = RenderContext(
                theme=self._av_theme, ansi=True, width=max(40, self._main_width())
            )
            new_sink = TuiEventSink(log, new_ctx)
            # ① Detach the outgoing session: stop it painting #log, but keep its
            # engine (and any in-flight turn) alive in the pool (§5.6.3). Releasing
            # its `detached` event lets the foreground drain return so a new turn
            # can run; the turn task itself keeps going in the background.
            old_sid = self._session_id
            old = self._live.get(old_sid)
            if old is not None:
                old.redirect.set_target(None)
                old.last_used = time.monotonic()
                if old.detached is not None:
                    old.detached.set()
            # ② Acquire the target engine: warm reuse (already live, e.g. a still-
            # running background session) or a cold build bound to *session_id*.
            warm = self._live.get(session_id) if session_id else None
            if warm is not None:
                warm.redirect.set_target(new_sink)
                new = warm.engine
                interrupt = warm.interrupt
            else:
                redirect = RedirectSink(target=new_sink)
                try:
                    new = await switch(session_id, redirect)
                except Exception as exc:  # P5: build failed → keep old intact
                    if old is not None:
                        old.redirect.set_target(self._sink)  # roll back the detach
                    self.toast(f"切换失败：{exc}", variant="error")
                    return
                new_sid = str(getattr(new, "session_id", "") or "")
                interrupt = InterruptHandler(new)
                self._live[new_sid] = _LiveSession(
                    engine=new, redirect=redirect, interrupt=interrupt,
                    last_used=time.monotonic(),
                )
                self._bind_ask_user(new, new_sid)
            if mine != self._switch_gen:  # superseded by a later switch (§5.2.1)
                if warm is None:  # retire the engine we just built
                    sess = self._live.pop(str(getattr(new, "session_id", "") or ""), None)
                    if sess is not None:
                        self._evict_engine(sess.engine)
                else:  # detach the warm target we briefly re-attached
                    warm.redirect.set_target(None)
                return
            # ③ Swap the foreground references onto the target engine (§5.2 ⑤).
            self._engine = new
            self._sink = new_sink
            self._render_ctx = new_ctx
            self._session_id = str(getattr(new, "session_id", "") or "")
            self._interrupt = interrupt
            self._app_ctx = getattr(new, "app", None) or self._app_ctx
            self._live[self._session_id].last_used = time.monotonic()
            self._queue.clear()          # queue belonged to the old session (§5.5)
            self._refresh_queue_panel()
            # ④ replay the target session's history into a cleared #log (§5.3).
            await self._replay_history(self._session_id)
            self._engine_ready = True
            # ⑤ If we switched INTO a still-running background turn, restore the
            # foreground band + live footer so it reads as running again (§5.6.3).
            self._sync_fg_running()
            if self._session_id in self._running_sessions:
                self._turn_started_at = time.monotonic()
                self._begin_turn_footer()
                self._set_phase("generating")
            self._mark_viewed(self._session_id)
            self._refresh_status_context()
            self._refresh_sidebar()
            self._refresh_session_rail()
            # ⑥ Surface any approval/question that parked while backgrounded.
            self._resume_pending_relay(self._session_id)
            # Cap the pool now that the old session is idle/backgrounded.
            self._evict_idle_sessions()
            title = self._session_title(self._session_id)
            self.toast(f"已切换到 {title}", variant="info")
        finally:
            self._switching = False

    async def _replay_history(self, session_id: str) -> None:
        """Clear #log and repaint the session's stored conversation (§5.3)."""
        from rich.text import Text

        try:
            log = self.query_one("#log", ConversationLog)
        except Exception:  # pragma: no cover
            return
        # Await the clear: remove_children() is async, so without this the old
        # session's widgets are still mounted while we repaint the new one.
        await cast(Awaitable[object], log.clear())
        store = getattr(self._app_ctx, "store", None)
        ctx = self._render_ctx
        if store is None or ctx is None:
            return
        try:
            messages = await store.load(session_id)
        except Exception as exc:  # P5: corrupt history → banner, do not crash
            log.write(Text(f"(无法载入历史: {exc})", style=self._av_theme.role("text_dim")))
            return
        self._collect_replayed_history_state(messages)
        blocks = render_history_blocks(messages, ctx)
        muted = self._av_theme.role("text_muted")
        text_role = self._av_theme.role("text")
        for b in blocks:
            if b.kind == "user":
                row = Text()
                for i, ln in enumerate(b.text.splitlines() or [""]):
                    if i:
                        row.append("\n")
                    row.append(ln, style=text_role)
                log.write(row, classes="user-echo")
            elif b.kind == "agent":
                md = dict(b.metadata)
                job_id = str(md.get("job_id") or "")
                if self._sink is not None and job_id:
                    try:
                        self._sink.add_replayed_subagent(
                            job_id=job_id,
                            subagent_type=str(md.get("subagent_type") or "general"),
                            description=str(md.get("description") or ""),
                            task=str(md.get("task") or ""),
                            status=str(md.get("status") or "done"),
                            tool_count=_coerce_int(md.get("tool_count")),
                            summary=str(md.get("summary") or ""),
                        )
                    except (TypeError, ValueError):
                        self._sink.add_replayed_subagent(
                            job_id=job_id,
                            subagent_type=str(md.get("subagent_type") or "general"),
                            description=str(md.get("description") or ""),
                            task=str(md.get("task") or ""),
                            status=str(md.get("status") or "done"),
                            summary=str(md.get("summary") or ""),
                        )
                if job_id:
                    log.write_or_update_agent(job_id, Text.from_ansi(b.text), done=True)
                else:
                    log.write(Text.from_ansi(b.text))
            elif b.kind == "markdown":
                log.write_markdown(b.text)
            elif b.kind == "thinking":
                log.write_collapsible(b.title or "Thought", Text(b.text, style=muted))
            elif b.kind == "collapsible":
                # Thinking bodies are muted prose; tool bodies carry their own ANSI.
                body = (
                    Text(b.text, style=muted)
                    if b.thinking_body
                    else Text.from_ansi(b.text)
                )
                log.write_collapsible(b.title, body, classes=b.css_class)
            elif b.kind == "tool_images":
                # WebUI replays tool-produced screenshots from this block kind.
                # The terminal UI has no inline bitmap surface, so it simply
                # keeps the original tool text card.
                continue
            else:
                log.write(Text.from_ansi(b.text))

    def _collect_replayed_history_state(self, messages: list[object]) -> None:
        """Rehydrate sink-collected sidebar state while switching sessions.

        ``_replay_history`` paints stored messages directly into ``#log`` rather
        than re-emitting them through ``TuiEventSink``. Without this data-only pass,
        session switch replay looks correct but the new sink loses derived panels
        such as Todo and Changed files until the next live event.
        """
        sink = self._sink
        if sink is None:
            return
        sink.collect_replayed_messages(messages)

    def _session_title(self, session_id: str) -> str:
        """Display title for a session id (sanitised, short-id fallback)."""
        from autoverse.frontends.repl.session_rail import _short_id

        raw = self._rail_titles.get(session_id, "")
        return sanitize_title(raw, max_cells=30, fallback=_short_id(session_id))

    @work(group="delete")
    async def _confirm_delete_session(self, session_id: str) -> None:
        query = self._session_query
        if query is None:
            return
        title = self._session_title(session_id)
        ok = await self.confirm(
            f"删除会话「{title}」？此操作不可撤销。",
            title="删除会话", confirm_label="删除", cancel_label="取消",
        )
        if not ok:
            return
        is_current = session_id == self._session_id
        try:
            await query.delete(session_id)
        except Exception as exc:  # pragma: no cover
            self.toast(f"删除失败：{exc}", variant="error")
            return
        if is_current:
            # Deleting the current session: switch to the most-recent remaining
            # one, or open a fresh session if none remain (§5.4, no dangling ref).
            try:
                remaining = await query.list()
            except Exception:  # pragma: no cover
                remaining = []
            nxt = next((s.session_id for s in remaining if s.session_id != session_id), "")
            self._do_switch(nxt)
        else:
            self._refresh_session_rail()

    @work(group="rename")
    async def _rename_session(self, session_id: str) -> None:
        query = self._session_query
        if query is None:
            return
        initial = self._session_title(session_id)
        new_title = await self.push_screen_wait(
            PromptScreen(
                "重命名会话", placeholder="新标题…",
                initial=initial, theme=self._av_theme,
            )
        )
        if not new_title:
            return
        try:
            await query.rename(session_id, str(new_title).strip())
        except Exception as exc:  # pragma: no cover
            self.toast(f"重命名失败：{exc}", variant="error")
            return
        self._refresh_session_rail()

    def on_resize(self, event: object) -> None:  # noqa: ARG002
        # Re-evaluate auto visibility when the terminal is resized.
        if self._sidebar_mode == "auto":
            self._apply_sidebar_visibility()
        if self._rail_mode == "auto":
            was_visible = self._rail_currently_shown()
            self._apply_rail_visibility()
            # Newly revealed by a widen → populate it (it may have been empty).
            if self._rail_visible() and not was_visible:
                self._refresh_session_rail()
        # The renderers fold/wrap output at a FIXED width captured on mount
        # (RenderContext.width). After a resize that width is stale, so every
        # already-mounted block's measured height no longer matches the rows it
        # actually paints — the root cause of the "duplicated rows when
        # scrolling" artifact (Textual composites scroll as a row delta against
        # those measured heights). Re-sync the width and force a full relayout +
        # repaint of both output regions so the height map is rebuilt cleanly.
        ctx = self._render_ctx
        if ctx is not None:
            ctx.width = max(40, self._main_width())
        try:
            log = self.query_one("#log", ConversationLog)
            log.refresh(layout=True)
        except Exception:  # pragma: no cover - mount/teardown window
            pass
        try:
            view = self.query_one("#agentview", AgentView)
            if ctx is not None:
                view.set_width(ctx.width)
            view.refresh(layout=True)
        except Exception:  # pragma: no cover - mount/teardown window / not built
            pass

    # ── helpers ────────────────────────────────────────────────────────────
    def _status_bar(self) -> "StatusBar | None":
        """Return the StatusBar, or ``None`` during the mount/teardown window.

        Timers (``set_interval``) and the turn worker can fire a final callback
        after ``run_test``/the app begins tearing the widget tree down; querying
        ``#status`` then raises ``NoMatches``. Guard it like the sidebar lookups.
        """
        try:
            return self.query_one("#status", StatusBar)
        except Exception:  # pragma: no cover - mount/teardown race only
            return None

    @staticmethod
    def _elapsed_seconds(
        started_at: float,
        paused_elapsed: float = 0.0,
        pause_started_at: float = 0.0,
    ) -> float:
        if not started_at:
            return 0.0
        now = time.monotonic()
        elapsed = now - started_at - paused_elapsed
        if pause_started_at:
            elapsed -= now - pause_started_at
        return max(elapsed, 0.0)

    def _turn_elapsed_ms(self) -> float:
        return self._elapsed_seconds(
            self._turn_started_at,
            self._turn_paused_elapsed,
            self._turn_pause_started_at,
        ) * 1000.0

    def _pause_turn_timer(self) -> None:
        if not self._turn_started_at or self._turn_pause_started_at:
            return
        now = time.monotonic()
        self._turn_pause_started_at = now
        self._phase_pause_started_at = now

    def _resume_turn_timer(self) -> None:
        if not self._turn_pause_started_at:
            return
        now = time.monotonic()
        self._turn_paused_elapsed += max(now - self._turn_pause_started_at, 0.0)
        self._turn_pause_started_at = 0.0
        if self._phase_pause_started_at:
            self._phase_paused_elapsed += max(now - self._phase_pause_started_at, 0.0)
            self._phase_pause_started_at = 0.0

    def _set_phase(self, phase: str) -> None:
        status = self._status_bar()
        if status is None:  # mount/teardown window — widget not in the tree
            return
        # Restart the per-phase timer only on a REAL transition. Streaming events
        # (THINKING_DELTA / TEXT_DELTA) re-assert the same phase many times a
        # second — resetting on every one would peg the timer at ~0s and make it
        # useless. Comparing against the live reactive keeps it monotonic within
        # a phase and zeroes only when the operation genuinely changes.
        if phase != status.phase:
            self._phase_started_at = time.monotonic()
            self._phase_paused_elapsed = 0.0
            self._phase_pause_started_at = (
                time.monotonic() if self._turn_pause_started_at else 0.0
            )
        status.phase = phase
        if phase == "idle":
            status.frame = ""
            status.elapsed = 0.0
            status.activity = ""
            if self._sink is not None:
                self._sink.activity = ""
            # Re-sync the idle context line (model may have changed via /model).
            self._refresh_status_context()

    def _log_line(self, line: str) -> None:
        self.query_one("#log", ConversationLog).write(line)

    def _log_command_start(self, spec: "CommandSpec | None") -> None:
        """Write the lightweight persistent heading for workflow-like commands."""
        if spec is None or not spec.activity_label:
            return
        self.query_one("#log", ConversationLog).write(
            f"[bold $av-accent]/{spec.name}[/] "
            f"[$av-text-dim]·[/] {spec.activity_label}"
        )

    def _log_command_result(self, result: CommandResult) -> None:
        """Render command output using the presentation requested by its handler."""
        if not result.display:
            return
        log = self.query_one("#log", ConversationLog)
        if result.display_markdown:
            log.write_markdown(result.display)
        else:
            log.write(result.display)

    def _clear_log(self) -> None:
        """Wipe the visible conversation scrollback (engine history is cleared
        separately by the /clear handler). Invoked when a command returns
        ``should_clear`` so the screen matches the now-empty engine context."""
        try:
            self.query_one("#log", ConversationLog).clear()
        except Exception:  # pragma: no cover - mount/teardown window
            pass

    def _enqueue(self, text: str, *, priority: Priority = "next") -> None:
        """Enqueue *text*, stamping the interaction mode active right now so the
        queued panel can show the mode each pending message was sent under
        (frozen against later Tab mode changes, parity with the user-echo card
        + per-turn footer; docs/guides/using-cli-and-tui.md §3.10)."""
        self._queue.enqueue(text, priority=priority, mode=self._interaction_mode())

    def _refresh_queue_panel(self) -> None:
        """Sync the queued-message panel with the live queue (R5 visibility).

        Pending submissions stay visible just above the input frame; the panel
        is hidden whenever the queue is empty so it never occupies blank rows.
        """
        try:
            panel = self.query_one("#queued-panel", QueuedPanel)
        except Exception:  # pragma: no cover - mount/teardown window
            return
        items = self._queue.items_with_mode()
        panel.lines = tuple(items)
        panel.set_class(not items, "-hidden")

    def _echo_user(self, prompt: str) -> None:
        """Echo the user's message as a card (MiMo UserMessage parity).

        The echo is one ``user-echo`` block widget: a real left accent bar +
        grey background block drawn by CSS (not a baked-in ``▌`` glyph), with
        ``padding`` so the bar spans the full block height. Its block margin
        visually separates each exchange (no mechanical 'turn N').
        """
        from rich.text import Text

        text_role = self._av_theme.role("text")
        row = Text()
        for i, ln in enumerate(prompt.splitlines() or [""]):
            if i:
                row.append("\n")
            row.append(ln, style=text_role)
        # Stamp the SEND-time mode so the card's left bar is frozen to that
        # mode's colour (a later Tab recolours only the live input bar, never
        # this committed card).
        classes = f"user-echo -mode-{self._interaction_mode()}"
        log = self.query_one("#log", ConversationLog)
        log.write(row, classes=classes)
        if self._sink is not None:
            self._sink.mark_user_turn()  # reset the sink's vertical-rhythm state

    def _compose_turn_footer(self, elapsed_ms: float, interrupted: bool) -> "Text":
        """Build the per-turn footer line (MiMo/opencode parity): a muted
        metadata line with the model, elapsed time, token counts and cost —
        ``▪ dsv4 · 12.3s · ↑1.2k ↓340 · $0.0042``.

        This is a strictly PER-TURN view: the ↑↓ tokens, cache% and cost are
        shown only once THIS turn has produced a USAGE event (compared against
        the snapshot taken at turn start), and the cost is THIS turn's spend
        (current cumulative minus the start snapshot) — never the previous
        turn's stale figures carried over. So a fresh reply reads ``▪ model``
        (with a live elapsed) until its own usage arrives, then fills in.

        Pure (no mounting): used both for the live in-place refresh during a
        turn and the final frozen line at the turn boundary."""
        from rich.text import Text

        from autoverse.frontends.repl.renderer import _kfmt, format_duration

        t = self._av_theme
        accent = t.role("accent")
        muted = t.role("text_muted")
        dim = t.role("text_dim")
        s = self._sink
        sep = (" · ", dim)

        # Has THIS turn produced usage yet? (snapshot taken in _begin_turn_footer)
        turn_has_usage = s is not None and s.usage_seq > self._turn_usage_seq0

        row = Text()
        row.append("▪ ", style=accent)
        parts: list[tuple[str, str]] = [(self._current_model() or "model", muted)]
        # Mode badge (docs/guides/using-cli-and-tui.md §3.10): same label + colour as the input
        # meta badge / left bar, so the reply records which interaction mode it
        # ran under at a glance — normal=amber, plan=green, worktree=lavender.
        # Uses the turn-start snapshot (frozen in _begin_turn_footer) so a Tab
        # mid-turn / while reading never rewrites THIS reply's mode.
        mode_label, mode_role = self._turn_mode_badge or self._mode_badge()
        parts.append((mode_label, t.role(mode_role)))
        if elapsed_ms >= 1:
            parts.append((format_duration(elapsed_ms), muted))
        if turn_has_usage and s is not None:
            if s.last_input or s.last_output:
                parts.append((f"↑{_kfmt(s.last_input)} ↓{_kfmt(s.last_output)}", muted))
            # Cache + cost are deltas against the turn-start snapshot, so the
            # footer reflects only what THIS turn consumed/spent.
            cache_read = s.cum_cache_read - self._turn_cache_read0
            cache_write = s.cum_cache_write - self._turn_cache_write0
            prompt = s.cum_prompt - self._turn_prompt0
            if cache_read or cache_write:
                rate = (cache_read / prompt) if prompt > 0 else 0.0
                parts.append((f"cache {round(rate * 100)}%", muted))
            cost = s.cost_usd - self._turn_cost0
            if cost > 0:
                parts.append((f"${cost:.4f}", muted))
        if interrupted:
            parts.append(("interrupted", t.role("warning")))
        for i, (txt, style) in enumerate(parts):
            if i:
                row.append(*sep)
            row.append(txt, style=style)
        return row

    def _begin_turn_footer(self) -> None:
        """Mount the live turn-footer the moment a turn starts so the meta line
        is visible from the first frame (``▪ model``) and refreshes in place as
        the reply streams (driven by the 10 Hz tick).

        Snapshots the sink's session-cumulative usage counters so the footer can
        render a strictly per-turn view (see ``_compose_turn_footer``) instead of
        carrying the previous turn's ↑↓/cost until this turn's usage lands."""
        s = self._sink
        self._turn_usage_seq0 = s.usage_seq if s is not None else 0
        self._turn_cost0 = s.cost_usd if s is not None else 0.0
        self._turn_cache_read0 = s.cum_cache_read if s is not None else 0
        self._turn_cache_write0 = s.cum_cache_write if s is not None else 0
        self._turn_prompt0 = s.cum_prompt if s is not None else 0
        # Freeze the mode this turn runs under so the footer keeps it even if the
        # user Tabs to another mode mid-turn or while reading the reply.
        self._turn_mode_badge = self._mode_badge()
        self._footer_line = ""  # fresh turn → force the first live refresh to render
        try:
            log = self.query_one("#log", ConversationLog)
        except Exception:  # pragma: no cover - mount/teardown race
            return
        log.begin_footer(self._compose_turn_footer(0.0, False))

    def _refresh_turn_footer(self) -> None:
        """Update the live turn-footer in place (called from the tick). Shows the
        running elapsed plus whatever tokens/cost the sink has accumulated so
        far, so the meta line ticks live instead of only appearing at the end."""
        try:
            log = self.query_one("#log", ConversationLog)
        except Exception:  # pragma: no cover - mount/teardown race
            return
        elapsed_ms = self._turn_elapsed_ms()
        footer = self._compose_turn_footer(elapsed_ms, False)
        # Skip the widget update when the rendered line is unchanged (e.g. the
        # elapsed rounds to the same label between ticks at minute granularity).
        line = footer.plain
        if line == self._footer_line:
            return
        self._footer_line = line
        log.update_footer(footer)

    def _write_turn_footer(self, elapsed_ms: float, interrupted: bool) -> None:
        """Freeze the live turn-footer with the final elapsed/interrupt state.

        The footer was already mounted at turn start and refreshed live; here we
        commit any in-flight streamed answer, stamp the final line and release
        the live reference so it stays in place as the exchange's closing line."""
        log = self.query_one("#log", ConversationLog)
        s = self._sink
        # Commit any in-flight live-streamed answer (and flush buffered prose)
        # before freezing the footer — an interrupted turn may not emit a
        # trailing boundary event that would otherwise do this. The flushed
        # answer mounts ABOVE the footer (ConversationLog pins it last).
        if s is not None:
            s.finalize_turn()
        # If the live footer was never mounted (defensive — e.g. an interrupted
        # mount race), fall back to mounting it now so the line is never lost.
        log.begin_footer(self._compose_turn_footer(elapsed_ms, interrupted))
        log.update_footer(self._compose_turn_footer(elapsed_ms, interrupted))
        log.end_footer()
        if s is not None:
            s._last_kind = "usage"  # keep the sink's rhythm tracker in sync

    def _write_welcome(self, log: ConversationLog) -> None:
        """A composed welcome state (taste-skill: beautiful empty states).

        Accent wordmark + a muted hint line + a subtle rule — hierarchy by
        weight/colour, not scale; no boxes (cockpit aesthetic).
        """
        from rich.text import Text

        t = self._av_theme
        accent = t.role("accent")
        muted = t.role("text_muted")
        border = t.role("border_subtle")
        title = Text()
        title.append("◆ ", style=accent)
        title.append("AutoVerse", style=f"bold {t.role('text')}")
        title.append("  ", style="")
        title.append("the evolvable agent harness", style=muted)
        log.write(title, classes="banner")
        # The key-hint moved to the bottom status footer; keep a blank row here
        # so the wordmark still breathes above the rule.
        log.write(Text(" "), classes="banner")
        rule_w = min(48, max(20, self._main_width() - 6))
        log.write(Text("─" * rule_w, style=border), classes="banner")

    def _command_ctx(self) -> CommandContext:
        return CommandContext(
            engine=self._engine,
            app=self._app_ctx,
            registry=self._av_registry,
            permissions=getattr(self._app_ctx, "permissions", None),
            is_running=self._mode_stack.current() is InputMode.RUNNING,
            # "sink" lets /details toggle fold verbosity; "ui" is the DialogHost
            # facade so handlers (/model …) can open overlays (§8).
            extra={"sink": self._sink, "ui": self},
        )

    # ── DialogHost facade (docs/reference/commands.md §8) ──────────────────────────────
    async def select(
        self,
        options: "Sequence[SelectOption[object]]",
        *,
        title: str = "Select",
        placeholder: str = "type to filter…",
        multi: bool = False,
    ) -> object | None:
        """Open a searchable selection overlay.

        Single mode returns the chosen value (or None). Multi mode returns a
        list of checked values (empty list if none) or None if dismissed.
        """
        return await self.push_screen_wait(
            SelectScreen(list(options), title=title, placeholder=placeholder,
                         theme=self._av_theme, multi=multi)
        )

    async def confirm(
        self,
        message: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
    ) -> bool:
        """Open a yes/no overlay; return True if confirmed."""
        return await self.push_screen_wait(
            ConfirmScreen(message, title=title, confirm_label=confirm_label,
                          cancel_label=cancel_label, theme=self._av_theme)
        )

    async def prompt(
        self,
        title: str,
        *,
        placeholder: str = "",
        initial: str = "",
        password: bool = False,
    ) -> str | None:
        """Open a one-line input overlay; return None if cancelled."""
        return await self.push_screen_wait(
            PromptScreen(
                title,
                placeholder=placeholder,
                initial=initial,
                password=password,
                theme=self._av_theme,
            )
        )

    def toast(self, message: str, *, variant: str = "info") -> None:
        """Show a transient notification in the main content area (§8 I7)."""
        from autoverse.frontends.repl.dialogs.toast import ToastVariant

        v: ToastVariant = variant if variant in ("info", "success", "warning", "error") else "info"  # type: ignore[assignment]
        toast = Toast(message, variant=v, theme=self._av_theme)
        try:
            self.query_one("#toast-host", ToastHost).mount(toast)
        except Exception:  # pragma: no cover - defensive during early teardown/startup
            self.mount(toast)

    async def show_text(self, body: str, *, title: str = "") -> None:
        """Open a scrollable read-only viewer for a long report (§8 I9)."""
        await self.push_screen_wait(
            TextScreen(body, title=title, theme=self._av_theme)
        )

    # ── inline choice menu: AskUserQuestion + permission (gap12/B1) ───────────
    async def _ask_one_question(
        self,
        *,
        prompt: str,
        items: list[ChoiceItem],
        multi: bool,
        mode: InputMode,
        reserve_log_rows: int | None = None,
    ) -> list[str] | None:
        """Show one inline choice menu above the input and await the result.

        Resolves to the list of chosen option values (multi: all checked; single:
        the one selected), to a one-element list holding the user's free-text when
        the "Other" row is picked, or ``None`` if dismissed with Esc. The result
        Future is set from the forwarded key handlers (see :meth:`_handle_choice_key`).
        """
        menu = self.query_one("#choice", InlineChoiceMenu)
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[list[str] | None]" = loop.create_future()
        self._choice_future = fut
        self._choice_awaiting_custom = False
        self._choice_pending_selected = []
        # Focus the input so CommandInput._on_key captures/forwards menu keys (the
        # menu itself never takes focus, so the "Other" free-text answer can flow
        # straight into the same box).
        try:
            self.query_one("#input", CommandInput).focus()
        except Exception:  # pragma: no cover - defensive: input not mounted
            pass
        if reserve_log_rows is None:
            reserve_log_rows = self._choice_menu_reserved_rows(prompt, items)
        if reserve_log_rows > 0:
            self._reserve_log_overlay_space(reserve_log_rows)
        menu.open(prompt, items, multi=multi)
        menu.set_bottom_offset(self._completion_bottom_offset())
        with self._mode_stack.push(mode):
            try:
                return await fut
            finally:
                menu.close()
                if reserve_log_rows > 0:
                    self._clear_log_overlay_space()
                self._choice_future = None
                self._choice_awaiting_custom = False
                self._choice_pending_selected = []
                self._restore_input_placeholder()

    @staticmethod
    def _choice_menu_reserved_rows(prompt: str, items: list[ChoiceItem]) -> int:
        """Rows covered by InlineChoiceMenu, including border and hint."""
        content_rows = len(items) + 1  # options + hint
        if prompt:
            content_rows += 1
        return min(14, content_rows + 2)

    def _handle_choice_key(self, key: str) -> None:
        """Route a forwarded key to the active inline choice menu (picking state)."""
        menu = self.query_one("#choice", InlineChoiceMenu)
        if not menu.picking_now or self._choice_future is None:
            return
        if key == "up":
            menu.move_up()
        elif key == "down":
            menu.move_down()
        elif key == "escape":
            self._resolve_choice(None)
        elif key == "space" and menu.multi:
            menu.toggle_current()
        elif key == "enter":
            self._confirm_choice()
        else:
            value = menu.value_for_shortcut(key)
            if value is not None:
                self._resolve_choice([value])

    def _confirm_choice(self) -> None:
        menu = self.query_one("#choice", InlineChoiceMenu)
        if menu.multi:
            checked = menu.checked_values()
            if InlineChoiceMenu.CUSTOM in checked:
                pending = [v for v in checked if v != InlineChoiceMenu.CUSTOM]
                self._enter_custom_input(pending)
                return
            self._resolve_choice(checked)
        else:
            if menu.current_is_custom():
                self._enter_custom_input([])
                return
            value = menu.current_value()
            self._resolve_choice([value] if value is not None else [])

    def _resolve_choice(self, result: "list[str] | None") -> None:
        fut = self._choice_future
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _cancel_choice(self) -> None:
        """Resolve any pending choice to a cancel (Esc-equivalent).

        Used on a hard abort (Ctrl+C) so a pending permission/question never
        leaves the awaiting coroutine — and the permission adapter — wedged.
        """
        self._resolve_choice(None)

    def _enter_custom_input(self, pending: list[str]) -> None:
        """Hand free-text entry to the main input box (the "Other" escape hatch).

        The option list hides and the input box takes over: the user types their
        own answer and presses Enter (handled in ``on_command_input_entered``),
        or Esc to fall back to the option list.
        """
        menu = self.query_one("#choice", InlineChoiceMenu)
        menu.close()
        self._clear_log_overlay_space()
        self._choice_pending_selected = list(pending)
        self._choice_awaiting_custom = True
        try:
            inp = self.query_one("#input", CommandInput)
            inp.placeholder = "type your own answer… · Esc to go back"
            inp.focus()
        except Exception:  # pragma: no cover - defensive
            pass

    def _submit_choice_custom(self, value: str) -> None:
        """Consume the free-text answer the user typed for an "Other" choice."""
        text = (value or "").strip()
        selected = list(self._choice_pending_selected)
        if text:
            selected.append(text)
        self._choice_awaiting_custom = False
        self._restore_input_placeholder()
        self._resolve_choice(selected)

    def _handle_choice_custom_cancel(self) -> None:
        """Esc while typing a free-text answer → return to the option list."""
        self._choice_awaiting_custom = False
        self._restore_input_placeholder()
        menu = self.query_one("#choice", InlineChoiceMenu)
        menu.reopen()
        menu.set_bottom_offset(self._completion_bottom_offset())
        self._reserve_log_overlay_space(menu.reserved_rows())

    def _restore_input_placeholder(self) -> None:
        try:
            self.query_one("#input", CommandInput).placeholder = (
                CommandInput.DEFAULT_PLACEHOLDER
            )
        except Exception:  # pragma: no cover - defensive
            pass

    # ── AskUserQuestion UI (gap12/B1) ─────────────────────────────────────────
    async def _ask_user(
        self, payload: dict[str, object], session_id: str = ""
    ) -> dict[str, object]:
        """Render the AskUserQuestion tool's questions as inline choice menus.

        Wired into ``app.extra["ask_user"]`` so the tool resolves to a real
        selection instead of the "no interactive UI" error. Each question is shown
        as an :class:`InlineChoiceMenu` docked above the input (no full-screen
        modal, no fuzzy filter); a synthetic "✎ Other" row lets the user type a
        free-text answer in the input box. An Esc dismissal records an empty
        selection for that question. Returns
        ``{"answers": [{"id", "selected": [...]}]}``.
        """
        # Relay: a backgrounded session waits for switch-in before seizing the
        # single inline menu (§5.6.3); the foreground returns immediately.
        await self._gate_foreground(session_id)
        title = str(payload.get("title") or "")
        questions = payload.get("questions") or []
        answers: list[dict[str, object]] = []
        if isinstance(questions, list):
            for q in questions:
                if not isinstance(q, dict):
                    continue
                qid = str(q.get("id") or "")
                prompt = str(q.get("prompt") or qid)
                multi = bool(q.get("allow_multiple"))
                raw_options = q.get("options") or []
                items: list[ChoiceItem] = []
                if isinstance(raw_options, list):
                    for opt in raw_options:
                        if isinstance(opt, dict):
                            items.append(
                                ChoiceItem(
                                    value=str(opt.get("id") or ""),
                                    label=str(opt.get("label") or opt.get("id") or ""),
                                )
                            )
                # Always offer the escape hatch: the user must never be boxed into
                # the model's suggested options.
                items.append(
                    ChoiceItem(
                        value=InlineChoiceMenu.CUSTOM,
                        label="✎ Other (type your own answer)",
                        is_custom=True,
                    )
                )
                heading = f"{title} — {prompt}" if title else prompt
                result = await self._ask_one_question(
                    prompt=heading, items=items, multi=multi,
                    mode=InputMode.MULTICHOICE,
                )
                answers.append({"id": qid, "selected": result or []})
        # When the user dismissed every question (Esc on each), flag the payload
        # so the tool (and its TUI renderer) can distinguish a real "no answer"
        # (the user picked nothing from the options) from an outright dismissal.
        cancelled = all(not a.get("selected") for a in answers) if answers else True
        return {"answers": answers, "cancelled": cancelled}

    # ── permission approval (inline, replaces the modal PermissionScreen) ─────
    async def request_permission(
        self, call: "ToolCall", session_id: str = ""
    ) -> "Decision":
        """Ask the user to approve a tool call via the inline choice menu.

        Resolves to a :class:`Decision`: ``y``/Allow once → allow, ``a``/Always →
        allow (the adapter then persists a scoped rule), ``n``/Esc → deny. Runs
        inline above the input rather than as a full-screen modal, sharing the
        AskUserQuestion menu/key machinery.
        """
        from autoverse.types.tools import Decision

        # Relay a backgrounded session's approval until switch-in (§5.6.3).
        await self._gate_foreground(session_id)
        target = self._summarize_call(call)
        prompt = f"Permission required · {call.name}" + (
            f" · {target}" if target else ""
        )
        items = [
            ChoiceItem(value="allow", label="Allow once", shortcut="y"),
            ChoiceItem(value="always", label="Always allow", shortcut="a"),
            ChoiceItem(value="deny", label="Deny", shortcut="n"),
        ]
        self._pause_turn_timer()
        try:
            result = await self._ask_one_question(
                prompt=prompt, items=items, multi=False, mode=InputMode.PERMISSION,
            )
        finally:
            self._resume_turn_timer()
        choice = result[0] if result else "deny"
        if choice == "allow":
            return Decision.allow(rule="user-approved")
        if choice == "always":
            return Decision.allow(rule="user-always")
        return Decision.deny(reason="denied by user")

    async def _handle_plan_proposed(self, engine: object) -> None:
        """Approve/revise/cancel an ExitPlanMode proposal after the turn stops."""
        app = getattr(engine, "app", None)
        extra = getattr(app, "extra", None)
        plan_mode = extra.get("plan_mode") if isinstance(extra, dict) else None
        if not (isinstance(plan_mode, dict) and plan_mode.get("pending_approval")):
            return
        plan = str(plan_mode.get("plan") or "").strip()
        prompt = "Plan proposed"
        items = [
            ChoiceItem(value="approve", label="Approve and switch to normal", shortcut="y"),
            ChoiceItem(value="revise", label="Revise plan", shortcut="r"),
            ChoiceItem(value="cancel", label="Cancel", shortcut="n"),
        ]
        result = await self._ask_one_question(
            prompt=prompt, items=items, multi=False, mode=InputMode.PERMISSION,
        )
        choice = result[0] if result else "cancel"
        if choice == "approve":
            cast(dict[str, object], extra)["plan_mode"] = {
                "active": False,
                "pending_approval": False,
                "plan": plan,
                "approved": True,
            }
            self._log_line("[bold $av-success]Plan approved. Executing now.[/bold $av-success]")
            self._enqueue(approved_plan_prompt(plan), priority="now")
            self._refresh_queue_panel()
        elif choice == "revise":
            cast(dict[str, object], extra)["plan_mode"] = {
                "active": True,
                "pending_approval": False,
                "reason": "user requested plan revision",
                "plan": plan,
            }
            self._log_line("[bold $av-warning]Plan returned for revision. Plan mode restored.[/bold $av-warning]")
        else:
            cast(dict[str, object], extra)["plan_mode"] = {
                "active": False,
                "pending_approval": False,
                "plan": plan,
                "cancelled": True,
            }
            self._log_line("[dim]Plan cancelled; no changes will be made.[/dim]")
        self._refresh_status_context()

    @staticmethod
    def _summarize_call(call: "ToolCall") -> str:
        data = getattr(call, "input", {}) or {}
        if "file_path" in data:
            return str(data["file_path"])
        if "command" in data:
            return str(data["command"])
        return ""

    # ── command palette (ctrl+p, §8 I5) ───────────────────────────────────────
    @work(group="palette")
    async def action_command_palette(self) -> None:  # type: ignore[override]
        entries = self._av_registry.palette_entries(self._command_ctx())
        # suggested first (when no query), then the rest — both via categories.
        options: list[SelectOption[str]] = []
        for entry in entries:
            spec = entry.spec
            footer = spec.keybind or ""
            cat = "suggested" if spec.suggested else spec.group
            options.append(
                SelectOption(
                    title=f"/{spec.name}",
                    value=spec.name,
                    description=spec.summary,
                    footer=footer,
                    category=cat,
                    disabled=not entry.enabled,
                )
            )
        # Order: suggested group first.
        options.sort(key=lambda o: (o.category != "suggested", o.category, o.title))
        chosen = await self.push_screen_wait(
            SelectScreen(options, title="Commands", placeholder="search commands…",
                         theme=self._av_theme)
        )
        if chosen is not None:
            self._run_command(f"/{chosen}")

    # ── subagent drill-down (in-place AgentView, MiMo route-driven parity) ─────
    @work(group="subagents")
    async def action_view_subagents(self) -> None:
        """Pick a dispatched sub-agent and open its context in the in-place view.

        Clicking a sidebar row enters directly (``on_sidebar_agent_clicked``);
        this picker is the keyboard/narrow-terminal path (``ctrl+r`` /
        ``/subagents``) where the sidebar may be hidden.
        """
        records = list(self._sink.subagents()) if self._sink else []
        if not records:
            self.toast("No sub-agents dispatched yet.", variant="info")
            return
        glyph = {"running": "●", "pending": "○", "done": "✓", "failed": "✗", "cancelled": "⊘"}
        options: list[SelectOption[int]] = []
        for i, rec in enumerate(records):
            meta: list[str] = []
            if rec.turn:
                meta.append(f"{rec.turn}t")
            if rec.elapsed_ms >= 1000:
                meta.append(f"{rec.elapsed_ms / 1000:.0f}s")
            options.append(
                SelectOption(
                    title=f"{glyph.get(rec.status, '●')} [{rec.subagent_type}]  {rec.status}",
                    value=i,
                    description=rec.description or "(no task recorded)",
                    footer=" · ".join(meta),
                )
            )
        chosen = await self.push_screen_wait(
            SelectScreen(options, title="Subagents", placeholder="filter sub-agents…",
                         theme=self._av_theme)
        )
        if chosen is None or isinstance(chosen, list):
            return
        self._enter_agent_view(index=int(chosen))

    # ── cancel a queued message (ctrl+x, MiMo editFollowup parity) ─────────────
    def _cancel_queued(self, index: int, expected_text: str) -> bool:
        """Remove one queued message, then refresh the panel. Returns success.

        ``index`` is the flat :meth:`MessageQueue.items` position the user
        picked. Because a turn boundary may drain an item between the picker
        opening and the choice landing, we verify the text at *index* still
        matches *expected_text* before popping; otherwise we fall back to
        removing the first item with that text. If neither matches (the message
        already started running), nothing is removed.
        """
        snapshot = self._queue.items()
        removed = False
        if 0 <= index < len(snapshot) and snapshot[index][1] == expected_text:
            removed = self._queue.pop_index(index) is not None
        else:
            removed = self._queue.remove_first(expected_text)
        if removed:
            self._refresh_queue_panel()
        return removed

    @work(group="queue")
    async def action_cancel_queued(self) -> None:
        """Pick a queued message to cancel (or clear all) via a select overlay."""
        items = self._queue.items()
        if not items:
            self.toast("No queued messages.", variant="info")
            return
        options: list[SelectOption[int]] = [
            SelectOption(
                title=f"✗ Clear all queued ({len(items)})",
                value=-1,
                category="actions",
            )
        ]
        for i, (prio, text) in enumerate(items):
            flat = " ".join(text.split())
            title = flat if len(flat) <= 72 else flat[:71] + "…"
            options.append(
                SelectOption(
                    title=title or "(empty)",
                    value=i,
                    footer=prio,
                    category="queued",
                )
            )
        chosen = await self.push_screen_wait(
            SelectScreen(options, title="Cancel queued message",
                         placeholder="filter queued messages…", theme=self._av_theme)
        )
        if chosen is None or isinstance(chosen, list):
            return
        if int(chosen) == -1:
            cleared = self._queue.clear()
            self._refresh_queue_panel()
            self.toast(f"Cleared {cleared} queued message(s).", variant="success")
            return
        idx = int(chosen)
        expected = items[idx][1] if 0 <= idx < len(items) else ""
        if self._cancel_queued(idx, expected):
            self.toast("Canceled queued message.", variant="success")
        else:
            self.toast(
                "That message already started or was not found.", variant="warning"
            )

    def action_edit_last_queued(self) -> None:
        """Pull the most recently queued message back into the input box to edit.

        While the engine is busy, reclaim the last pending submission, tweak it,
        and re-queue it on Enter instead of cancelling (Ctrl+X) and retyping it.
        Gated by
        :meth:`check_action` to fire only when the queue is non-empty and the input
        box is empty, so it never overwrites a draft.
        """
        text = self._queue.pop_last()
        if text is None:  # pragma: no cover - guarded by check_action
            return
        self._refresh_queue_panel()
        try:
            inp = self.query_one("#input", CommandInput)
        except Exception:  # pragma: no cover - mount/teardown window
            self._enqueue(text, priority="next")  # don't lose it
            self._refresh_queue_panel()
            return
        inp.value = text
        inp.cursor_position = len(text)
        inp.focus()
        self.toast("Pulled queued message back to edit.", variant="info")

    # ── in-place AgentView switching ──────────────────────────────────────────
    def _enter_agent_view(self, *, job_id: str = "", index: int | None = None) -> None:
        """Show the in-place sub-agent context view for *job_id* or *index*.

        Hides ``#log`` and reveals ``#agentview`` populated with the selected
        sub-agent's transcript. ``#log`` is left untouched so returning to Main
        restores the main conversation (scroll intact)."""
        records = list(self._sink.subagents()) if self._sink else []
        if not records:
            self.toast("No sub-agents dispatched yet.", variant="info")
            return
        if index is None:
            index = next((i for i, r in enumerate(records) if r.job_id == job_id), 0)
        index = max(0, min(index, len(records) - 1))
        try:
            view = self.query_one("#agentview", AgentView)
            log = self.query_one("#log", ConversationLog)
        except Exception:  # pragma: no cover - before mount
            return
        # Flip the active flag first so _sidebar_visible() / _main_width()
        # report the full-width geometry before we hide the sidebar and size
        # the transcript (MiMo parity: sub-agent view occupies the whole width).
        self._agent_view_active = True
        self._agent_phase_key = None  # restart the current-operation timer fresh
        self._apply_sidebar_visibility()
        view.set_records(records, index, width=self._main_width())
        log.add_class("-hidden")
        view.remove_class("-hidden")
        # Collapse the input region: there is nothing to type into while viewing
        # a sub-agent's context. The StatusBar footer stays and is repurposed to
        # show the open agent's live status (see _tick → _refresh_agent_status).
        self._set_input_region_hidden(True)

    def _exit_agent_view(self) -> None:
        """Return to the main conversation (``#log`` restored, scroll preserved)."""
        if not self._agent_view_active:
            return
        try:
            self.query_one("#agentview", AgentView).add_class("-hidden")
            self.query_one("#log", ConversationLog).remove_class("-hidden")
        except Exception:  # pragma: no cover - teardown race
            pass
        self._agent_view_active = False
        self._agent_phase_key = None
        # Restore the input region and reset the StatusBar back to the main
        # agent's idle/running status (it was repurposed for the sub-agent).
        self._set_input_region_hidden(False)
        status = self._status_bar()
        if status is not None:
            # Release the AgentView's single-line takeover so the main-agent
            # idle/running status renders normally again.
            status.agent_line = ""
        if status is not None and self._mode_stack.current() is not InputMode.RUNNING:
            self._set_phase("idle")
        # Restore the sidebar per the normal auto/shown/hidden rules now that
        # we are back on Main (it was force-hidden while in the sub-agent view).
        self._apply_sidebar_visibility()
        self.query_one("#input", CommandInput).focus()

    def _set_input_region_hidden(self, hidden: bool) -> None:
        """Collapse (or restore) the input frame + its spacer + queued panel.

        Used by the in-place AgentView: while a sub-agent's context is shown
        there is nothing to type into, so the prompt collapses and the StatusBar
        footer is repurposed to display the open agent's live status."""
        for wid in ("#input-wrap", "#input-spacer"):
            try:
                self.query_one(wid).set_class(hidden, "-hidden")
            except Exception:  # pragma: no cover - mount/teardown window
                pass
        # The queued panel is normally driven by queue contents; force it hidden
        # while in the agent view and let _refresh_queue_panel restore it after.
        try:
            self.query_one("#queued-panel", QueuedPanel).set_class(hidden, "-hidden")
        except Exception:  # pragma: no cover - mount/teardown window
            pass
        if not hidden:
            self._refresh_queue_panel()

    def _cycle_agent_view(self, direction: int) -> None:
        if not self._agent_view_active:
            return
        try:
            self.query_one("#agentview", AgentView).cycle(direction)
        except Exception:  # pragma: no cover
            pass

    # ── AgentView / Sidebar messages + guarded nav actions ─────────────────────
    def on_sidebar_agent_clicked(self, message: "Sidebar.AgentClicked") -> None:
        self._enter_agent_view(job_id=message.job_id)

    def on_conversation_log_agent_card_clicked(
        self, message: "ConversationLog.AgentCardClicked"
    ) -> None:
        """Clicking a sub-agent card in the main log drills into its context —
        identical to clicking the sidebar row (MiMo's clickable Task parity)."""
        self._enter_agent_view(job_id=message.job_id)

    def on_agent_view_main(self, message: "AgentView.Main") -> None:  # noqa: ARG002
        self._exit_agent_view()

    def on_agent_view_prev(self, message: "AgentView.Prev") -> None:  # noqa: ARG002
        self._cycle_agent_view(-1)

    def on_agent_view_next(self, message: "AgentView.Next") -> None:  # noqa: ARG002
        self._cycle_agent_view(1)

    def action_agent_main(self) -> None:
        self._exit_agent_view()

    def action_agent_prev(self) -> None:
        self._cycle_agent_view(-1)

    def action_agent_next(self) -> None:
        self._cycle_agent_view(1)

    # ── normal/plan/worktree interaction mode (docs/guides/using-cli-and-tui.md §3.10) ─────────
    def _plan_mode_active(self) -> bool:
        """Whether the shared ``app.extra["plan_mode"]`` flag is currently on."""
        extra = getattr(self._app_ctx, "extra", None)
        pm = extra.get("plan_mode") if isinstance(extra, dict) else None
        return bool(isinstance(pm, dict) and pm.get("active"))

    def _worktree_mode_active(self) -> bool:
        """Whether the shared ``app.extra["worktree_mode"]`` flag is currently on."""
        from autoverse.core.loop import worktree_mode

        return worktree_mode.is_active(self._require_app_ctx())

    def _worktree_branch(self) -> str | None:
        extra = getattr(self._app_ctx, "extra", None)
        wm = extra.get("worktree_mode") if isinstance(extra, dict) else None
        if isinstance(wm, dict):
            br = wm.get("branch")
            return str(br) if br else None
        return None

    def _interaction_mode(self) -> str:
        """The active interaction mode: 'normal' | 'plan' | 'worktree'."""
        if self._plan_mode_active():
            return "plan"
        if self._worktree_mode_active():
            return "worktree"
        return "normal"

    def _mode_badge(self) -> tuple[str, str]:
        """``(label, theme-role)`` for the active interaction-mode badge.

        Single source of truth for the mode's display label + colour, shared by
        the input meta line, the input frame's left bar and the per-turn footer
        so all three always read the same mode/colour (docs/guides/using-cli-and-tui.md §3.10).
        worktree carries its branch name so the isolated-write target is visible.
        """
        mode = self._interaction_mode()
        if mode == "plan":
            return "plan", "success"
        if mode == "worktree":
            return f"worktree:{self._worktree_branch() or 'wt'}", "mode_worktree"
        return "normal", "warning"

    def _apply_input_mode_class(self) -> None:
        """Toggle the ``-mode-*`` class on the input frame so its left bar
        recolours to match the active mode badge. Idempotent; a no-op before the
        frame is mounted (or in headless harnesses without ``#input-wrap``)."""
        try:
            wrap = self.query_one("#input-wrap")
        except Exception:  # pragma: no cover - mount/teardown race
            return
        mode = self._interaction_mode()
        wrap.set_class(mode == "normal", "-mode-normal")
        wrap.set_class(mode == "plan", "-mode-plan")
        wrap.set_class(mode == "worktree", "-mode-worktree")

    def _set_plan_mode(self, active: bool) -> None:
        extra = getattr(self._app_ctx, "extra", None)
        if isinstance(extra, dict):
            extra["plan_mode"] = {
                "active": active,
                "reason": "user toggled plan mode" if active else "",
            }

    async def action_cycle_mode(self, direction: str = "forward") -> None:
        """Cycle normal → plan → worktree → normal (Tab) / reverse (Shift+Tab).

        Single source of truth on ``app.extra``; the turn loop reads it next turn.
        Switching INTO/OUT OF worktree mode has side effects (build/finalize a
        worktree, switch cwd) handled by ``core.loop.worktree_mode`` so the logic
        is shared with the model tools (P1). No toast on success — the persistent
        meta badge is the feedback; worktree *failures* do toast (§3.10.4)."""
        order = ["normal", "plan", "worktree"]
        cur = self._interaction_mode()
        step = 1 if direction != "backward" else -1
        target = order[(order.index(cur) + step) % len(order)]
        if target == cur:
            return
        await self._switch_mode(cur, target)
        self._refresh_status_context()

    async def _switch_mode(self, cur: str, target: str) -> None:
        """Apply a mode transition, running worktree enter/exit as needed."""
        from autoverse.core.loop import worktree_mode

        # Leaving worktree mode: finalize. A pristine box (nothing changed, no
        # new commits) is silently discarded so Tab-in/Tab-out does not leave
        # empty worktrees or branches behind. Only prompt keep/merge/discard
        # when there is actual work to decide on. ``has_changes`` is fail-safe:
        # unknown (None) or dirty → prompt; only an explicit False takes the
        # silent cleanup path.
        if cur == "worktree":
            dirty = await worktree_mode.has_changes(self._require_app_ctx())
            if dirty is False:
                action = "discard"
            else:
                chosen = await self._ask_worktree_exit_action()
                if chosen is None:
                    # Cancelled the finalize dialog → abort the whole switch and
                    # stay in the worktree, untouched (no exit, no cwd flip, no
                    # plan toggle). Makes an accidental Tab fully recoverable.
                    return
                action = chosen
            exit_res = await worktree_mode.exit(self._require_app_ctx(), action=action)
            if not exit_res.ok:
                self.toast(f"worktree exit: {exit_res.error}", variant="warning")
            self._sync_meta_cwd()

        # Entering plan mode (mutually exclusive with worktree, already exited).
        if target == "plan":
            self._set_plan_mode(True)
        else:
            self._set_plan_mode(False)

        # Entering worktree mode: build the box (fail-closed → stay normal + toast).
        if target == "worktree":
            enter_res = await worktree_mode.enter(
                self._require_app_ctx(), reason="user switched to worktree mode"
            )
            if not enter_res.ok:
                self.toast(
                    f"cannot enter worktree mode: {enter_res.error}",
                    variant="error",
                )
                # fail-closed: remain in normal (plan already cleared above)
                return
            self._sync_meta_cwd()

    async def _ask_worktree_exit_action(self) -> str | None:
        """Prompt the user for the worktree finalize action.

        Returns ``'keep'`` / ``'merge'`` / ``'discard'``, or ``None`` when the
        user cancels (Esc). ``None`` means "abort the mode switch and stay in the
        worktree" — so an accidental Tab is fully recoverable and never forces you
        out of an in-progress box (P5). A dialog that can't even open (headless)
        falls back to the safe ``'keep'`` so the switch still completes rather
        than trapping the session.
        """
        try:
            options = [
                SelectOption(
                    title="keep",
                    value="keep",
                    description="Keep the branch and changes for manual merge / PR (safest).",
                ),
                SelectOption(
                    title="merge",
                    value="merge",
                    description="Commit and attempt merge back to base (fail-safe on conflict).",
                ),
                SelectOption(
                    title="discard",
                    value="discard",
                    description="Drop all changes and remove the worktree.",
                ),
            ]
            chosen = await self.select(
                cast(Sequence[SelectOption[object]], options),
                title="Exit worktree — finalize how?",
            )
            if chosen in ("keep", "merge", "discard"):
                return str(chosen)
            return None  # Esc/cancel → caller aborts the switch, stays in worktree
        except Exception:  # pragma: no cover - dialog unavailable (headless/tests)
            return "keep"

    def _sync_meta_cwd(self) -> None:
        """Refresh anything that displays the (possibly switched) session cwd."""
        try:
            self._refresh_sidebar()
        except Exception:  # pragma: no cover - sidebar not mounted
            pass

    def _input_is_empty(self) -> bool:
        """True when the input box has no draft text (so Tab can toggle mode)."""
        try:
            return not self.query_one("#input", CommandInput).value.strip()
        except Exception:  # pragma: no cover - before mount
            return True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:  # noqa: ARG002
        """Enable the agent-view nav bindings only while that view is shown.

        Otherwise ``escape``/``left``/``right`` must stay free for normal use
        (closing menus, cursor movement in the input box)."""
        if action in ("agent_main", "agent_prev", "agent_next"):
            return bool(self._agent_view_active)
        # Ctrl+X only acts when there is something queued; otherwise let the key
        # fall through to the input box (no accidental interception of editing).
        if action == "cancel_queued":
            return len(self._queue) > 0
        # Ctrl+E pulls the last queued message back to edit — only when there is
        # something queued AND the input box is empty, so a half-typed draft is
        # never overwritten (otherwise the key falls through to the input box).
        if action == "edit_last_queued":
            return len(self._queue) > 0 and self._input_is_empty()
        # Tab cycles interaction mode only when the input is empty; otherwise Tab
        # must fall through to TextArea indentation (docs/guides/using-cli-and-tui.md §3.10.3).
        if action == "cycle_mode":
            return self._input_is_empty()
        return True


def _coerce_int(value: object) -> int:
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


__all__ = ["AutoVerseApp"]
