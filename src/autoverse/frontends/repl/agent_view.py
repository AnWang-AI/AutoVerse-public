"""AgentView — an in-place sub-agent context view (MiMo route-driven parity).

MiMo-Code's TUI lets you click a sub-agent and the *main session view itself*
swaps to that sub-agent's context slice (``route.navigate({agentID})``), with a
clickable ``Main / Prev / Next`` footer to cycle or return. AutoVerse keeps each
dispatched sub-agent's raw event stream in a :class:`SubAgentRecord` (captured
via ``SUBAGENT_CHILD``; see ``tui_sink``).

This widget is the AutoVerse adaptation: rather than a modal overlay, it is a
sibling region of the main ``ConversationLog`` inside ``#main``. The app hides
``#log`` and shows this view when the user enters a sub-agent (click in the
sidebar / ``ctrl+r`` picker / footer Prev-Next). Because ``#log`` is never
mutated, returning to Main restores the main conversation exactly — scroll
position intact ("snapshot+restore" semantics).

Pure presentation (P3): it receives already-collected records and the shared
renderer; no engine/store access. ``render_transcript`` turns the retained
events into display blocks lazily, only for the record currently shown.
"""

from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Collapsible, Static

from autoverse.frontends.repl.conversation_log import RepaintOnScroll
from autoverse.frontends.repl.markdown import AutoVerseMarkdown
from autoverse.frontends.repl.renderer import (
    RenderContext,
    SUBAGENT_ROLE,
    TranscriptBlock,
    render_transcript_blocks,
)
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.frontends.repl.tui_sink import (
    SubAgentRecord,
    subagent_meta,
)

# status → leading glyph (mirrors sidebar._STATUS_GLYPH glyphs).
_STATUS_GLYPH: dict[str, str] = {
    "running": "●", "pending": "○", "done": "✓", "failed": "✗", "cancelled": "⊘",
}


def _fmt_elapsed(ms: float) -> str:
    s = ms / 1000.0
    if s <= 0:
        return ""
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


class _FooterButton(Static):
    """A clickable footer label that posts *message_factory()* on click."""

    def __init__(
        self, label: "str | Text", message_factory: Callable[[], Message],
        **kwargs: object,
    ) -> None:
        super().__init__(label, **kwargs)  # type: ignore[arg-type]
        self._make = message_factory

    def on_click(self) -> None:
        self.post_message(self._make())


class AgentView(Vertical):
    """In-place sub-agent transcript view with a clickable Main/Prev/Next footer.

    The app calls :meth:`set_records` to load the session's sub-agent records and
    select one by index; :meth:`refresh_body` re-renders the current record's
    transcript (used by the spinner tick to follow a still-running agent).
    """

    # ── messages the app handles to drive view switching ──────────────────────
    class Main(Message):
        """Return to the main conversation."""

    class Prev(Message):
        """Cycle to the previous sub-agent."""

    class Next(Message):
        """Cycle to the next sub-agent."""

    def __init__(self, *, theme: Theme | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._theme = theme or DEFAULT_THEME
        self._records: list[SubAgentRecord] = []
        self._index = 0
        self._width = 100
        # Number of events rendered for the current record — lets the tick skip
        # a re-render when a running agent has produced nothing new.
        self._rendered_n = -1
        # Incremental-render state: the transcript blocks currently mounted (in
        # body order, AFTER the optional task echo), and whether an echo card /
        # placeholder note occupies a leading/sole slot. ``refresh_body`` diffs
        # new blocks against this cache and only re-mounts the changed tail, so a
        # live-streaming agent doesn't tear down + rebuild the whole transcript
        # (Markdown/Collapsible widgets) every 100 ms tick — the cause of the
        # UI freeze when a long subagent context was open.
        self._block_cache: list[TranscriptBlock] = []
        self._echo_n = 0          # 1 if a task user-echo card leads the body
        self._note_shown = False  # True while the "(running…)" placeholder is up
        # The usage meta footer, mounted as the LAST child of the body so it
        # scrolls inline at the end of the transcript (parity with the main
        # conversation's per-turn footer) rather than pinned outside the scroll
        # region. Hidden (``-empty`` class) until the agent produces a USAGE.
        self._meta_widget: Static | None = None

    # ── composition ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static("", id="agentview-title")
        # The usage meta footer is NOT mounted here: it lives INSIDE the body as
        # its last child (see _mount_meta) so it scrolls inline at the end of the
        # transcript — mirroring the main conversation's per-turn footer — instead
        # of staying pinned outside the scroll region. The nav footer below stays
        # pinned (it is an action bar, not content).
        yield RepaintOnScroll(id="agentview-body")
        with Horizontal(id="agentview-footer"):
            yield _FooterButton(
                Text.from_markup("Main [dim]Esc[/dim]"),
                lambda: AgentView.Main(),
                id="agentview-main",
            )
            yield _FooterButton(
                Text.from_markup("Prev [dim]←[/dim]"),
                lambda: AgentView.Prev(),
                id="agentview-prev",
            )
            yield _FooterButton(
                Text.from_markup("Next [dim]→[/dim]"),
                lambda: AgentView.Next(),
                id="agentview-next",
            )
            yield Static("", id="agentview-pos")

    # ── state ──────────────────────────────────────────────────────────────
    @property
    def current(self) -> SubAgentRecord | None:
        if not self._records:
            return None
        return self._records[self._index]

    def set_records(
        self,
        records: list[SubAgentRecord],
        index: int,
        *,
        width: int | None = None,
    ) -> None:
        """Load *records*, select *index*, and render that record."""
        self._records = list(records)
        self._index = max(0, min(index, len(records) - 1)) if records else 0
        if width is not None:
            self._width = max(40, width)
        self._rendered_n = -1
        self._refresh_all()

    def set_width(self, width: int) -> None:
        """Update the render width (e.g. after a terminal resize) and re-render.

        The transcript folds/wraps at ``self._width``; a stale width after a
        resize leaves every mounted block's measured height out of sync with the
        rows it paints (the "duplicated rows when scrolling" artifact). A real
        change forces a full re-render so heights are recomputed at the new
        width; an unchanged width is a no-op."""
        new = max(40, width)
        if new == self._width:
            return
        self._width = new
        self._rendered_n = -1
        self._refresh_all()

    def cycle(self, direction: int) -> None:
        """Move the selection by *direction* (+1 next, -1 prev), wrapping."""
        if len(self._records) <= 1:
            return
        self._index = (self._index + direction) % len(self._records)
        self._rendered_n = -1
        self._refresh_all()

    def refresh_body(self) -> None:
        """Incrementally update the current record's transcript if it grew.

        Called by the app's 10 Hz tick so a still-running agent's context updates
        live. This must be CHEAP: instead of tearing down and rebuilding every
        block widget (Markdown/Collapsible — ~2 ms each, so a freeze for a long
        transcript), it diffs the new blocks against the mounted ones and only
        re-mounts the changed tail. Streaming text only mutates the last block
        and appends new ones, so each tick touches just a widget or two.
        """
        rec = self.current
        if rec is None:
            return
        if len(rec.events) == self._rendered_n:
            return
        self._rendered_n = len(rec.events)
        self._update_title(rec)
        self._update_meta(rec)
        try:
            body = self.query_one("#agentview-body", VerticalScroll)
        except Exception:  # pragma: no cover - before mount
            return
        new_blocks = render_transcript_blocks(rec.events, self._ctx())
        # If the placeholder note is still up (no blocks yet) or there were no
        # blocks before, fall back to a full body render — simplest correct path
        # for the empty→first-content transition.
        if self._note_shown or not self._block_cache:
            self._render_body(rec)
            return
        if not new_blocks:
            return
        # Diff against the mounted blocks. Keep the byte-identical leading prefix.
        common = 0
        for old, new in zip(self._block_cache, new_blocks):
            if old != new:
                break
            common += 1
        if common == len(new_blocks) and len(new_blocks) == len(self._block_cache):
            return  # nothing actually changed

        # Block widgets live AFTER the optional echo card and BEFORE the trailing
        # inline meta footer — slice both ends off so the diff only touches real
        # transcript blocks (the meta is updated separately and must stay last).
        children = list(body.children)
        meta = self._meta_widget if self._meta_widget in children else None
        end = children.index(meta) if meta is not None else len(children)
        widgets = children[self._echo_n:end]
        i = common
        # While the diverging block is the SAME kind as the mounted one, it only
        # GREW (streaming) — update the widget's content IN PLACE. This is the
        # critical fix: re-mounting a Markdown/Collapsible every 10 Hz tick (the
        # last, still-streaming block) is what caused the flicker + freeze. An
        # in-place update is cheap and keeps the widget identity stable.
        while (
            i < len(new_blocks)
            and i < len(widgets)
            and i < len(self._block_cache)
            and self._same_kind(self._block_cache[i], new_blocks[i])
        ):
            self._update_widget(widgets[i], new_blocks[i])
            i += 1
        # From the first STRUCTURAL change (different kind / new / removed) on:
        # drop the stale tail widgets and mount the new tail blocks.
        for w in widgets[i:]:
            w.remove()
        for block in new_blocks[i:]:
            widget = self._mount_widget(block)
            # Keep the inline meta footer pinned last: insert new tail blocks
            # before it (append normally only if the meta isn't mounted yet).
            if meta is not None:
                body.mount(widget, before=meta)
            else:
                body.mount(widget)
        self._block_cache = new_blocks

    @staticmethod
    def _same_kind(a: TranscriptBlock, b: TranscriptBlock) -> bool:
        """True if *a* and *b* map to the same widget that can be updated in
        place (only the text differs). Collapsibles must also share title / class
        / body kind so the foldable identity is preserved."""
        if a.kind != b.kind:
            return False
        if a.kind == "collapsible":
            return (
                a.title == b.title
                and a.css_class == b.css_class
                and a.thinking_body == b.thinking_body
            )
        return True

    def _update_widget(self, widget, block: TranscriptBlock) -> None:  # type: ignore[no-untyped-def]
        """Update an already-mounted block widget's content in place (no remount)."""
        if block.kind == "markdown":
            widget.update(block.text)  # Markdown.update(text)
        elif block.kind == "thinking":
            widget.update(Text(block.text, style=self._theme.role("text_muted")))
        elif block.kind == "collapsible":
            # The foldable body is the Collapsible's inner Static — update it,
            # leaving the title / collapsed state untouched.
            try:
                inner = widget.query_one(Static)
            except Exception:  # pragma: no cover - unexpected structure
                return
            if block.thinking_body:
                inner.update(Content.from_rich_text(
                    Text(block.text, style=self._theme.role("text_muted"))))
            else:
                inner.update(Content.from_rich_text(Text.from_ansi(block.text)))
        else:  # ansi
            widget.update(Text.from_ansi(block.text))

    # ── rendering ────────────────────────────────────────────────────────────
    def _ctx(self) -> RenderContext:
        return RenderContext(theme=self._theme, ansi=True, width=self._width)

    def _refresh_all(self) -> None:
        rec = self.current
        self._update_title(rec)
        self._update_pos()
        self._update_meta(rec)
        self._render_body(rec)

    def _meta_text(
        self, rec: SubAgentRecord | None, *, elapsed_ms_override: float | None = None
    ) -> Text:
        """The usage meta line for *rec*, formatted like the main turn footer:
        ``▪ {model} · {time} · ↑{in} ↓{out} · cache {N}% · ${cost}``.

        Returns an empty Text when there is no record / no USAGE yet, so the row
        collapses to nothing (parity with the main footer only appearing after a
        turn closes).

        ``elapsed_ms_override`` lets the live tick substitute a smooth, monotonic
        elapsed for a still-running subagent (``rec.elapsed_ms`` only advances at
        child-turn boundaries, so the timer would otherwise jump in big steps —
        unlike the main footer's 10 Hz timer)."""
        from autoverse.frontends.repl.renderer import _kfmt, format_duration

        if rec is None:
            return Text("")
        meta = subagent_meta(rec)
        if not meta.has_usage:
            return Text("")
        t = self._theme
        accent = t.role("accent")
        muted = t.role("text_muted")
        dim = t.role("text_dim")
        elapsed_ms = (
            elapsed_ms_override if elapsed_ms_override is not None else meta.elapsed_ms
        )
        parts: list[tuple[str, str]] = []
        if meta.model:
            parts.append((meta.model, muted))
        if elapsed_ms >= 1:
            parts.append((format_duration(elapsed_ms), muted))
        if meta.input_tokens or meta.output_tokens:
            parts.append(
                (f"↑{_kfmt(meta.input_tokens)} ↓{_kfmt(meta.output_tokens)}", muted)
            )
        if meta.cum_cache_read or meta.cum_cache_write:
            parts.append((f"cache {round(meta.cache_hit_rate * 100)}%", muted))
        if meta.cost_usd:
            parts.append((f"${meta.cost_usd:.4f}", muted))
        row = Text()
        row.append("▪ ", style=accent)
        for i, (txt, style) in enumerate(parts):
            if i:
                row.append(" · ", style=dim)
            row.append(txt, style=style)
        return row

    def _update_meta(self, rec: SubAgentRecord | None) -> None:
        """Refresh the inline meta footer's content in place. Hides it (``-empty``
        → ``display:none``) while there is no usage so it doesn't occupy a blank
        line at the end of the transcript."""
        meta = self._meta_widget
        if meta is None:
            return
        text = self._meta_text(rec)
        meta.update(text)
        meta.set_class(not text.plain.strip(), "-empty")

    def refresh_meta_elapsed(self, live_elapsed_ms: float | None) -> None:
        """Re-render ONLY the inline meta footer's timer each tick.

        ``live_elapsed_ms`` is a smooth monotonic elapsed for a RUNNING subagent
        (so the timer advances every tick, parity with the main footer); pass
        ``None`` for a finished subagent — the footer then shows the authoritative
        frozen ``rec.elapsed_ms`` reported at completion (a cheap one-line update,
        not a transcript rebuild)."""
        meta = self._meta_widget
        rec = self.current
        if meta is None or rec is None:
            return
        text = self._meta_text(rec, elapsed_ms_override=live_elapsed_ms)
        meta.update(text)
        meta.set_class(not text.plain.strip(), "-empty")

    def _mount_meta(self, rec: SubAgentRecord | None) -> None:
        """Mount the usage footer as the LAST child of the body so it scrolls
        inline at the end of the transcript (parity with the main conversation).
        Called by the full-render path after the transcript blocks are mounted."""
        try:
            body = self.query_one("#agentview-body", VerticalScroll)
        except Exception:  # pragma: no cover - before mount
            return
        text = self._meta_text(rec)
        # A CSS CLASS (not an id): a full re-render's ``remove_children`` is
        # async, so the old footer may still be detaching when the new one mounts
        # — a fixed id would raise DuplicateIds. Classes may repeat, so the brief
        # overlap is harmless and the stale widget finishes detaching afterwards.
        meta = Static(text, classes="agentview-meta")
        if not text.plain.strip():
            meta.add_class("-empty")
        body.mount(meta)
        self._meta_widget = meta

    def _title_text(
        self, rec: SubAgentRecord | None, *, elapsed_ms_override: float | None = None
    ) -> Text:
        if rec is None:
            return Text("Subagents (none)", style="bold")
        glyph = _STATUS_GLYPH.get(rec.status, "●")
        meta: list[str] = []
        model = rec.model.strip() or subagent_meta(rec).model
        if model:
            meta.append(model)
        if rec.tool_count:
            meta.append(f"{rec.tool_count} toolcalls")
        # Like the inline footer, a RUNNING agent's timer is fed a smooth monotonic
        # elapsed (rec.elapsed_ms only advances at child-turn boundaries → jumpy);
        # a finished one uses the frozen rec.elapsed_ms.
        elapsed = _fmt_elapsed(
            elapsed_ms_override if elapsed_ms_override is not None else rec.elapsed_ms
        )
        if elapsed:
            meta.append(elapsed)
        suffix = f"  ·  {' · '.join(meta)}" if meta else ""
        # The [subagent_type] segment is tinted with the SAME per-type role as
        # the agent's line in the main conversation (`_render_subagent`) and its
        # sidebar row (`_agent_row_markup`), via the shared SUBAGENT_ROLE map —
        # so a drilled-in sub-agent's title reads the same colour as its card
        # elsewhere. The glyph + status text stay neutral so the type tint is
        # the colour cue and the status glyph (●○✓✗⊘) carries the state.
        t = Text()
        t.append(f"{glyph} ", style="bold")
        type_role = SUBAGENT_ROLE.get(rec.subagent_type, "accent")
        t.append(f"[{rec.subagent_type}]", style=f"bold {self._theme.role(type_role)}")
        t.append(f"  {rec.status}{suffix}", style="bold")
        # Only show the description sub-line when it adds information beyond the
        # task echo card mounted below. At dispatch the description is seeded with
        # ``req.task[:80]`` (a prefix of the full task), so rendering it here would
        # duplicate the prompt: the title shows the first 80 chars and the echo
        # card right beneath shows the whole thing. Suppress that prefix case.
        desc = rec.description.strip()
        task = rec.task.strip()
        if desc and not (task and task.startswith(desc)):
            t.append(f"\n{desc}", style="dim")
        return t

    def _update_title(self, rec: SubAgentRecord | None) -> None:
        try:
            self.query_one("#agentview-title", Static).update(self._title_text(rec))
        except Exception:  # pragma: no cover - before mount
            pass

    def refresh_title_elapsed(self, live_elapsed_ms: float | None) -> None:
        """Re-render the title's timer each tick with a live elapsed override so a
        running subagent's title clock advances smoothly (parity with the inline
        footer / status bar). ``None`` keeps the frozen ``rec.elapsed_ms``."""
        rec = self.current
        if rec is None:
            return
        try:
            self.query_one("#agentview-title", Static).update(
                self._title_text(rec, elapsed_ms_override=live_elapsed_ms)
            )
        except Exception:  # pragma: no cover - before mount
            pass

    def _update_pos(self) -> None:
        n = len(self._records)
        pos = f"{self._index + 1} of {n}" if n else "0 of 0"
        try:
            self.query_one("#agentview-pos", Static).update(
                Text(pos, style="dim")
            )
        except Exception:  # pragma: no cover - before mount
            pass

    def _render_body(self, rec: SubAgentRecord | None) -> None:
        """Full (re)render — used on initial entry and prev/next cycling. The live
        tick uses the incremental :meth:`refresh_body` instead. Resets the cache
        state (``_block_cache`` / ``_echo_n`` / ``_note_shown``) that the
        incremental path diffs against."""
        try:
            body = self.query_one("#agentview-body", VerticalScroll)
        except Exception:  # pragma: no cover - before mount
            return
        body.remove_children()
        self._block_cache = []
        self._echo_n = 0
        self._note_shown = False
        self._meta_widget = None  # removed by remove_children; re-mounted below
        if rec is None:
            body.mount(Static(Text("No sub-agents in this session.", style="dim")))
            self._rendered_n = 0
            self._mount_meta(rec)
            return
        self._rendered_n = len(rec.events)
        # Echo the dispatch task as the first card — the sub-agent's "user
        # prompt", styled exactly like the main conversation's user-echo
        # (amber left bar + grey wash via the #agentview-body > .user-echo CSS
        # rule). Mirrors AutoVerseApp._echo_user: the user message is a
        # presentation-layer injection, NOT a stream event, on both paths.
        if rec.task.strip():
            text_role = self._theme.role("text")
            row = Text()
            for i, ln in enumerate(rec.task.splitlines() or [""]):
                if i:
                    row.append("\n")
                row.append(ln, style=text_role)
            body.mount(Static(row, classes="user-echo"))
            self._echo_n = 1
        blocks = render_transcript_blocks(rec.events, self._ctx())
        if not blocks:
            note = (
                "(running — no output captured yet)"
                if rec.status == "running"
                else "(no captured output)"
            )
            body.mount(Static(Text(note, style="dim")))
            self._note_shown = True
        else:
            for block in blocks:
                body.mount(self._mount_widget(block))
            self._block_cache = blocks
        # The usage footer is the LAST child so it trails the transcript inline.
        self._mount_meta(rec)
        body.scroll_home(animate=False)

    def _mount_widget(self, block: TranscriptBlock):  # type: ignore[no-untyped-def]
        """Build the widget for one transcript block — the SAME widget the main
        conversation uses: assistant prose → native Markdown; a foldable Thought
        / long-tool card → Textual Collapsible (click to expand/collapse in place,
        like #log); other cards → ANSI Static. Format-identical to #log."""
        if block.kind == "markdown":
            return AutoVerseMarkdown(block.text)
        if block.kind == "collapsible":
            return self._collapsible(block)
        if block.kind == "thinking":
            return Static(Text(block.text, style=self._theme.role("text_muted")))
        return Static(Text.from_ansi(block.text))

    def _collapsible(self, block) -> Collapsible:  # type: ignore[no-untyped-def]
        """Build a foldable card for a thinking / long-tool block — mirrors
        ConversationLog.write_collapsible (collapsed by default; ``tool-block``
        class drives the grey-wash styling for tool cards). Thinking bodies are
        muted reasoning; tool bodies are ANSI output."""
        if block.thinking_body:
            inner: Content = Content.from_rich_text(
                Text(block.text, style=self._theme.role("text_muted"))
            )
        else:
            inner = Content.from_rich_text(Text.from_ansi(block.text))
        return Collapsible(
            Static(inner),
            title=block.title,
            collapsed=True,
            collapsed_symbol="+",
            expanded_symbol="-",
            classes=block.css_class or None,
        )


__all__ = ["AgentView"]
