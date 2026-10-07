"""Built-in per-tool renderers — MiMo-Code TUI parity (docs/guides/using-cli-and-tui.md §3.9.3.1).

These register against the ``TOOL_RENDERERS`` registry via
``register_tool_renderer`` — the framework (registry + dispatch) is unchanged;
this is the P8 extension point (adding a tool renderer touches no dispatcher).

Each renderer is a pure ``(ToolEndPayload, RenderContext) -> str`` function. Data
needed for rendering (path, pattern, match count, command, diff, todo list) is
read from ``ToolEndPayload.metadata`` / ``output`` — never by reflecting engine
privates (P3). Output is folded with :func:`collapse_tool_output`; full text
stays in the payload / trace.

**MiMo parity (deliberate, the user-requested alignment target)** — mirrors
``opencode/src/cli/cmd/tui/routes/session/index.tsx``:

* **Inline tools** (Read / Glob / Grep / WebFetch / WebSearch / …): one muted
  line ``icon Label`` — the *whole* line is ``text_muted`` on success (icon
  included, **no accent, no bold, no duration suffix**), exactly like MiMo's
  ``InlineTool`` whose ``fg`` is ``textMuted`` when complete. The label is built
  from **input/metadata** (``Read {path}``, ``Glob "{pattern}" (N matches)``),
  not the tool's output.
* **Block tools** (bash / Edit / Write / GenericTool): a titled block; the left
  bar + grey wash are drawn by the conversation log's ``tool-block`` CSS class,
  so renderers emit no ``┃`` glyph.
* **Status by colour + form, never ``[ok]``/``[err]`` text** — success = muted;
  a *hard* error renders a separate ``Error (N lines)`` line in red below the
  tool line (MiMo ``CollapsibleError``), the tool line itself staying muted;
  **denied / benign-recoverable** failures render **struck-through** (MiMo
  ``TextAttributes.STRIKETHROUGH``), not a red block.

Importing this module is enough to activate the renderers (registration runs at
import).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autoverse.frontends.repl.collapse import collapse_tool_output, overflow_note
from autoverse.frontends.repl.tool_error import (
    error_text,
    is_benign_error,
    is_hard_error,
)
from autoverse.frontends.repl.tool_renderer import ToolRenderer, register_tool_renderer
from autoverse.types.events import ToolEndPayload

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autoverse.frontends.repl.renderer import RenderContext

# MiMo icon set (index.tsx): read→ glob/grep✱ webfetch% bash$ edit/write←
# codesearch◇ websearch◈ skill/question→ task│ generic●. Registered against the
# *actual* tool names (snake_case) the dispatcher emits — the previous map keyed
# on TitleCase names that never matched, so every tool fell through to generic.
_ICONS: dict[str, str] = {
    "read_file": "→", "Read": "→",
    "glob": "✱", "Glob": "✱",
    "grep": "✱", "Grep": "✱",
    "web_fetch": "%", "WebFetch": "%", "Fetch": "%",
    "web_search": "◈", "WebSearch": "◈",
    "tool_search": "◇",
    "bash": "$", "Bash": "$",
    "edit_file": "←", "Edit": "←",
    "write_file": "←", "Write": "←", "MultiEdit": "←",
    "delete_file": "✗", "Delete": "✗",
    "todo_write": "☰", "TodoWrite": "☰",
    "think": "✻", "Think": "✻",
    "agent": "│", "Agent": "│",
    # Background-job family — sentence-case icon `⧖` (a waiting hourglass), NOT
    # the old `▸` (which read as "play/enter" and carried no information).
    "JobOutput": "⧖", "JobList": "⧖", "JobGet": "⧖", "JobStop": "⧖",
    # Managed-session family — `⇄` (message exchange between sessions).
    "SessionSpawn": "⇄", "SessionTell": "⇄", "SendMessage": "⇄",
    "SessionInbox": "⇄", "SessionList": "⇄", "SessionAbort": "⇄",
    "SessionTaskUpdate": "⇄",
    # Browser family — `◍` (a viewport dot).
    "browser_navigate": "◍", "browser_click": "◍", "browser_type": "◍",
    "browser_get_content": "◍", "browser_scroll": "◍", "browser_wait": "◍",
    "browser_select": "◍", "browser_keyboard": "◍", "browser_evaluate": "◍",
    "browser_screenshot": "◍", "browser_close": "◍",
    "memory_read": "▤", "memory_write": "▤",
    "EnterPlanMode": "◔", "ExitPlanMode": "◔",
    "EnterWorktreeMode": "●", "ExitWorktreeMode": "●",
    "AskUserQuestion": "?",
    "sleep": "⏱",
    "generate_image": "⊡",
    "read_image": "◉",
    "notebook_edit": "←",
}

def _icon(name: str) -> str:
    return _ICONS.get(name, "●")


def _meta(p: ToolEndPayload, key: str, default: object = "") -> object:
    return p.metadata.get(key, default) if p.metadata else default


# Backwards-compatible private alias (renderers below still call ``_is_benign``).
_is_benign = is_benign_error


def _error_line(p: ToolEndPayload, ctx: "RenderContext") -> str:
    """MiMo ``CollapsibleError`` collapsed form: ``Error (N lines)`` in red."""
    text = (p.error_message or p.output).strip()
    n = len(text.splitlines()) or 1
    label = f"Error ({n} {'line' if n == 1 else 'lines'})"
    return ctx.theme.style(label, "tool_err", ansi=ctx.ansi)


# ── inline tools (MiMo `InlineTool`) ──────────────────────────────────────────
def _inline_line(p: ToolEndPayload, ctx: "RenderContext", label: str) -> str:
    """One line: ``icon Label`` — whole line muted, struck-through if benign.

    Hard errors append a red ``Error (N lines)`` line below (MiMo parity)."""
    benign = _is_benign(p)
    body = f"{_icon(p.tool_name)} {label}".rstrip()
    line = ctx.theme.style(body, "text_muted", ansi=ctx.ansi, strike=benign)
    if p.is_error and not benign:
        line += "\n" + _error_line(p, ctx)
    return line


def _read(p: ToolEndPayload, ctx: "RenderContext") -> str:
    path = str(_meta(p, "file_path"))
    lines = _meta(p, "lines")
    label = f"Read {path}".rstrip()
    if isinstance(lines, str) and lines:  # explicit range, e.g. "1-120"
        label += f" · lines {lines}"
    return _inline_line(p, ctx, label)


def _glob(p: ToolEndPayload, ctx: "RenderContext") -> str:
    pattern = str(_meta(p, "pattern"))
    path = str(_meta(p, "path"))
    count = _meta(p, "count")
    label = f'Glob "{pattern}"' if pattern else "Glob"
    if path:
        label += f" in {path}"
    if isinstance(count, int):
        label += f" ({count} {'match' if count == 1 else 'matches'})"
    return _inline_line(p, ctx, label)


def _grep(p: ToolEndPayload, ctx: "RenderContext") -> str:
    pattern = str(_meta(p, "pattern"))
    path = str(_meta(p, "path"))
    matches = _meta(p, "matches")
    label = f'Grep "{pattern}"' if pattern else "Grep"
    if path:
        label += f" in {path}"
    if isinstance(matches, int):
        label += f" ({matches} {'match' if matches == 1 else 'matches'})"
    return _inline_line(p, ctx, label)


def _webfetch(p: ToolEndPayload, ctx: "RenderContext") -> str:
    url = str(_meta(p, "url"))
    return _inline_line(p, ctx, f"WebFetch {url}".rstrip())


def _websearch(p: ToolEndPayload, ctx: "RenderContext") -> str:
    query = str(_meta(p, "query"))
    count = _meta(p, "count")
    label = f'Web Search "{query}"' if query else "Web Search"
    if isinstance(count, int):
        label += f" ({count} results)"
    return _inline_line(p, ctx, label)


# ── bash (MiMo `Bash` → BlockTool with `$ command` + folded output) ───────────
def _bash(p: ToolEndPayload, ctx: "RenderContext") -> str:
    command = str(_meta(p, "command"))
    benign = _is_benign(p)
    head = ctx.theme.style(
        f"$ {command}".rstrip() if command else "$ bash",
        "text_muted",
        ansi=ctx.ansi,
        strike=benign,
    )
    out_text = p.output.strip()
    # Successful bash with no output → single inline line ($ command).
    if not p.is_error and not out_text:
        return head
    rows = [head]
    if out_text:
        preview, overflow = collapse_tool_output(
            out_text, max_lines=10, max_chars=max(20, ctx.width - 6) * 10
        )
        for ln in preview.split("\n"):
            rows.append(ctx.theme.style(ln, "text_muted", ansi=ctx.ansi))
        if overflow:
            rows.append(
                ctx.theme.style(overflow_note(out_text, 10), "text_dim", ansi=ctx.ansi)
            )
    if p.is_error and not benign:
        rows.append(_error_line(p, ctx))
    return "\n".join(rows)


# ── Edit / Write (MiMo `Edit`/`Write` → BlockTool + <diff>, no +N/-N footer) ──
def _diff(p: ToolEndPayload, ctx: "RenderContext") -> str:
    file_path = str(_meta(p, "file_path"))
    diff_text = str(_meta(p, "diff"))
    created = bool(_meta(p, "created", False))
    verb = "Wrote" if (p.tool_name in ("write_file", "Write") and created) else "Edit"

    if p.is_error:
        benign = _is_benign(p)
        head = ctx.theme.style(
            f"← {verb} {file_path}".rstrip(), "text_muted", ansi=ctx.ansi, strike=benign
        )
        if benign:
            return head
        return head + "\n" + _error_line(p, ctx)

    head_prefix = f"# Wrote {file_path}" if verb == "Wrote" else f"← Edit {file_path}"
    head = ctx.theme.style(head_prefix.rstrip(), "text_muted", ansi=ctx.ansi)
    rows: list[str] = [head]
    if diff_text:
        # Full MiMo ``<diff>`` parity: syntax-highlighted, line-numbered,
        # word-level-emphasised rows in a split/unified view (diff_render).
        from autoverse.frontends.repl.diff_render import render_diff

        rows.extend(render_diff(diff_text, file_path, ctx))
    # Post-edit diagnostics (MiMo ``<Diagnostics>``): warnings/errors the edit
    # introduced, shown right under the diff so the agent (and user) see them.
    rows.extend(_diagnostics_rows(p, ctx))
    return "\n".join(rows)


# Severity → (icon, role) for the diagnostics footer under a diff card.
_DIAG_SEV: dict[str, tuple[str, str]] = {
    "error": ("✗", "tool_err"),
    "warning": ("⚠", "warning"),
    "info": ("·", "info"),
    "hint": ("·", "text_dim"),
}
_DIAG_MAX_ROWS = 5


def _diagnostics_rows(p: ToolEndPayload, ctx: "RenderContext") -> list[str]:
    """Render the post-edit diagnostics block (MiMo ``<Diagnostics>`` parity).

    Reads ``metadata['diagnostics']`` — a list of ``{line, column, severity,
    message, source}`` dicts attached by the edit/write tools. Returns ``[]``
    when there are none (the common, clean case). Caps the list so a noisy file
    does not flood the card; a trailing ``… +N more`` note covers the rest."""
    raw = _meta(p, "diagnostics")
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    items: list[dict[str, object]] = [d for d in raw if isinstance(d, dict)]
    if not items:
        return []
    n_err = sum(1 for d in items if str(d.get("severity")) == "error")
    n_warn = sum(1 for d in items if str(d.get("severity")) == "warning")
    bits: list[str] = []
    if n_err:
        bits.append(f"{n_err} error{'s' if n_err != 1 else ''}")
    if n_warn:
        bits.append(f"{n_warn} warning{'s' if n_warn != 1 else ''}")
    other = len(items) - n_err - n_warn
    if other:
        bits.append(f"{other} more")
    summary = ", ".join(bits) or f"{len(items)} problems"
    head_role = "tool_err" if n_err else "warning"
    rows = [ctx.theme.style(f"⚠ Diagnostics · {summary}", head_role, ansi=ctx.ansi)]
    for d in items[:_DIAG_MAX_ROWS]:
        sev = str(d.get("severity", "info"))
        icon, role = _DIAG_SEV.get(sev, ("·", "info"))
        line = _as_int(d.get("line")) or 0
        col = _as_int(d.get("column")) or 0
        loc = f"{line}:{col}" if col else str(line)
        msg = _clip(str(d.get("message", "")), 80)
        rows.append(ctx.theme.style(f"  {icon} {loc} {msg}", role, ansi=ctx.ansi))
    hidden = len(items) - _DIAG_MAX_ROWS
    if hidden > 0:
        rows.append(ctx.theme.style(f"  … +{hidden} more", "text_dim", ansi=ctx.ansi))
    return rows


# ── todo_write (full checklist, never folded; three-state icons) ──────────────
def _todo(p: ToolEndPayload, ctx: "RenderContext") -> str:
    items = _meta(p, "todos")
    todos: list[tuple[str, str]] = []
    if isinstance(items, (list, tuple)):
        for it in items:
            if isinstance(it, dict):
                content = str(it.get("content", "")).strip()
                if content:
                    todos.append((str(it.get("status", "pending")), content))
    elif isinstance(items, str):
        todos = _parse_todo_text(items)
    if not todos:  # nothing structured → fall back to the summary line
        return ctx.theme.style(f"{_icon('todo_write')} {p.output}", "text_muted", ansi=ctx.ansi)
    done = sum(1 for st, _ in todos if st == "completed")
    icon = ctx.theme.style(_icon("todo_write"), "text_muted", ansi=ctx.ansi)
    title = ctx.theme.style("Todos", "text_muted", ansi=ctx.ansi)
    count = ctx.theme.style(f"· {done}/{len(todos)}", "text_dim", ansi=ctx.ansi)
    rows = [f"{icon} {title} {count}"]
    for st, content in todos:
        if st == "completed":
            rows.append(ctx.theme.style(f"✓ {content}", "success", ansi=ctx.ansi))
        elif st in ("in_progress", "active"):
            # MiMo / opencode in-progress marker is `•` (warning/amber).
            rows.append(ctx.theme.style(f"• {content}", "warning", ansi=ctx.ansi))
        else:
            rows.append(ctx.theme.style(f"○ {content}", "text_muted", ansi=ctx.ansi))
    return "\n".join(rows)


def _parse_todo_text(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        raw = line.strip()
        marker = ""
        content = raw
        for prefix in ("[x]", "[X]", "[~]", "[ ]"):
            idx = raw.find(prefix)
            if idx >= 0:
                marker = prefix
                content = raw[idx + len(prefix):].strip()
                break
        if not marker or not content:
            continue
        status = {
            "[x]": "completed",
            "[X]": "completed",
            "[~]": "in_progress",
            "[ ]": "pending",
        }[marker]
        out.append((status, content))
    return out


# ── Agent (MiMo `Task` → InlineTool icon `│`, "{Type} Task — {desc}" + footer) ─
def _titlecase(s: str) -> str:
    return s[:1].upper() + s[1:] if s else "General"


def _agent(p: ToolEndPayload, ctx: "RenderContext") -> str:
    from autoverse.frontends.repl.renderer import format_duration

    sub = str(_meta(p, "subagent_type")) or "general"
    desc = str(_meta(p, "description"))
    icon = _icon(p.tool_name)
    benign = _is_benign(p)
    role = "tool_err" if (p.is_error and not benign) else "text_muted"
    # MiMo `Task`: "{Type} Task — {description}".
    label = f"{icon} {_titlecase(sub)} Task"
    if desc:
        label += f" — {desc}"
    rows = [ctx.theme.style(label, role, ansi=ctx.ansi, strike=benign)]
    # MiMo footer parity: "└ {N} toolcalls · {duration}" — structured metrics,
    # not the (verbose) first line of the sub-agent's text output.
    raw_tool_count = _meta(p, "tool_count", 0)
    if isinstance(raw_tool_count, (int, float, str)):
        try:
            n_tools = int(raw_tool_count)
        except (TypeError, ValueError):
            n_tools = 0
    else:
        n_tools = 0
    bits: list[str] = []
    if n_tools:
        bits.append(f"{n_tools} toolcalls")
    elapsed = format_duration(p.duration_ms) if p.duration_ms >= 1.0 else ""
    if elapsed:
        bits.append(elapsed)
    if bits:
        rows.append(ctx.theme.style(f"└ {' · '.join(bits)}", "text_muted", ansi=ctx.ansi))
    if p.is_error and not benign:
        rows.append(_error_line(p, ctx))
    return "\n".join(rows)


# ── helpers for the new fallback-tool renderers ──────────────────────────────
def _clip(text: str, limit: int = 48) -> str:
    """One-line clip for inline labels (selectors, queries, JS results)."""
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _as_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None


# ── delete_file (Inline, MiMo-style; ✗ icon) ─────────────────────────────────
def _delete(p: ToolEndPayload, ctx: "RenderContext") -> str:
    path = str(_meta(p, "file_path"))
    existed = _meta(p, "existed", True)
    label = f"Delete {path}".rstrip() if path else "Delete"
    if existed is False and not p.is_error:
        label += " (nothing to delete)"
    return _inline_line(p, ctx, label)


# ── think (Inline; private — only a char count, never the thought text) ───────
def _think(p: ToolEndPayload, ctx: "RenderContext") -> str:
    n = _as_int(_meta(p, "chars"))
    label = "Thought" if n is None else f"Thought ({n} chars)"
    return _inline_line(p, ctx, label)


# ── sleep / hash / uuid (Inline; output already terse) ────────────────────────
def _sleep(p: ToolEndPayload, ctx: "RenderContext") -> str:
    return _inline_line(p, ctx, _clip(p.output) or "Sleep")


# ── generate_image (Inline; the image preview is emitted separately) ──────────
def _genimage(p: ToolEndPayload, ctx: "RenderContext") -> str:
    path = str(_meta(p, "path"))
    model = str(_meta(p, "model"))
    label = f"Image → {path}".rstrip() if path else "Generate image"
    if model:
        label += f" · {model}"
    return _inline_line(p, ctx, label)


# ── read_image (multimodal delegation → foldable card: title + answer body) ───
def _read_image(p: ToolEndPayload, ctx: "RenderContext") -> str:
    """Curated, auto-folding card for the vision-delegation tool.

    Title line summarises ``◉ Read image {source} · {model}``; the expandable
    body carries the question asked and the vision model's full answer. On a
    hard error the sink folds it into the standard ``Error (N lines)`` card; a
    benign error stays a struck-through inline line.
    """
    model = str(_meta(p, "model"))
    raw_sources = _meta(p, "sources")
    if isinstance(raw_sources, (list, tuple)) and raw_sources:
        sources = [str(s) for s in raw_sources]
    else:  # backward-compat with the single-source payload
        one = str(_meta(p, "source"))
        sources = [one] if one else []
    count = _as_int(_meta(p, "count"))
    n = count if count is not None else len(sources)

    if n > 1:
        label = f"Read {n} images"
    else:
        first = _clip(sources[0], 48) if sources else "image"
        label = f"Read image {first}".rstrip()
    if model:
        label += f" · {model}"
    # Errors keep the inline form (the sink owns the hard-error card).
    if p.is_error:
        return _inline_line(p, ctx, label)

    head = ctx.theme.style(f"{_icon(p.tool_name)} {label}", "text_muted", ansi=ctx.ansi)
    rows = [head]
    # List each source when more than one (so the card documents what was read).
    if n > 1:
        for s in sources:
            rows.append(
                ctx.theme.style(f"• {_clip(s, 60)}", "text_dim", ansi=ctx.ansi)
            )
    question = str(_meta(p, "question")).strip()
    if question:
        rows.append(
            ctx.theme.style(f"Q: {_clip(question, 80)}", "text_dim", ansi=ctx.ansi)
        )
    chars = _as_int(_meta(p, "chars"))
    if chars is not None:
        rows.append(
            ctx.theme.style(f"Answer · {chars} chars", "text_dim", ansi=ctx.ansi)
        )
    answer = (p.output or "").strip()
    for ln in answer.split("\n"):
        rows.append(ctx.theme.style(ln.rstrip(), "text_muted", ansi=ctx.ansi))
    return "\n".join(rows)


# ── memory (Inline; ▤ icon, namespace + empty/append hint) ────────────────────
def _memory(p: ToolEndPayload, ctx: "RenderContext") -> str:
    agent = str(_meta(p, "agent_type"))
    verb = "Read memory" if p.tool_name in ("memory_read",) else "Wrote memory"
    label = f"{verb} ({agent})" if agent else verb
    if p.tool_name == "memory_read" and _meta(p, "exists", True) is False:
        label += " · empty"
    return _inline_line(p, ctx, label)


# ── plan-mode (Enter inline; Exit shows the plan body) ────────────────────────
def _enter_plan(p: ToolEndPayload, ctx: "RenderContext") -> str:
    return _inline_line(p, ctx, "Entered plan mode")


def _exit_plan(p: ToolEndPayload, ctx: "RenderContext") -> str:
    head = ctx.theme.style(
        f"{_icon('ExitPlanMode')} Plan proposed", "success", ansi=ctx.ansi
    )
    plan = str(_meta(p, "plan")).strip()
    if not plan:
        return head
    rows = [head]
    for ln in plan.split("\n"):
        rows.append(ctx.theme.style(f"  {ln}".rstrip(), "text", ansi=ctx.ansi))
    return "\n".join(rows)


# ── worktree-mode (Inline; Exit names the requested/actual action) ────────────
def _enter_worktree(p: ToolEndPayload, ctx: "RenderContext") -> str:
    branch = str(_meta(p, "branch")).strip()
    cwd = str(_meta(p, "cwd")).strip()
    label = "Entered worktree mode"
    if cwd:
        label += f" · {cwd}"
    elif branch:
        label += f" · {branch}"
    return _inline_line(p, ctx, label)


def _exit_worktree(p: ToolEndPayload, ctx: "RenderContext") -> str:
    action = str(_meta(p, "action")).strip()
    label = "Exit worktree"
    if action:
        label += f" · {action}"
    return _inline_line(p, ctx, label)


# ── AskUserQuestion (curated, foldable card: question → user's answer) ────────
def _ask(p: ToolEndPayload, ctx: "RenderContext") -> str:
    """Render a foldable Q&A card.

    The title summarises ``? Asked the user · N answered``; the body lists each
    question and the option label(s) the user picked. The sink folds this into a
    collapsible card (collapsed by default) so the main conversation stays clean
    and the user can expand it to review what was asked and answered.

    Three failure-aware paths:
    * **benign error** (no interactive UI): a struck-through multi-line card —
      the title says "Question unavailable", the body still shows what was
      asked, followed by the error message. The agent can self-correct, so it
      stays struck-through (not a red Error card), but the user can now see
      *why* it failed and *what* was asked.
    * **hard error** (callback crashed): a struck-through title "Question failed"
      + the question list + a red ``Error (N lines)`` line.
    * **user dismissed** (Esc on every question): title "Question dismissed"
      (not "answered"), each answer shows ``(dismissed)``.
    """
    if p.is_error:
        return _ask_error(p, ctx)

    questions = _meta(p, "questions")
    answer = _meta(p, "answer")
    cancelled = bool(_meta(p, "cancelled", False))

    # Build id → list[selected option ids] from the answer payload.
    selected_by_q: dict[str, list[str]] = {}
    if isinstance(answer, dict):
        for a in answer.get("answers", []) or []:
            if isinstance(a, dict):
                qid = str(a.get("id", ""))
                sel = a.get("selected", []) or []
                if isinstance(sel, list):
                    selected_by_q[qid] = [str(s) for s in sel]

    if not isinstance(questions, (list, tuple)) or not questions:
        # No structured questions (older payload) — fall back to a one-liner.
        if cancelled:
            return _inline_line(p, ctx, "Question dismissed")
        return _inline_line(p, ctx, "Asked the user")

    n = len(questions)
    if cancelled:
        head = ctx.theme.style(
            f"{_icon(p.tool_name)} Question dismissed", "text_muted", ansi=ctx.ansi
        )
    else:
        head = ctx.theme.style(
            f"{_icon(p.tool_name)} Asked the user · {n} answered"
            if n != 1
            else f"{_icon(p.tool_name)} Asked the user",
            "text_muted",
            ansi=ctx.ansi,
        )
    rows = [head]
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id", ""))
        prompt = str(q.get("prompt", "") or qid)
        labels = q.get("labels", {})
        sel_ids = selected_by_q.get(qid, [])
        if isinstance(labels, dict) and labels:
            chosen = [str(labels.get(s, s)) for s in sel_ids]
        else:
            chosen = [str(s) for s in sel_ids]
        if cancelled:
            answer_str = "(dismissed)"
        else:
            answer_str = ", ".join(chosen) if chosen else "(no answer)"
        rows.append(
            ctx.theme.style(f"Q: {prompt}", "text_muted", ansi=ctx.ansi)
        )
        rows.append(
            ctx.theme.style(f"  → {answer_str}", "accent", ansi=ctx.ansi)
        )
    return "\n".join(rows)


def _ask_error(p: ToolEndPayload, ctx: "RenderContext") -> str:
    """Render AskUserQuestion errors with question context + the error message.

    Benign errors (``ask_user_unavailable``) show a struck-through "Question
    unavailable" title + the questions that were asked + the error message as a
    muted line — not a red Error card (the agent can self-correct), but the user
    can now see *what* was asked and *why* it failed instead of a bare label.

    Hard errors (callback crash) show "Question failed" + the questions + a red
    ``Error (N lines)`` line so the user can expand to read the full traceback.
    """
    benign = _is_benign(p)
    questions = _meta(p, "questions")
    error_msg = (p.error_message or p.output or "").strip()

    if benign:
        title = "Question unavailable"
    else:
        title = "Question failed"

    # If we have question context, render a multi-line card with it.
    if isinstance(questions, (list, tuple)) and questions:
        head = ctx.theme.style(
            f"{_icon(p.tool_name)} {title}", "text_muted",
            ansi=ctx.ansi, strike=benign,
        )
        rows = [head]
        for q in questions:
            if not isinstance(q, dict):
                continue
            prompt = str(q.get("prompt", "") or q.get("id", ""))
            rows.append(
                ctx.theme.style(f"Q: {prompt}", "text_muted", ansi=ctx.ansi)
            )
        if error_msg:
            rows.append(
                ctx.theme.style(f"  {error_msg}", "tool_err" if not benign else "text_dim",
                                ansi=ctx.ansi)
            )
        if not benign:
            rows.append(_error_line(p, ctx))
        return "\n".join(rows)

    # No question context — fall back to the inline line with error info.
    label = title
    if error_msg:
        # Clip the error message to keep the inline label readable.
        label = f"{title} · {_clip(error_msg, 60)}"
    return _inline_line(p, ctx, label)


# ── background jobs (⧖ icon) ──────────────────────────────────────────────────
def _jobs_block(
    p: ToolEndPayload, ctx: "RenderContext", title: str
) -> str:
    """Shared block for JobOutput / JobList: a header + one line per job."""
    icon = _icon(p.tool_name)
    jobs = _meta(p, "jobs")
    head = ctx.theme.style(f"{icon} {title}", "text_muted", ansi=ctx.ansi)
    if not isinstance(jobs, (list, tuple)) or not jobs:
        return head
    rows = [head]
    for j in jobs:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("job_id", "?"))
        subj = _clip(str(j.get("subject", "")), 40)
        status = str(j.get("status", ""))
        line = f"└ [{jid}] {subj}" + (f" — {status}" if status else "")
        rows.append(ctx.theme.style(line, "text_muted", ansi=ctx.ansi))
    return "\n".join(rows)


def _job_output(p: ToolEndPayload, ctx: "RenderContext") -> str:
    collected = _as_int(_meta(p, "collected"))
    pending = bool(_meta(p, "pending", False))
    if not collected:  # nothing finished this round → terse single line
        note = "still running" if pending else "none dispatched"
        return _inline_line(p, ctx, f"Collected 0 jobs ({note})")
    title = f"Collected {collected} {'job' if collected == 1 else 'jobs'}"
    if pending:
        title += " · more running"
    return _jobs_block(p, ctx, title)


def _job_list(p: ToolEndPayload, ctx: "RenderContext") -> str:
    total = _as_int(_meta(p, "total")) or 0
    pending = _as_int(_meta(p, "pending"))
    if total == 0:
        return _inline_line(p, ctx, "Jobs (none)")
    title = f"Jobs · {total} total"
    if pending:
        title += f" · {pending} running"
    return _jobs_block(p, ctx, title)


def _job_get(p: ToolEndPayload, ctx: "RenderContext") -> str:
    jid = str(_meta(p, "job_id"))
    status = str(_meta(p, "status"))
    label = "Job"
    if jid:
        label += f" [{jid}]"
    if status:
        label += f" — {status}"
    return _inline_line(p, ctx, label)


def _job_stop(p: ToolEndPayload, ctx: "RenderContext") -> str:
    jid = str(_meta(p, "job_id"))
    cancelled = bool(_meta(p, "cancelled", False))
    verb = "Stopped" if cancelled else "Stop (no-op)"
    return _inline_line(p, ctx, f"{verb} job [{jid}]" if jid else verb)


# ── managed sessions (⇄ icon) ─────────────────────────────────────────────────
def _sess_id(p: ToolEndPayload) -> str:
    sid = str(_meta(p, "session_id"))
    return f" [{sid}]" if sid else ""


def _session(verb: str) -> ToolRenderer:
    def render(p: ToolEndPayload, ctx: "RenderContext") -> str:
        return _inline_line(p, ctx, f"{verb}{_sess_id(p)}")
    return render


def _session_list(p: ToolEndPayload, ctx: "RenderContext") -> str:
    n = _as_int(_meta(p, "count"))
    label = "Sessions" if n is None else f"Sessions ({n})"
    return _inline_line(p, ctx, label)


def _session_task_update(p: ToolEndPayload, ctx: "RenderContext") -> str:
    status = str(_meta(p, "status"))
    label = f"Session{_sess_id(p)}" + (f" → {status}" if status else " task update")
    return _inline_line(p, ctx, label)


def _send_message(p: ToolEndPayload, ctx: "RenderContext") -> str:
    return _inline_line(p, ctx, f"Message{_sess_id(p)}")


# ── browser family (◍ icon; show the operation target) ────────────────────────
def _browser(verb: str, *keys: str) -> ToolRenderer:
    """Build a browser renderer: ``◍ {verb} {first-present meta key value}``."""

    def render(p: ToolEndPayload, ctx: "RenderContext") -> str:
        detail = ""
        for k in keys:
            v = _meta(p, k)
            if v not in ("", None):
                detail = _clip(str(v))
                break
        label = f"{verb} {detail}".rstrip()
        return _inline_line(p, ctx, label)

    return render


def _browser_type(p: ToolEndPayload, ctx: "RenderContext") -> str:
    sel = _clip(str(_meta(p, "selector")), 24)
    text = _clip(str(_meta(p, "text")), 24)
    label = f"Type into {sel}".rstrip()
    if text:
        label += f' "{text}"'
    return _inline_line(p, ctx, label)


def _browser_get_content(p: ToolEndPayload, ctx: "RenderContext") -> str:
    mode = str(_meta(p, "mode")) or "text"
    chars = _as_int(_meta(p, "chars"))
    label = f"Get content ({mode}"
    label += f", {chars} chars)" if chars is not None else ")"
    return _inline_line(p, ctx, label)


def _browser_scroll(p: ToolEndPayload, ctx: "RenderContext") -> str:
    direction = str(_meta(p, "direction")) or "down"
    amount = _as_int(_meta(p, "amount"))
    label = f"Scroll {direction}"
    if amount is not None:
        label += f" {amount}px"
    return _inline_line(p, ctx, label)


def _browser_wait(p: ToolEndPayload, ctx: "RenderContext") -> str:
    cond = str(_meta(p, "condition")) or "visible"
    sel = _clip(str(_meta(p, "selector")), 30)
    label = f"Wait {cond}"
    if sel:
        label += f" {sel}"
    return _inline_line(p, ctx, label)


def _browser_screenshot(p: ToolEndPayload, ctx: "RenderContext") -> str:
    scope = "full page" if _meta(p, "full_page", False) else "viewport"
    return _inline_line(p, ctx, f"Screenshot ({scope})")


def _browser_evaluate(p: ToolEndPayload, ctx: "RenderContext") -> str:
    res = _clip(str(_meta(p, "result_repr")), 36)
    label = "Evaluate JS"
    if res:
        label += f" → {res}"
    return _inline_line(p, ctx, label)


def register_builtin_tool_renderers() -> None:
    """Register all built-in per-tool renderers (idempotent).

    Registered against the *actual* dispatcher tool names (snake_case) plus the
    legacy TitleCase aliases, so renderers actually fire (the prior map keyed
    only TitleCase and silently fell through to the generic card)."""
    for name in ("read_file", "Read"):
        register_tool_renderer(name, _read)
    for name in ("glob", "Glob"):
        register_tool_renderer(name, _glob)
    for name in ("grep", "Grep"):
        register_tool_renderer(name, _grep)
    for name in ("web_fetch", "WebFetch", "Fetch"):
        register_tool_renderer(name, _webfetch)
    for name in ("web_search", "WebSearch"):
        register_tool_renderer(name, _websearch)
    for name in ("bash", "Bash"):
        register_tool_renderer(name, _bash)
    for name in ("edit_file", "Edit", "write_file", "Write", "MultiEdit"):
        register_tool_renderer(name, _diff)
    for name in ("todo_write", "TodoWrite"):
        register_tool_renderer(name, _todo)
    for name in ("agent", "Agent"):
        register_tool_renderer(name, _agent)
    for name in ("delete_file", "Delete"):
        register_tool_renderer(name, _delete)
    for name in ("think", "Think"):
        register_tool_renderer(name, _think)
    register_tool_renderer("sleep", _sleep)
    register_tool_renderer("generate_image", _genimage)
    register_tool_renderer("read_image", _read_image)
    for name in ("memory_read", "memory_write"):
        register_tool_renderer(name, _memory)
    register_tool_renderer("EnterPlanMode", _enter_plan)
    register_tool_renderer("ExitPlanMode", _exit_plan)
    register_tool_renderer("EnterWorktreeMode", _enter_worktree)
    register_tool_renderer("ExitWorktreeMode", _exit_worktree)
    register_tool_renderer("AskUserQuestion", _ask)

    # Background-job family.
    register_tool_renderer("JobOutput", _job_output)
    register_tool_renderer("JobList", _job_list)
    register_tool_renderer("JobGet", _job_get)
    register_tool_renderer("JobStop", _job_stop)

    # Managed-session family.
    register_tool_renderer("SessionSpawn", _session("Spawned session"))
    register_tool_renderer("SessionTell", _session("Tell session"))
    register_tool_renderer("SendMessage", _send_message)
    register_tool_renderer("SessionInbox", _session("Inbox"))
    register_tool_renderer("SessionList", _session_list)
    register_tool_renderer("SessionAbort", _session("Aborted session"))
    register_tool_renderer("SessionTaskUpdate", _session_task_update)

    # Browser family (operation target from metadata).
    register_tool_renderer("browser_navigate", _browser("Navigate", "url"))
    register_tool_renderer("browser_click", _browser("Click", "selector"))
    register_tool_renderer("browser_type", _browser_type)
    register_tool_renderer("browser_get_content", _browser_get_content)
    register_tool_renderer("browser_scroll", _browser_scroll)
    register_tool_renderer("browser_wait", _browser_wait)
    register_tool_renderer("browser_select", _browser("Select", "selector"))
    register_tool_renderer("browser_keyboard", _browser("Press", "key"))
    register_tool_renderer("browser_evaluate", _browser_evaluate)
    register_tool_renderer("browser_screenshot", _browser_screenshot)
    register_tool_renderer("browser_close", _browser("Browser closed"))


register_builtin_tool_renderers()


__all__ = [
    "register_builtin_tool_renderers",
    "is_benign_error",
    "is_hard_error",
    "error_text",
]
