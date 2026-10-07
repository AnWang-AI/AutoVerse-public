"""Rich diff rendering — MiMo ``<diff>`` component parity in a string pipeline.

The TUI commits tool cards as ANSI strings (``Text.from_ansi`` → Textual
``Static``/``Collapsible``), so the whole MiMo diff feature set is reproduced by
*emitting the right ANSI* rather than mounting a native widget:

* **Syntax highlighting** — the code on each diff row is tokenised by Pygments
  (lexer inferred from the file path) and the token foreground colours are laid
  *under* the add/del/context wash (MiMo ``filetype`` + ``syntaxStyle``).
* **Line numbers** — a left gutter shows the old line number on ``-`` rows and
  the new line number on ``+``/context rows, parsed from the ``@@`` hunk header
  (MiMo ``showLineNumbers``), with the gutter tinted to match the row.
* **Word-level emphasis** — paired ``-``/``+`` rows are intra-line diffed
  (``difflib.SequenceMatcher``); the *changed* character spans get a brighter
  emphasis background so the eye lands on what changed (MiMo per-token highlight).
* **Split / unified view** — wide terminals (``width > split_threshold``) render
  a side-by-side old │ new view; narrow ones stack a unified view (MiMo's
  ``ctx.width > 120 ? "split" : "unified"``).

All colour is opt-in via ``RenderContext.ansi``; with ``ansi=False`` the
functions emit plain, deterministic text (line numbers + ``+``/``-`` signs +
code) so unit tests stay TTY-free. Highlighting failures degrade gracefully to
no-highlight — a diff card must never break the conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autoverse.frontends.repl.theme import Theme
    from autoverse.frontends.repl.renderer import RenderContext

# Alignment is by *display width*, not character count: tabs and East-Asian
# wide glyphs occupy a number of terminal cells that differs from ``len(str)``,
# which is exactly what makes a naive split view's separator drift. Rich's cell
# helpers give the true width; if Rich is unavailable (headless ``--print``) we
# fall back to 1-cell-per-char so the module still imports and runs.
try:  # pragma: no cover - import wiring
    from rich.cells import cell_len as _cell_len
    from rich.cells import set_cell_size as _set_cell_size
except Exception:  # pragma: no cover - rich missing
    _cell_len = None  # type: ignore[assignment]
    _set_cell_size = None  # type: ignore[assignment]

# Tab stop used when expanding tabs to spaces (so a tab never desyncs the grid).
_TABSIZE = 4

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _cw(ch: str) -> int:
    """Display width (terminal cells) of a single character."""
    if _cell_len is None:
        return 1
    try:
        return _cell_len(ch)
    except Exception:  # pragma: no cover - defensive
        return 1


def _fit_plain(body: str, width: int) -> str:
    """Truncate / pad *body* to exactly *width* display cells (plain text)."""
    if _set_cell_size is not None:
        try:
            return _set_cell_size(body, width)
        except Exception:  # pragma: no cover - defensive
            pass
    return body[:width].ljust(width)

# Below this intra-line similarity a ``-``/``+`` pair is treated as a wholesale
# rewrite (no word-level emphasis) — emphasising *every* character is noise.
_INTRALINE_MIN_RATIO = 0.3

# Hard ceiling on the rendered width so a very wide terminal does not produce
# absurdly long padded rows (matches the renderer's existing ``min(width, 100)``
# instinct, lifted to 120 to allow a comfortable split view).
_MAX_WIDTH = 120


@dataclass
class _Row:
    kind: str  # "hunk" | "add" | "del" | "ctx"
    text: str
    old_no: int = 0
    new_no: int = 0
    emph: list[bool] = field(default_factory=list)


# ── Pygments seams (cached; failures degrade to no-highlight) ─────────────────
@lru_cache(maxsize=64)
def _lexer_for(suffix: str):  # type: ignore[no-untyped-def]
    if not suffix:
        return None
    try:
        from pygments.lexers import get_lexer_for_filename

        return get_lexer_for_filename(f"x{suffix}", stripnl=False)
    except Exception:  # noqa: BLE001 — unknown suffix simply has no lexer
        return None


def _suffix(file_path: str) -> str:
    i = file_path.rfind(".")
    j = max(file_path.rfind("/"), file_path.rfind("\\"))
    return file_path[i:] if i > j and i != -1 else ""


def _role_for_token(tok_type) -> str | None:  # type: ignore[no-untyped-def]
    """Map a Pygments token type to one of the theme's warm ``syntax_*`` roles.

    We deliberately drive colour from *our* palette (warm, low-saturation, amber-
    aligned) rather than a Pygments style's neon defaults — the diff must read as
    part of the app, not a foreign code window. ``None`` → the row's default
    foreground (operators on plain identifiers, whitespace, etc.)."""
    try:
        from pygments.token import (
            Comment,
            Keyword,
            Name,
            Number,
            Operator,
            Punctuation,
            String,
        )
    except Exception:  # pragma: no cover - pygments missing
        return None
    # Order matters: test the most specific subtypes first (Pygments token types
    # support prefix membership, e.g. ``Keyword.Constant in Keyword``).
    if tok_type in Comment:
        return "syntax_comment"
    if tok_type in Keyword.Constant or tok_type in Keyword.Type:
        return "syntax_constant" if tok_type in Keyword.Constant else "syntax_class"
    if tok_type in Keyword:
        return "syntax_keyword"
    if tok_type in String:
        return "syntax_string"
    if tok_type in Number:
        return "syntax_number"
    if tok_type in Name.Decorator:
        return "syntax_decorator"
    if tok_type in Name.Function:
        return "syntax_function"
    if tok_type in Name.Class or tok_type in Name.Namespace:
        return "syntax_class"
    if tok_type in Name.Builtin or tok_type in Name.Builtin.Pseudo:
        return "syntax_builtin"
    if tok_type in Name.Annotation:
        return "syntax_type_annotation"
    if tok_type in Name.Constant:
        return "syntax_constant"
    if tok_type in Operator or tok_type in Punctuation:
        return "syntax_operator"
    return None


def _fg_per_char(code: str, file_path: str, theme) -> list[str | None]:  # type: ignore[no-untyped-def]
    """Per-character syntax foreground (hex) for *code*; ``None`` where untokened.

    Colours come from the theme's warm ``syntax_*`` roles (not a Pygments style),
    so highlighting stays on-brand. Returns a list as long as *code*; an absent
    lexer / Pygments error yields all-``None`` so callers fall back to the row's
    default foreground."""
    n = len(code)
    fgs: list[str | None] = [None] * n
    if n == 0:
        return fgs
    lexer = _lexer_for(_suffix(file_path))
    if lexer is None:
        return fgs
    try:
        pos = 0
        for tok_type, value in lexer.get_tokens(code):
            role = _role_for_token(tok_type)
            hexc = theme.role(role) if role else None
            for _ in value:
                if pos >= n:
                    break
                fgs[pos] = hexc or None
                pos += 1
            if pos >= n:
                break
    except Exception:  # pragma: no cover - defensive: never break a diff card
        return [None] * n
    return fgs


def _intraline_masks(old: str, new: str) -> tuple[list[bool], list[bool]]:
    """Boolean per-char "changed" masks for a paired ``-``/``+`` line."""
    sm = SequenceMatcher(None, old, new, autojunk=False)
    if sm.ratio() < _INTRALINE_MIN_RATIO:
        return [False] * len(old), [False] * len(new)
    dm = [False] * len(old)
    am = [False] * len(new)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for x in range(i1, i2):
            dm[x] = True
        for x in range(j1, j2):
            am[x] = True
    return dm, am


# ── parse ────────────────────────────────────────────────────────────────────
def _parse(diff_text: str) -> list[_Row]:
    rows: list[_Row] = []
    old = new = 0
    for raw in diff_text.split("\n"):
        if raw == "":
            continue
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if m:
                old, new = int(m.group(1)), int(m.group(2))
            rows.append(_Row("hunk", raw))
            continue
        # Defensive: a file-header or "\ No newline" marker is not a content row.
        if raw.startswith(("+++", "---", "\\")):
            continue
        # Expand tabs up front so every downstream step (intra-line diff, syntax
        # map, width math) sees the same space-based text and the split grid stays
        # aligned regardless of the file's indentation style.
        if raw.startswith("+"):
            rows.append(_Row("add", raw[1:].expandtabs(_TABSIZE), new_no=new))
            new += 1
        elif raw.startswith("-"):
            rows.append(_Row("del", raw[1:].expandtabs(_TABSIZE), old_no=old))
            old += 1
        else:
            txt = raw[1:] if raw.startswith(" ") else raw
            rows.append(_Row("ctx", txt.expandtabs(_TABSIZE), old_no=old, new_no=new))
            old += 1
            new += 1
    _apply_intraline(rows)
    return rows


def _apply_intraline(rows: list[_Row]) -> None:
    """Pair consecutive del-block → add-block runs and mark changed spans."""
    i = 0
    n = len(rows)
    while i < n:
        if rows[i].kind != "del":
            i += 1
            continue
        j = i
        while j < n and rows[j].kind == "del":
            j += 1
        k = j
        while k < n and rows[k].kind == "add":
            k += 1
        dels, adds = rows[i:j], rows[j:k]
        for d, a in zip(dels, adds):
            dm, am = _intraline_masks(d.text, a.text)
            d.emph, a.emph = dm, am
        i = k if k > i else i + 1


# ── styling primitives ───────────────────────────────────────────────────────
def _rgb(theme: "Theme", value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    if not value.startswith("#"):
        value = theme.role(value)
    return theme._rgb(value)


def _emit_run(
    chars: list[str],
    fgs: list[str | None],
    bgs: list[str | None],
    theme: "Theme",
) -> str:
    """Run-length encode (char, fg-hex, bg-role) triples into ANSI.

    ``fgs`` entries are hex colours (or ``None`` → terminal default); ``bgs``
    entries are role names (or ``None`` → no background). Adjacent identical
    (fg, bg) cells coalesce into one SGR span so the output stays compact."""
    out: list[str] = []
    i = 0
    n = len(chars)
    while i < n:
        fg, bg = fgs[i], bgs[i]
        j = i
        while j < n and fgs[j] == fg and bgs[j] == bg:
            j += 1
        seg = "".join(chars[i:j])
        codes: list[str] = []
        frgb = _rgb(theme, fg)
        if frgb is not None:
            codes.append(f"38;2;{frgb[0]};{frgb[1]};{frgb[2]}")
        brgb = _rgb(theme, bg)
        if brgb is not None:
            codes.append(f"48;2;{brgb[0]};{brgb[1]};{brgb[2]}")
        out.append(f"\x1b[{';'.join(codes)}m{seg}\x1b[0m" if codes else seg)
        i = j
    return "".join(out)


# Per-kind colour roles: (default-fg-role, wash-bg-role, emph-bg-role, lineno-bg-role).
# Context rows carry a neutral wash too so the diff is one aligned block (MiMo
# contextBg) rather than bare lines floating between the tinted add/del bands.
_KIND_ROLES = {
    "add": ("diff_add", "diff_add_bg", "diff_add_emph_bg", "diff_lineno_add_bg"),
    "del": ("diff_del", "diff_del_bg", "diff_del_emph_bg", "diff_lineno_del_bg"),
    "ctx": ("diff_context", "diff_context_bg", None, "diff_context_bg"),
}
_SIGNS = {"add": "+", "del": "-", "ctx": " "}


def _code_cell(
    row: _Row, code_w: int, ctx: "RenderContext", *, side: str | None = None
) -> str:
    """Render one row's ``sign + code`` body, padded to *code_w*.

    *side* selects which text to show in split view (``"old"`` shows del/ctx,
    ``"new"`` shows add/ctx); ``None`` is the unified view (every row)."""
    fg_role, bg_role, emph_role, _ = _KIND_ROLES[row.kind]
    sign = _SIGNS[row.kind]
    # One space between the +/- sign and the code (context rows get the same two
    # leading cells so every row's text starts in the same column).
    body = sign + " " + row.text
    # Plain path: deterministic text for tests / no-TTY transcripts. Fit by
    # display width so wide glyphs don't skew the split grid.
    if not ctx.ansi:
        return _fit_plain(body, code_w) if code_w > 0 else body

    theme = ctx.theme
    default_fg = theme.role(fg_role)
    syntax = _fg_per_char(row.text, _file_path(ctx), theme)
    # Build per-char arrays for: sign + gap + code, then pad with spaces (wash
    # extends). The gap after the sign is part of the row wash, not emphasised.
    chars: list[str] = [sign, " "]
    fgs: list[str | None] = [default_fg, default_fg]
    bgs: list[str | None] = [bg_role, bg_role]
    for idx, ch in enumerate(row.text):
        chars.append(ch)
        fgs.append(syntax[idx] or default_fg)
        if emph_role and idx < len(row.emph) and row.emph[idx]:
            bgs.append(emph_role)
        else:
            bgs.append(bg_role)
    # Width math is in display cells (not char count): split cells truncate to an
    # exact code_w-cell grid so the separator stays plumb; unified rows pad (short)
    # so their wash fills the row to one aligned block (every kind now carries a
    # wash), but are NEVER truncated in unified view — a long line keeps all its
    # content (the Static wraps it).
    fixed = side is not None
    if code_w > 0:
        widths = [_cw(c) for c in chars]
        total = sum(widths)
        if fixed and total > code_w:
            acc = 0
            cut = len(chars)
            for i, w in enumerate(widths):
                if acc + w > code_w:
                    cut = i
                    break
                acc += w
            chars, fgs, bgs = chars[:cut], fgs[:cut], bgs[:cut]
            total = acc  # a wide glyph straddling the edge leaves total < code_w
        if total < code_w and (fixed or bg_role is not None):
            pad = code_w - total
            chars += [" "] * pad
            fgs += [None] * pad
            bgs += [bg_role] * pad
    return _emit_run(chars, fgs, bgs, theme)


def _file_path(ctx: "RenderContext") -> str:
    return getattr(ctx, "_diff_file_path", "") or ""


def _lineno_cell(num: int | None, kind: str, gutter_w: int, ctx: "RenderContext") -> str:
    label = (str(num) if num else "").rjust(gutter_w)
    if not ctx.ansi:
        return label
    _, _, _, lineno_bg = _KIND_ROLES[kind]
    return ctx.theme.style(label, "diff_lineno", ansi=True, bg=lineno_bg)


# ── views ─────────────────────────────────────────────────────────────────────
def _gutter_w(rows: list[_Row]) -> int:
    hi = 0
    for r in rows:
        hi = max(hi, r.old_no, r.new_no)
    return max(2, len(str(hi)))


def _render_unified(rows: list[_Row], ctx: "RenderContext") -> list[str]:
    gw = _gutter_w(rows)
    width = min(max(ctx.width, 40), _MAX_WIDTH)
    code_w = max(10, width - gw - 1) if ctx.ansi else 0
    out: list[str] = []
    for r in rows:
        if r.kind == "hunk":
            out.append(ctx.theme.style(r.text, "diff_hunk", ansi=ctx.ansi))
            continue
        num = r.old_no if r.kind == "del" else r.new_no
        gutter = _lineno_cell(num, r.kind, gw, ctx)
        out.append(f"{gutter} {_code_cell(r, code_w, ctx)}")
    return out


def _split_pairs(rows: list[_Row]) -> list[tuple[object, object]]:
    """Pair rows into (left, right) for the side-by-side view.

    ``left``/``right`` are ``_Row`` or ``None`` (blank cell); a ``("hunk", row)``
    tuple marks a full-width hunk separator."""
    pairs: list[tuple[object, object]] = []
    i = 0
    n = len(rows)
    while i < n:
        r = rows[i]
        if r.kind == "hunk":
            pairs.append(("hunk", r))
            i += 1
        elif r.kind == "ctx":
            pairs.append((r, r))
            i += 1
        elif r.kind == "del":
            j = i
            while j < n and rows[j].kind == "del":
                j += 1
            k = j
            while k < n and rows[k].kind == "add":
                k += 1
            dels, adds = rows[i:j], rows[j:k]
            for idx in range(max(len(dels), len(adds))):
                left = dels[idx] if idx < len(dels) else None
                right = adds[idx] if idx < len(adds) else None
                pairs.append((left, right))
            i = k
        else:  # an add with no preceding del
            pairs.append((None, r))
            i += 1
    return pairs


def _render_split(rows: list[_Row], ctx: "RenderContext") -> list[str]:
    gw = _gutter_w(rows)
    width = min(max(ctx.width, 40), _MAX_WIDTH * 2)
    sep = " │ "
    col_w = max(12, (width - len(sep)) // 2)
    code_w = max(6, col_w - gw - 1)

    def cell(item: object, side: str) -> str:
        if not isinstance(item, _Row):  # blank
            if not ctx.ansi:
                return " " * col_w
            return " " * col_w
        num = item.old_no if side == "old" else item.new_no
        gutter = _lineno_cell(num, item.kind, gw, ctx)
        return f"{gutter} {_code_cell(item, code_w, ctx, side=side)}"

    sep_styled = ctx.theme.style(sep, "diff_hunk", ansi=ctx.ansi)
    out: list[str] = []
    for left, right in _split_pairs(rows):
        if left == "hunk":
            assert isinstance(right, _Row)
            out.append(ctx.theme.style(right.text, "diff_hunk", ansi=ctx.ansi))
            continue
        out.append(f"{cell(left, 'old')}{sep_styled}{cell(right, 'new')}")
    return out


def render_diff(
    diff_text: str,
    file_path: str,
    ctx: "RenderContext",
    *,
    split_threshold: int = 120,
) -> list[str]:
    """Render a unified-diff string into styled rows (MiMo ``<diff>`` parity).

    Returns one string per visual line (no trailing newline). The caller adds the
    block header (``← Edit {path}``) and joins. Chooses split vs unified by
    terminal width like MiMo, and stashes *file_path* on *ctx* so the per-row
    syntax highlighter can infer the lexer."""
    rows = _parse(diff_text)
    if not rows:
        return []
    # Stash the path for the per-row highlighter (cheap, avoids threading it
    # through every cell helper); cleared in a finally so ctx stays clean.
    prev = getattr(ctx, "_diff_file_path", None)
    ctx._diff_file_path = file_path  # type: ignore[attr-defined]
    try:
        use_split = ctx.width > split_threshold and any(
            r.kind in ("add", "del") for r in rows
        )
        return _render_split(rows, ctx) if use_split else _render_unified(rows, ctx)
    finally:
        if prev is None:
            try:
                delattr(ctx, "_diff_file_path")
            except AttributeError:  # pragma: no cover
                pass
        else:  # pragma: no cover - re-entrancy guard
            ctx._diff_file_path = prev  # type: ignore[attr-defined]


__all__ = ["render_diff"]
