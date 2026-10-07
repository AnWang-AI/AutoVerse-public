"""Textual widgets for the AutoVerse TUI.

The widgets that make up the bottom input region (MiMo-Code session-page parity):

* :class:`StatusBar` — a single reactive line showing spinner frame, phase and
  elapsed seconds while the engine is running, plus a compact subagent-activity
  summary. It is driven by the app's ``set_interval`` tick; the timing is a
  render-time derivation (no module-level globals, P4).
* :class:`CommandInput` — the bottom input box. It is a **multi-line**
  ``TextArea`` (MiMo uses a ``<textarea>``): ``Enter`` submits (posts
  :class:`CommandInput.Entered`), ``Shift+Enter`` / ``Alt+Enter`` inserts a
  newline, and the box grows 1→6 rows with content. Submitting reads the text on
  the *next* refresh tick so an in-flight IME composition (Chinese / Korean) is
  flushed to the buffer first — the same trick MiMo uses (double ``setTimeout``).
* :class:`PromptMeta` — the muted line *inside* the input frame, below the text
  area (``model · cwd``; MiMo shows ``agent · model`` there).
* :class:`PromptTail` — the one-row spacer *outside / below* the input frame
  (MiMo's trailing ``height={1}`` box), keeping the left bar visually continuous.
* :class:`QueuedPanel` — the pending-message panel pinned just above the input
  frame: while the engine runs, queued submissions stay visible here as a muted
  numbered list (position index + truncated text, capped rows) instead of a
  transient log line, and the panel hides itself when the queue empties.

These widgets import ``textual`` and are only loaded on the TUI path.
"""

from __future__ import annotations

from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static, TextArea


# Human-readable labels for the semantic phase keys the app sets. The app keeps
# state as terse keys (P2: presentation lives here, not in the engine driver);
# busy_label phases (e.g. ``Compacting…``) are already capitalised prose and pass
# through the ``.get(key, key)`` fallback unchanged, so the whole bar reads in one
# consistent ``Title…`` voice (see commands.py busy_label).
_PHASE_LABELS = {
    "thinking": "Thinking…",
    "generating": "Responding…",
    "tool": "Running tool…",
}


class StatusBar(Static):
    """Bottom status line: spinner · phase · elapsed · subagent activity.

    All fields are reactive; the app updates them and the widget re-renders.
    When idle the bar shows a muted hint.
    """

    phase: reactive[str] = reactive("idle")
    elapsed: reactive[float] = reactive(0.0)
    frame: reactive[str] = reactive("")
    activity: reactive[str] = reactive("")
    context: reactive[str] = reactive("")  # e.g. "dsv4 · ~/proj" shown when idle
    # When the in-place AgentView is open the app drives this single pre-formatted
    # markup line directly (spinner · activity · elapsed while running; a terse
    # ✓/✗ final-state line when done). It takes over the whole bar so the footer
    # stays ONE line and never shows main-agent ready/help/model hints that are
    # irrelevant to the subagent you drilled into.
    agent_line: reactive[str] = reactive("")

    def on_mount(self) -> None:
        # Pin a fixed height before the first render so the widget is visible
        # from frame 0.  Textual renders widgets *before* laying them out, so a
        # ``height: auto`` widget starts with size (0, 0) — its first render
        # produces nothing and it only appears after ``on_resize`` triggers a
        # second render.  Setting height = 1 here gives the first frame a
        # non-zero region; ``on_resize`` restores ``auto`` so the activity line
        # can still grow the bar to 2 rows during tool execution.
        self.styles.height = 1

    def on_resize(self, event: object) -> None:  # noqa: ARG002
        # Restore auto height once layout has provided a real size, so the
        # activity line (a second row) can expand the bar when present.
        if self.styles.height is not None:
            self.styles.height = "auto"
        # The idle hint narrows to fit the bar (``_idle_hint``), so a width
        # change must re-render even though no reactive attribute moved.
        self.refresh()

    def render(self) -> str:
        # Colours mirror the amber-gold theme (accent $av-accent, dim $av-text-dim).
        if self.agent_line:
            return self.agent_line
        if self.phase == "idle":
            return f"[$av-text-dim]{self._idle_hint()}[/$av-text-dim]"
        label = _PHASE_LABELS.get(self.phase, self.phase)
        parts = [p for p in (self.frame, label) if p]
        head = " ".join(parts)
        line = (
            f"[bold $av-accent]{head}[/bold $av-accent]"
            f"[$av-text-dim] · {self.elapsed:.1f}s[/$av-text-dim]"
        )
        if self.activity:
            line += f"\n[$av-text-muted]{self.activity}[/$av-text-muted]"
        return line

    def _idle_hint(self) -> str:
        """Idle hint text, narrowed to fit the bar so it never overflows/wraps.

        The bar carries CSS padding ``0 2 0 3`` (5 cols), so the usable width is
        ``size.width - 5``. We pick the widest variant that fits: full hint →
        keys-only (drop the ``ready`` lead-in) → ``/help`` only. The optional
        idle ``context`` prefix (``model · ~/proj``) is shown only when the full
        hint still fits with it. ``size.width`` is 0 before the first layout, in
        which case we assume the full hint (it is corrected on the next render).
        """
        keys = "Ctrl+C interrupt · Ctrl+D quit"
        full = f"ready · /help · /model · {keys}"
        avail = self.size.width - 5
        if avail <= 0:  # not laid out yet — assume room, refresh corrects it
            ctx = f"{self.context} · " if self.context else ""
            return f"{ctx}{full}"
        # Prefer prefixing the idle context, but only if the whole thing fits.
        if self.context and len(f"{self.context} · {full}") <= avail:
            return f"{self.context} · {full}"
        if len(full) <= avail:
            return full
        keys_only = f"/help · {keys}"
        if len(keys_only) <= avail:
            return keys_only
        if len("/help · /model") <= avail:
            return "/help · /model"
        return "/help"


class PromptMeta(Static):
    """Muted meta line inside the input frame (``model · cwd``; MiMo parity).

    A single reactive ``text`` line; the app sets it from the model/cwd facts.
    """

    text_line: reactive[str] = reactive("")

    def render(self) -> str:
        return self.text_line or ""


class PromptTail(Static):
    """One-row spacer below the input frame (MiMo's trailing ``height={1}``).

    Renders empty content; its left bar comes from CSS so the input's left
    accent rule reads as continuous through this row.
    """

    def render(self) -> str:
        return ""


class QueuedPanel(Static):
    """Pending-message panel pinned just above the input frame.

    While the engine is running, user submissions are enqueued rather than run
    immediately. Instead of a transient log line, the still-pending messages
    stay visible here (MiMo shows them inline with a ``QUEUED`` badge; we keep
    a stable panel anchored at the input so they never scroll out of view).

    The app drives ``lines`` from ``MessageQueue.items()`` after every
    enqueue / dequeue and toggles the ``-hidden`` class when the queue empties.

    Each entry is a single truncated row numbered by its position in the queue
    (the running index doubles as the "ordered, waiting" cue — no emoji, in
    keeping with the rest of the text-only TUI). The common ``next`` priority is
    left implicit; only an off-default priority (``now`` / ``later``) is tagged
    at the row's end. At most :data:`MAX_ROWS` rows are shown, followed by a
    single muted footer that names the region (``queued``), notes any overflow
    (``+N more``) and the edit / cancel affordances (``Ctrl+E edit`` pulls the
    last queued message back to edit; ``Ctrl+X cancel``).
    """

    MAX_ROWS = 5
    _MAX_TEXT = 72  # per-row text width before ellipsis

    # Mode → CSS colour var for the per-row mode badge. MUST mirror `_mode_badge`
    # (app.py) + the input frame's left-bar CSS, the single source of truth for
    # interaction-mode colour (docs/guides/using-cli-and-tui.md §3.10): normal=amber (warning),
    # plan=green (success), worktree=lavender (mode_worktree). A divergent copy
    # here previously swapped normal/plan and pointed worktree at an undefined
    # `$av-secondary` var (→ broken badge), so the queued row's colour disagreed
    # with the same mode's input bar / status badge.
    _MODE_COLOR = {
        "normal": "$av-warning",
        "plan": "$av-success",
        "worktree": "$av-mode-worktree",
    }

    # Each line is ``(priority, text, mode)`` — ``mode`` is the interaction mode
    # active when the message was enqueued (see MessageQueue.items_with_mode).
    lines: reactive[tuple[tuple[str, str, str], ...]] = reactive(())

    def render(self) -> str:
        items = self.lines
        if not items:
            return ""
        shown = items[: self.MAX_ROWS]
        rows: list[str] = []
        for idx, (prio, text, mode) in enumerate(shown, start=1):
            flat = " ".join(text.split())
            if len(flat) > self._MAX_TEXT:
                flat = flat[: self._MAX_TEXT - 1] + "…"
            flat = flat.replace("[", r"\[")
            # Mode badge: the message keeps the mode it was SENT under, so a
            # later Tab never rewrites a pending row's mode (parity with the
            # frozen user-echo card + per-turn footer).
            color = self._MODE_COLOR.get(mode, "$av-text-dim")
            badge = f"[{color}]{mode}[/{color}]  "
            # The position index carries the "queued / waiting" meaning; the
            # ubiquitous ``next`` priority stays implicit so a normal queue
            # reads as a clean numbered list. Only an unusual priority shows.
            tag = (
                "" if prio == "next"
                else f"  [$av-text-dim]{prio}[/$av-text-dim]"
            )
            rows.append(
                f"[$av-text-dim]{idx}[/$av-text-dim]  {badge}"
                f"[$av-text-muted]{flat}[/$av-text-muted]{tag}"
            )
        # One muted footer: region label · overflow · cancel affordance. The
        # word "queued" replaces the old emoji as the region's semantic cue.
        parts = ["queued"]
        extra = len(items) - len(shown)
        if extra > 0:
            parts.append(f"+{extra} more")
        parts.append("Ctrl+E edit")
        parts.append("Ctrl+X cancel")
        footer = " · ".join(parts)
        rows.append(f"[$av-text-dim]{footer}[/$av-text-dim]")
        return "\n".join(rows)


class CommandInput(TextArea):
    """Bottom input box — a multi-line ``TextArea`` (MiMo ``<textarea>`` parity).

    Posts :class:`CommandInput.Entered` on Enter and :class:`CommandInput.Changed`
    on content change (so the app's inline completion menu keeps its single
    handler). Exposes a ``value`` property and ``cursor_position`` setter so the
    rest of the app (and tests) treat it like the old single-line ``Input``.

    Enter submits; Shift+Enter / Alt+Enter / Ctrl+J insert a newline. The box is
    never disabled while the engine runs — submissions are queued by the app.
    """

    DEFAULT_PLACEHOLDER = "› message or /command"

    class Entered(Message):
        """Posted when the user presses Enter with non-empty content."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class ContentChanged(Message):
        """Posted whenever the text content changes (drives slash completion).

        Named ``ContentChanged`` (not ``Changed``) so it does not shadow
        ``TextArea.Changed``, which the base widget posts internally."""

        def __init__(self, value: str, command_input: "CommandInput") -> None:
            self.value = value
            self.input = command_input
            super().__init__()

    def __init__(
        self, *, completion_names: tuple[str, ...] = (), **kwargs: object
    ) -> None:
        super().__init__(
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            placeholder=self.DEFAULT_PLACEHOLDER,
            # No cursor-line highlight — the lighter grey wash on the current
            # line is visual noise on a single grey prompt surface.
            highlight_cursor_line=False,
            **kwargs,  # type: ignore[arg-type]
        )
        self._completion_names = completion_names
        # Command history (shell-style ↑/↓ recall). ``_history`` holds submitted
        # entries oldest→newest; ``_history_idx`` is ``None`` while editing a
        # fresh draft, else an index into ``_history``. ``_history_draft`` stashes
        # the unsent draft so ↓ past the newest entry restores it.
        self._history: list[str] = []
        self._history_idx: int | None = None
        self._history_draft: str = ""

    # ── value shim (single-line Input compatibility) ─────────────────────────
    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, new: str) -> None:
        self.load_text(new)

    @property
    def cursor_position(self) -> int:
        # Flat character offset of the cursor (Input-style); derived from the
        # (row, col) location TextArea exposes.
        row, col = self.cursor_location
        lines = self.text.splitlines(keepends=True)
        return sum(len(ln) for ln in lines[:row]) + col

    @cursor_position.setter
    def cursor_position(self, offset: int) -> None:
        # Place the cursor at a flat character offset (used by tab-complete).
        self.move_cursor(self._location_from_offset(offset))

    def _location_from_offset(self, offset: int) -> tuple[int, int]:
        """Map a flat character offset to a TextArea ``(row, col)`` location."""
        remaining = max(0, offset)
        lines = self.text.split("\n")
        for row, line in enumerate(lines):
            if remaining <= len(line):
                return (row, remaining)
            remaining -= len(line) + 1  # +1 for the consumed newline
        last = len(lines) - 1
        return (last, len(lines[last]))

    # ── growth: 1→6 rows with content (MiMo minHeight 1 / maxHeight 6) ────────
    def _resize_to_content(self) -> None:
        rows = max(1, min(6, self.document.line_count))
        self.styles.height = rows

    # ── events ────────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self._resize_to_content()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Re-post a typed message so the app keeps one handler; resize to fit.
        if event.text_area is not self:
            return
        self._resize_to_content()
        self.post_message(self.ContentChanged(self.text, self))

    async def _on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        key = event.key
        # Inline choice menu (AskUserQuestion / permission) captures ALL keys
        # while picking — modal-like capture without a full-screen modal. The
        # input is focused (the menu never takes focus) so this handler sees keys
        # first; it forwards the recognised ones and swallows the rest so nothing
        # leaks into the text buffer or the queue during a prompt.
        if self._choice_picking():
            event.prevent_default()
            event.stop()
            handler = getattr(self.app, "_handle_choice_key", None)
            if callable(handler):
                handler(key)
            return
        # Free-text "Other" answer mode: Esc returns to the option list; all other
        # keys edit/submit the answer normally.
        if key == "escape" and self._choice_awaiting_custom():
            event.prevent_default()
            event.stop()
            cancel = getattr(self.app, "_handle_choice_custom_cancel", None)
            if callable(cancel):
                cancel()
            return
        # Shift+Enter / Alt+Enter / Ctrl+J → insert a literal newline.
        if key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if key == "enter":
            event.prevent_default()
            event.stop()
            # IME flush: read the text on the next refresh tick so a composing
            # character (e.g. a CJK candidate) lands in the buffer first.
            self.call_after_refresh(self._submit)
            return
        # While the inline completion menu is open, ↑/↓ drive its highlight
        # (App.on_key forwards them to the menu). Let those keys bubble up
        # untouched: don't recall history or move the text cursor here. Without
        # this, ↑ on the first row hits the history-recall branch below and
        # ``event.stop()``s, so the menu could never move up — down only worked
        # by luck (its recall needs an active history index). MiMo hands the
        # same navigation keys to autocomplete via the textarea capture traits.
        if key in ("up", "down") and self._completion_visible():
            return
        # ↑/↓ recall history — but only when the cursor is on the boundary row,
        # so multi-line drafts can still move the cursor between lines. Pressing
        # ↑ on the top row recalls an older command; ↓ on the bottom row recalls
        # a newer one (and past the newest restores the in-progress draft).
        if key == "up" and self._history and self.cursor_location[0] == 0:
            event.prevent_default()
            event.stop()
            self._history_prev()
            return
        if key == "down" and self._history_idx is not None and (
            self.cursor_location[0] == self.document.line_count - 1
        ):
            event.prevent_default()
            event.stop()
            self._history_next()
            return
        await super()._on_key(event)

    def _completion_visible(self) -> bool:
        """True while the app's inline slash-completion menu is showing.

        Queried by id (not type) to avoid importing — and circularly depending
        on — the dialogs package; harnesses without a ``#completion`` widget
        simply get False.
        """
        try:
            menu = self.app.query_one("#completion")
        except Exception:  # noqa: BLE001 — optional completion widget may be absent
            return False
        return bool(getattr(menu, "visible_now", False))

    def _choice_picking(self) -> bool:
        """True while the inline choice menu is showing options to pick from.

        Queried by id (like ``_completion_visible``) so harnesses without a
        ``#choice`` widget simply get False.
        """
        try:
            menu = self.app.query_one("#choice")
        except Exception:  # noqa: BLE001 — optional choice widget may be absent
            return False
        return bool(getattr(menu, "picking_now", False))

    def _choice_awaiting_custom(self) -> bool:
        """True while the app expects a free-text "Other" answer in this box."""
        return bool(getattr(self.app, "_choice_awaiting_custom", False))

    # ── history recall ──────────────────────────────────────────────────────
    def _history_prev(self) -> None:
        if self._history_idx is None:
            # Entering navigation: stash the live draft and start at the newest.
            self._history_draft = self.text
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        else:
            return  # already at the oldest entry
        self._load_history_entry(self._history[self._history_idx])

    def _history_next(self) -> None:
        if self._history_idx is None:
            return
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self._load_history_entry(self._history[self._history_idx])
        else:
            # Past the newest entry → restore the original draft, exit navigation.
            self._history_idx = None
            self._load_history_entry(self._history_draft)

    def _load_history_entry(self, text: str) -> None:
        self.load_text(text)
        self._resize_to_content()
        self.move_cursor(self.document.end)

    def _record_history(self, raw: str) -> None:
        # Append unless it duplicates the most recent entry (shell-style dedup).
        if not self._history or self._history[-1] != raw:
            self._history.append(raw)
        self._history_idx = None
        self._history_draft = ""

    def _submit(self) -> None:
        raw = (self.text or "").strip()
        self.load_text("")
        self._resize_to_content()
        if raw:
            self._record_history(raw)
            self.post_message(self.Entered(raw))


__all__ = ["StatusBar", "CommandInput", "PromptMeta", "PromptTail"]
