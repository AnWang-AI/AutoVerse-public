"""ConversationLog — the TUI output region.

A ``VerticalScroll`` that mounts one child widget per logical block, replacing
the append-only ``RichLog``. This is what makes **interactive fold/expand**
possible (MiMo/opencode parity): long tool output and the thinking block are
mounted as Textual ``Collapsible`` widgets that the user toggles in place with
click / Enter (``▶`` ↔ ``▼``) — impossible with a flat scroll log.

All writes funnel through three methods so the sink/app never sprinkle Textual
widget calls (a single LogView seam):

* ``write(renderable)`` — a plain block (prose / tool line / footer / echo).
* ``write_markdown(text)`` — the assistant answer, via Textual's native
  ``Markdown`` widget (robust across versions; themed for the dark UI).
* ``write_collapsible(title, body, collapsed)`` — a foldable card.

Rich renderables (``Text``/ANSI) are wrapped in a Textual ``Content`` before
mounting — a bare Rich ``Text`` is not promoted to a Visual in this Textual
version (validated against the sidebar work).
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Collapsible, Static

from autoverse.frontends.repl.markdown import AutoVerseMarkdown


class RepaintOnScroll(VerticalScroll):
    """A ``VerticalScroll`` that forces a clean repaint shortly after scrolling.

    Textual composites a scroll as an INCREMENTAL row-delta against each child's
    measured height — it does not redraw the whole region, only the rows newly
    revealed. When a child's measured height differs from the rows it actually
    paints (common with ``Markdown`` and wrapped ANSI ``Text``, and worse with
    full-width CJK glyphs), that delta drifts and previously-painted rows are
    never cleared, so they reappear duplicated/ghosted as you scroll up/down.

    Forcing a full repaint once the scroll settles rebuilds the visible strips
    and clears those ghosts. The repaint is debounced (one-shot timer) so a fast
    scroll refreshes once it stops rather than on every crossed row, and it is
    only a repaint (``layout=False``) so it stays cheap.
    """

    _REPAINT_DEBOUNCE: float = 0.04  # seconds (~one frame after scroll settles)
    _repaint_timer: Timer | None = None

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        # Preserve the base scrollbar/anchor handling, then (only when a real
        # row boundary was crossed) schedule a debounced clean repaint.
        super().watch_scroll_y(old_value, new_value)
        if round(old_value) != round(new_value):
            self._schedule_repaint()

    def _schedule_repaint(self) -> None:
        if self._repaint_timer is not None:
            self._repaint_timer.stop()
        self._repaint_timer = self.set_timer(
            self._REPAINT_DEBOUNCE, self._do_repaint
        )

    def _do_repaint(self) -> None:
        self._repaint_timer = None
        # Repaint only (no relayout): re-render the visible strips against the
        # current scroll offset, clearing any ghosted/duplicated rows left by
        # the incremental scroll composite.
        self.refresh(repaint=True, layout=False)


class AgentCard(Static):
    """A clickable sub-agent card in the main conversation log.

    Clicking it posts :class:`ConversationLog.AgentCardClicked` carrying the
    ``job_id`` so the app can drill into that sub-agent's context — the exact
    parity of MiMo's clickable ``Task`` inline-tool (and of the sidebar's
    :class:`~autoverse.frontends.repl.sidebar.AgentRow`). The card is updated in
    place (``update``) as the sub-agent runs, so its identity stays stable across
    the 10 Hz live-elapsed tick and a click (down+up spanning a tick) is not lost."""

    def __init__(self, job_id: str, renderable: object, **kwargs: object) -> None:
        super().__init__(renderable, **kwargs)  # type: ignore[arg-type]
        self.job_id = job_id

    def on_click(self) -> None:
        self.post_message(ConversationLog.AgentCardClicked(self.job_id))


class ConversationLog(RepaintOnScroll):
    """Scrollable, widget-per-block conversation output region."""

    class AgentCardClicked(Message):
        """Posted when a sub-agent card in the log is clicked. Carries ``job_id``
        so the app can drill into that sub-agent's context (MiMo parity)."""

        def __init__(self, job_id: str) -> None:
            super().__init__()
            self.job_id = job_id

    # The single in-flight streaming answer widget (the assistant's current
    # paragraph). ``None`` between turns. While streaming it is a lightweight
    # ``Static`` showing the raw text — updating a ``Static`` only re-renders one
    # line-set, with NO markdown parse and NO child-widget churn. The fully
    # formatted ``Markdown`` is mounted ONCE at ``end_stream`` (a single parse
    # for the whole answer). This avoids Textual ``Markdown.update``'s O(n²)
    # re-parse-and-remount-every-delta cost (the prior streaming hot path).
    _stream_md: Static | None = None
    # Latest complete accumulated prose (the sink sends the full string each
    # delta). Held so ``end_stream`` can render the finished Markdown once.
    _stream_text: str = ""

    # Live-stream coalescing (smoothness): even the cheap ``Static.update`` need
    # not run on every delta. A fast backend emits dozens of deltas/sec, so we
    # keep only the latest snapshot (``_stream_pending``) and flush it to the
    # widget at most once per ``_STREAM_FLUSH_INTERVAL`` via a one-shot timer.
    # The viewer still sees the answer grow continuously, but render frequency
    # is capped regardless of token rate. ``end_stream`` forces a final flush so
    # the committed widget always shows the complete answer.
    _STREAM_FLUSH_INTERVAL: float = 0.05  # seconds (~20 fps)
    _stream_pending: str | None = None
    _stream_timer: Timer | None = None

    # Live sub-agent cards keyed by dispatch/job id. A dispatched sub-agent
    # mounts ONE card here on its first SUBAGENT_ACTIVITY and then updates it in
    # place (turn / elapsed / activity → terminal summary) — so the user sees it
    # running in the main conversation from dispatch and the card becomes its own
    # completion card, never a second block. Set lazily (avoids a mutable class
    # default shared across instances).
    _agent_cards: "dict[str, AgentCard] | None" = None

    # The single in-flight live turn-footer (the muted meta line — model ·
    # elapsed · ↑↓tokens · cost). Mounted at the start of a turn so it pins to
    # the bottom of the current exchange and refreshes every tick; ``None``
    # between turns. While set, EVERY other block is mounted *before* it so the
    # footer stays the last child (glued to the bottom of the reply); the app
    # mutates it in place via ``update_footer`` and freezes it with
    # ``end_footer`` (which drops the reference, leaving the widget in place as
    # the exchange's closing line — the next turn mounts a fresh one).
    _live_footer: Static | None = None
    # Temporary bottom padding used while an overlay choice menu is open. Because
    # overlays do not participate in layout, the last log block would otherwise
    # sit underneath the menu after scroll_end.
    _bottom_spacer: Static | None = None

    # Live "running" tool cards keyed by ``tool_use_id`` (P2 — transient tool
    # progress, mirroring the sub-agent live card). A tool that is still
    # executing after ``_TOOL_DEBOUNCE`` mounts ONE muted "running" card in the
    # main log so the user sees what's in flight (``⠋ Bash: npm test``) in the
    # right chronological place — instead of the view sitting blank until the
    # result lands. Near-instant inline tools (Read/Glob) finish before the
    # debounce fires, so they never flicker a card. ``end_tool`` cancels a
    # still-pending mount or removes the mounted card; the sink then renders the
    # final TOOL_END card, which lands in the running card's place (it was the
    # last block, so chronological order is preserved). The spinner glyph is
    # advanced in place by ``tick_tool_spinner`` from the app's 10 Hz tick.
    _TOOL_DEBOUNCE: float = 0.18  # seconds
    _tool_cards: "dict[str, Static] | None" = None
    _tool_pending: "dict[str, Timer] | None" = None
    _tool_bodies: "dict[str, Text] | None" = None
    _tool_frame: str = "⠋"

    def _scroll_to_end(self) -> None:
        # Defer so the just-mounted child is laid out before we scroll.
        self.call_after_refresh(self.scroll_end, animate=False)

    def _mount_block(self, widget: object) -> None:
        """Mount a block, keeping the live turn-footer (if any) pinned last.

        New conversation blocks (prose / tool cards / sub-agent cards / the
        streaming answer) must appear ABOVE the live footer so the footer stays
        glued to the bottom of the current reply. When a footer is mounted we
        insert before it; otherwise we append normally."""
        if self._live_footer is not None and self._live_footer.parent is self:
            self.mount(widget, before=self._live_footer)  # type: ignore[arg-type]
        elif self._bottom_spacer is not None and self._bottom_spacer.parent is self:
            self.mount(widget, before=self._bottom_spacer)  # type: ignore[arg-type]
        else:
            self.mount(widget)  # type: ignore[arg-type]
        self._scroll_to_end()

    def reserve_bottom_space(self, rows: int) -> None:
        """Keep recent log content above a bottom-docked overlay menu."""
        self.clear_bottom_space()
        rows = max(0, rows)
        if rows == 0:
            return
        spacer = Static("")
        spacer.styles.height = rows
        self._bottom_spacer = spacer
        self.mount(spacer)
        self._scroll_to_end()

    def clear_bottom_space(self) -> None:
        spacer = self._bottom_spacer
        self._bottom_spacer = None
        if spacer is not None and spacer.parent is self:
            spacer.remove()

    # ── live turn footer (per-exchange meta line) ────────────────────────────
    def begin_footer(self, renderable: RenderableType) -> None:
        """Mount the live turn-footer at the bottom of the log (idempotent).

        Called when a turn starts so the meta line is visible from the first
        moment and refreshes in place as the reply streams; a second call
        without ``end_footer`` just updates the existing widget."""
        if self._live_footer is not None and self._live_footer.parent is self:
            self.update_footer(renderable)
            return
        foot = Static(self._content(renderable))
        self._live_footer = foot
        self.mount(foot)
        self._scroll_to_end()

    def update_footer(self, renderable: RenderableType) -> None:
        """Refresh the live footer's content in place (no-op when none is set)."""
        if self._live_footer is not None and self._live_footer.parent is self:
            self._live_footer.update(self._content(renderable))

    def end_footer(self) -> None:
        """Freeze the live footer: drop the reference so it stays in place as the
        exchange's closing line and the next turn mounts a fresh one."""
        self._live_footer = None

    # ── live streaming answer ────────────────────────────────────────────────
    def begin_stream(self, text: str = "") -> None:
        """Mount a lightweight ``Static`` that subsequent ``update_stream`` calls
        grow in place (cheap re-render, no markdown parse). Idempotent: a second
        call without ``end_stream`` keeps the existing widget (the sink owns the
        lifecycle). The formatted ``Markdown`` is produced once at ``end_stream``."""
        if self._stream_md is not None:
            self.update_stream(text)
            return
        self._stream_text = text
        w = Static(self._stream_content(text))
        self._stream_md = w
        self._stream_pending = None  # the initial text is already rendered
        self._mount_block(w)

    def update_stream(self, text: str) -> None:
        """Record the latest full accumulated prose and ensure a flush is
        scheduled. The (now cheap) ``Static.update`` is still coalesced to at
        most once per ``_STREAM_FLUSH_INTERVAL`` so a fast backend's delta storm
        does not flood the message pump."""
        if self._stream_md is None:
            self.begin_stream(text)
            return
        # Keep only the latest snapshot; the pending timer renders whatever is
        # current when it fires (intermediate snapshots are intentionally
        # dropped — only the newest matters for a full-string re-render).
        self._stream_text = text
        self._stream_pending = text
        if self._stream_timer is None:
            # One-shot: schedule a single flush. After it fires it sets the
            # timer back to ``None``; the next ``update_stream`` re-arms it, so
            # an idle stream stops scheduling work.
            self._stream_timer = self.set_timer(
                self._STREAM_FLUSH_INTERVAL, self._flush_stream
            )

    def _flush_stream(self) -> None:
        """Render the latest pending snapshot into the live ``Static`` (a plain
        text re-render — no markdown parse, no child-widget churn)."""
        self._stream_timer = None
        w = self._stream_md
        text = self._stream_pending
        if w is None or text is None:
            return
        self._stream_pending = None
        # We deliberately do NOT scroll on every flush: the single scroll at
        # ``begin_stream`` plus boundary ``_scroll_to_end`` keep the viewport
        # pinned without flooding the message pump.
        w.update(self._stream_content(text))

    def end_stream(self) -> None:
        """Finalize the in-flight answer: replace the raw-text streaming
        ``Static`` with the fully formatted ``Markdown`` (a single parse for the
        whole answer), then drop the reference so the next turn starts fresh."""
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        w = self._stream_md
        text = self._stream_text
        self._stream_pending = None
        self._stream_md = None
        self._stream_text = ""
        if w is None:
            return
        # Commit the formatted answer in the streaming widget's place (it was
        # the last content block before the live footer, so mounting after it
        # preserves chronological order) and drop the raw-text placeholder.
        if w.parent is self:
            self.mount(AutoVerseMarkdown(text), after=w)
            w.remove()

    @staticmethod
    def _stream_content(text: str):  # type: ignore[no-untyped-def]
        """A literal (un-parsed) ``Content`` for the streaming Static — answer
        text may contain ``[`` brackets that must NOT be read as console markup."""
        from textual.content import Content

        return Content(text)

    @staticmethod
    def _content(renderable: RenderableType):  # type: ignore[no-untyped-def]
        """Wrap a Rich renderable / str into a Textual Content for a Static.

        A ``str`` is parsed as **Textual console markup** (``[dim]…[/dim]`` — the
        app's status/queued lines); a Rich ``Text`` (from renderers, may carry
        ANSI styling) is wrapped via ``from_rich_text``."""
        from textual.content import Content

        if isinstance(renderable, str):
            return Content.from_markup(renderable)
        if isinstance(renderable, Text):
            return Content.from_rich_text(renderable)
        return renderable  # already a Visual / Content

    def write(self, renderable: RenderableType, *, classes: str = "") -> None:
        """Mount a plain block (prose / tool line / footer / echo / rule).

        ``classes`` attaches CSS classes to the mounted ``Static`` — used to
        draw the MiMo-style left accent bar + grey background block on user
        echoes (``user-echo``) and tool blocks (``tool-block``) via CSS, rather
        than baking ``▌``/``┃`` glyphs into the text.
        """
        self._mount_block(Static(self._content(renderable), classes=classes or None))

    def write_markdown(self, text: str) -> None:
        """Mount the assistant answer as a native Textual Markdown widget."""
        self._mount_block(AutoVerseMarkdown(text))

    def write_collapsible(
        self,
        title: str,
        body: RenderableType,
        *,
        collapsed: bool = True,
        classes: str = "",
    ) -> None:
        """Mount a foldable card: ``title`` (console markup) + ``body`` (the
        full content, shown when expanded). Toggled in place via click / Enter.

        ``classes`` (e.g. ``tool-block``) styles the card with the MiMo left
        accent bar + grey background via CSS."""
        self._mount_block(
            Collapsible(
                Static(self._content(body)),
                title=title,
                collapsed=collapsed,
                collapsed_symbol="+",
                expanded_symbol="-",
                classes=classes or None,
            )
        )

    def write_or_update_agent(
        self, job_id: str, renderable: RenderableType, *, done: bool = False
    ) -> None:
        """Mount (first call) or update-in-place (later calls) the #log card for
        one dispatched sub-agent, keyed by ``job_id``.

        The first SUBAGENT_ACTIVITY mounts a card so the user sees the sub-agent
        the moment it is dispatched; subsequent activity/terminal events update
        the SAME widget (turn/elapsed → completion summary) instead of appending
        a new block — exactly one card per dispatch, growing live like the main
        conversation's streaming answer. ``done`` releases the key so a stray
        late event cannot resurrect a finalized card."""
        cards = self._agent_cards
        if cards is None:
            cards = self._agent_cards = {}
        card = cards.get(job_id)
        if card is None:
            # Clickable card (AgentCard) → drills into the sub-agent on click,
            # matching the sidebar row and MiMo's clickable Task inline-tool.
            card = AgentCard(job_id, self._content(renderable))
            cards[job_id] = card
            self._mount_block(card)
        else:
            card.update(self._content(renderable))
        if done:
            cards.pop(job_id, None)

    # ── live "running" tool cards ────────────────────────────────────────────
    def begin_tool(self, tool_use_id: str, body: Text) -> None:
        """Schedule a live "running" card for a tool, keyed by ``tool_use_id``.

        Debounced: the card is mounted only if the tool is still running after
        ``_TOOL_DEBOUNCE`` (``end_tool`` before then cancels it), so a near-
        instant inline tool never flickers a card on screen. ``body`` is the
        already-styled label (the sink owns theming); the spinner glyph is
        prepended here and animated by ``tick_tool_spinner``. Idempotent per id.
        """
        if not tool_use_id:
            return
        cards = self._tool_cards
        if cards is not None and tool_use_id in cards:
            return
        pend = self._tool_pending
        if pend is None:
            pend = self._tool_pending = {}
        if tool_use_id in pend:
            return
        bodies = self._tool_bodies
        if bodies is None:
            bodies = self._tool_bodies = {}
        bodies[tool_use_id] = body
        pend[tool_use_id] = self.set_timer(
            self._TOOL_DEBOUNCE, lambda: self._mount_tool(tool_use_id)
        )

    def _mount_tool(self, tool_use_id: str) -> None:
        """Mount the running card once the debounce fires (tool still in flight)."""
        pend = self._tool_pending
        if pend is not None:
            pend.pop(tool_use_id, None)
        bodies = self._tool_bodies or {}
        body = bodies.get(tool_use_id)
        if body is None:
            return
        cards = self._tool_cards
        if cards is None:
            cards = self._tool_cards = {}
        card = Static(self._content(self._spinner_text(body)), classes="tool-running")
        cards[tool_use_id] = card
        self._mount_block(card)

    def tick_tool_spinner(self, frame: str) -> None:
        """Advance the leading spinner glyph on every mounted running card.

        Called from the app's periodic tick so the card reads as alive (the
        same cadence as the StatusBar spinner). No-op when nothing is running."""
        self._tool_frame = frame
        cards = self._tool_cards
        if not cards:
            return
        bodies = self._tool_bodies or {}
        for tid, card in cards.items():
            body = bodies.get(tid)
            if body is not None and card.parent is self:
                card.update(self._content(self._spinner_text(body)))

    def end_tool(self, tool_use_id: str) -> None:
        """Cancel a pending mount or remove the mounted running card for a tool.

        After this the sink renders the tool's final TOOL_END card; since the
        running card was the last block, the final card lands in its place."""
        if not tool_use_id:
            return
        pend = self._tool_pending
        if pend is not None:
            timer = pend.pop(tool_use_id, None)
            if timer is not None:
                timer.stop()
        bodies = self._tool_bodies
        if bodies is not None:
            bodies.pop(tool_use_id, None)
        cards = self._tool_cards
        if cards is not None:
            card = cards.pop(tool_use_id, None)
            if card is not None and card.parent is self:
                card.remove()

    def clear_running_tools(self) -> None:
        """Drop every in-flight running card (turn boundary / interrupt safety).

        On a normal turn each tool's ``end_tool`` already removed its card; this
        clears any left dangling by an interrupted turn (whose tools never emit
        TOOL_END) so a frozen spinner never lingers into the next exchange."""
        pend = self._tool_pending
        if pend:
            for timer in pend.values():
                timer.stop()
            pend.clear()
        cards = self._tool_cards
        if cards:
            for card in list(cards.values()):
                if card.parent is self:
                    card.remove()
            cards.clear()
        if self._tool_bodies:
            self._tool_bodies.clear()

    def _spinner_text(self, body: Text) -> Text:
        """Prepend the current spinner glyph to a tool's styled label body."""
        return Text(f"{self._tool_frame} ") + body

    def clear(self) -> object:
        """Remove every block (for /clear and session-switch replay).

        Returns the ``AwaitComplete`` from ``remove_children`` so callers that
        immediately repaint (history replay) can ``await log.clear()`` and start
        from a truly empty tree — without that await the old session's widgets are
        still mounted while the new ones are written (session-switch bug)."""
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        if self._repaint_timer is not None:
            self._repaint_timer.stop()
            self._repaint_timer = None
        self._stream_pending = None
        self._stream_md = None
        self._stream_text = ""
        self._live_footer = None
        if self._agent_cards is not None:
            self._agent_cards.clear()
        if self._tool_pending:
            for timer in self._tool_pending.values():
                timer.stop()
            self._tool_pending.clear()
        if self._tool_cards is not None:
            self._tool_cards.clear()
        if self._tool_bodies is not None:
            self._tool_bodies.clear()
        return self.remove_children()


__all__ = ["ConversationLog", "AgentCard", "RepaintOnScroll"]
