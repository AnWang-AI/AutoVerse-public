"""Theme — loaded from a JSON dict, not a flat hardcoded Theme (D8).

A theme is a stepped colour scale (``steps``, 12 entries) plus semantic roles
(``text``/``accent``/``tool_ok``/diff colours ...) plus streaming-markdown and
batch-window knobs. Renderers take *semantic roles* only, never raw steps —
the scale is raw material, the roles are the abstraction layer (P2).

Tests run without a TTY: ``Theme.style`` defaults to a no-op (plain strings)
unless ``ansi=True`` is requested, so renderer output is plain text by default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# ── default stepped scale: warm-tinted charcoal (taste-skill: NO pure black) ─
# Tinted neutrals: the dark end (steps 0–4) carries a faint carbon-brown warmth
# (R slightly > B) and the light end (steps 8–11) a faint cool-white tint
# (B slightly > R), with steps 5–7 the neutral pivot. Luminance progression is
# preserved within ±1.5% per step vs the old pure-neutral scale, so every WCAG
# contrast relationship holds; only the *temperature* moves, so the ~70% of the
# screen that is neutral reads as "breathing" rather than dead grey.
_DEFAULT_STEPS: tuple[str, ...] = (
    "#100e0c", "#181613", "#1f1d1a", "#272522", "#30302f", "#3b3b3e",
    "#48484c", "#595a60", "#767884", "#989aa8", "#c0c2cc", "#eaecf0",
)

# Semantic role names → colours. Single calibrated accent — warm amber/gold
# (taste-skill: max 1 accent, saturation < 80%, no AI-blue/neon).
_DEFAULT_ROLES: dict[str, str] = {
    "text": "#ffffff",
    "text_muted": "#989aa8",
    # step8, not step7 — at #5b5b63 contrast on the charcoal bg was 2.87 (< the
    # WCAG 3.0 UI floor); #767884 lifts it to ~4.4 while staying a scale step
    # and clearly below text_muted, so the three text tiers still read apart.
    # Now carries the cool-white tint of the new scale's light end.
    "text_dim": "#767884",
    "accent": "#e8b339",
    "accent_soft": "#c9952a",
    # A single calm COOL counterpoint (sage/mint) — one of TWO cool hues now
    # (Plan A + accent_blue). Deliberately lower chroma than the amber accent
    # (OKLCH C≈0.07 vs 0.135) so it recedes as a quiet "breathing point" rather
    # than a second competing accent. Carries the "positive / literal-data"
    # semantic axis: success / tool_ok / diff_add / syntax_string all sit on
    # this hue, so "good" and "string literal" read cool against the warm code.
    "accent_cool": "#8eccaa",
    # Second cool hue — a low-saturation slate/fog blue (OKLCH C≈0.06, H≈255°).
    # Extends the cool end from a single sage point to sage + fog-blue, so the
    # palette's hue coverage spreads from the warm half-circle to ~230° of the
    # wheel. Sits at the same low-chroma tier as accent_cool so it reads as a
    # calm counterpoint, NOT the banned AI-blue neon. Carries the "calm info /
    # exploration / meta-annotation" semantic axis: info status, the scout
    # sub-agent family, and syntax decorators.
    "accent_blue": "#8a9ec0",
    # Second tint kept warm (clay/terracotta) — used by the debugger/researcher
    # sub-agent family; distinct from accent_cool's semantic role.
    "secondary": "#c08a6a",
    "mode_worktree": "#b5a4cc",
    "border": "#3b3b3e",
    "border_subtle": "#272522",
    "bg_panel": "#181613",
    "bg_element": "#1f1d1a",
    # Success sits on the cool sage axis (accent_cool) so "positive" reads with
    # one coherent cool hue everywhere (success / tool_ok / diff_add).
    "success": "#74c09a",
    "warning": "#e0a458",
    "error": "#d16b6b",
    # Informational status — now the fog-blue hue (accent_blue) so "info" reads
    # as calm-blue, clearly distinct from the sage success/warning axis. Still
    # low-saturation so it stays quiet; success/warning/error carry the coloured
    # status load.
    "info": "#8a9ec0",
    "tool_running": "#e8b339",
    "tool_ok": "#74c09a",
    "tool_err": "#d16b6b",
    # ── sub-agent per-type accents (5-family split) ──────────────────────────
    # Each family gets its own hue; within a family the two members differ by
    # lightness (dark / bright) so siblings read as kin yet stay distinguishable.
    #   scout         → fog-blue  (exploration: calm scan, strongest temp contrast)
    #   architect     → dark gold  (planning: steady design)      = accent_soft
    #   builder       → bright gold (construction: active output)  = accent
    #   validator     → dark sage  (verification: pass)           = success
    #   reviewer      → bright sage (review: inspect)             = accent_cool
    #   debugger      → dark terracotta (troubleshooting)         = secondary
    #   researcher    → bright terracotta (digging in)            = syntax_keyword
    #   generalist    → neutral grey (no specific role)           = text_muted
    # Hex values are shared with the semantic roles above for visual coherence,
    # but use dedicated keys so sub-agent colours can be tuned independently
    # from syntax/status colours in the future.
    "agent_scout": "#8a9ec0",
    "agent_architect": "#c9952a",
    "agent_builder": "#e8b339",
    "agent_validator": "#74c09a",
    "agent_reviewer": "#8eccaa",
    "agent_debugger": "#c08a6a",
    "agent_researcher": "#d68f72",
    "agent_generalist": "#989aa8",
    # Diff add foreground sits on the sage axis (slightly brighter than success
    # so it stays legible on the green wash); delete stays on the red axis.
    "diff_add": "#8fd0b0",
    "diff_del": "#e08585",
    "diff_add_bg": "#142420",
    "diff_del_bg": "#2a1719",
    # Word-level (intra-line) emphasis backgrounds — a brighter tint laid over
    # the changed token spans within a +/- line, so the eye lands on *what*
    # changed (MiMo's per-token diff highlight). Distinct from the calm
    # whole-line wash above.
    "diff_add_emph_bg": "#2b5544",
    "diff_del_emph_bg": "#5e2b2e",
    # Calm neutral wash on context rows so the whole diff reads as one aligned
    # block (MiMo contextBg) — without it, unfilled context lines float between
    # the tinted add/del bands and look misaligned.
    "diff_context_bg": "#1d1b1c",
    "diff_context": "#9a9aa2",
    "diff_hunk": "#787880",
    "diff_lineno": "#5b5b63",
    # ── syntax-highlight palette (code inside diffs) ─────────────────────────
    # A WARM, low-saturation set (gruvbox/everforest lineage) that sits with the
    # amber accent on the charcoal base — deliberately NOT Pygments' default
    # neon themes (taste-skill: one warm palette, saturation < 80%, no AI-blue/
    # neon). Each role is mapped from Pygments token types in diff_render.
    # Plan A: strings move to the COOL sage axis so they pop against the warm
    # keywords/numbers (the biggest code-legibility win); the remaining warm
    # tokens are grouped into two clearer sub-clusters — GOLD (values & names:
    # number/function/class) and TERRACOTTA (language/structure: keyword/
    # constant/builtin) — with greys for the rest. Net perceived buckets:
    # grey / sage / gold / terracotta — one warm palette + two cool points
    # (sage + fog-blue for decorators/type annotations).
    "syntax_default": "#c6c8d0",   # identifiers / code body — cool-white grey
    "syntax_keyword": "#d68f72",   # def/return/if/import — terracotta
    "syntax_string": "#8eccaa",    # string literals — COOL sage (was olive)
    "syntax_comment": "#6e6f76",   # comments — muted grey
    "syntax_number": "#dcae62",    # numeric literals — gold
    "syntax_function": "#e3bd7e",  # function / method names — gold (bright)
    "syntax_class": "#d4b985",     # class / type names — gold (tan)
    "syntax_constant": "#cf8a68",  # True/False/None, constants — terracotta
    "syntax_operator": "#a6a8b4",  # operators / punctuation — cool-white grey
    "syntax_builtin": "#d2986a",   # builtins (len/print/…) — terracotta/orange
    # Decorators (@staticmethod, @property) — fog-blue. Decorators are a
    # "meta/annotation" concept; the cool-blue reads against the warm
    # keyword/function cluster and lands the eye on the @ line. Previously
    # Name.Decorator fell through to syntax_function (gold) and was invisible
    # among method names.
    "syntax_decorator": "#8a9ec0",
    # Type annotations (x: int, -> str) — a cool grey aligned with text_muted
    # luminance so annotations recede as "meta info" rather than competing with
    # the function/class gold. Previously untokened → fell back to plain text.
    "syntax_type_annotation": "#989aa8",
    # Line-number gutter backgrounds, tinted to match the row's add/del wash so
    # the gutter reads as part of the line (MiMo addedLineNumberBg parity).
    "diff_lineno_add_bg": "#142420",
    "diff_lineno_del_bg": "#2a1719",
    "diff_lineno_bg": "#181613",
}


@dataclass(frozen=True)
class Theme:
    """Theme derived from a JSON dict (D8). Roles are the renderer-facing API."""

    name: str = "default"
    # The TUI is dark-only by design (the whole CSS layer + diff/fence colours
    # assume a charcoal base). A `light` mode was half-wired — the Textual theme
    # flipped but the hardcoded CSS stayed dark — so the knob was removed rather
    # than ship a broken half-light surface. Re-add a real light palette later
    # if needed (it must also re-theme the app CSS, not just this dataclass).
    steps: tuple[str, ...] = _DEFAULT_STEPS
    roles: Mapping[str, str] = field(default_factory=lambda: dict(_DEFAULT_ROLES))
    spinner_frames: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    streaming_markdown: bool = True
    batch_window_ms: int = 16

    # ── semantic roles (renderers call these, never `steps[k]`) ──────────────
    def role(self, name: str) -> str:
        return self.roles.get(name, "")

    @property
    def text(self) -> str:
        return self.roles.get("text", self.steps[-1])

    @property
    def text_muted(self) -> str:
        return self.roles.get("text_muted", self.steps[-3])

    @property
    def accent(self) -> str:
        return self.roles.get("accent", self.steps[8])

    @property
    def tool_ok(self) -> str:
        return self.roles.get("tool_ok", self.steps[8])

    @property
    def tool_err(self) -> str:
        return self.roles.get("tool_err", self.steps[8])

    @property
    def tool_running(self) -> str:
        return self.roles.get("tool_running", self.steps[8])

    def style(
        self,
        text: str,
        role: str,
        *,
        ansi: bool = False,
        bg: str | None = None,
        bold: bool = False,
        strike: bool = False,
    ) -> str:
        """Return *text*, optionally wrapped with ANSI for *role*.

        Default (ansi=False) returns the plain string — keeps renderer output
        deterministic and TTY-free for tests. ``bg`` is a background *role* name
        (24-bit ``48;2;...``); ``bold`` adds ``1m``; ``strike`` adds ``9m``
        (denied / benign-failure marker, the opencode/MiMo ``STRIKETHROUGH``
        convention). ``Text.from_ansi`` parses all four for the TUI.
        """
        if not ansi:
            return text
        codes: list[str] = []
        if bold:
            codes.append("1")
        if strike:
            codes.append("9")
        fg = self._rgb(self.role(role))
        if fg is not None:
            codes.append(f"38;2;{fg[0]};{fg[1]};{fg[2]}")
        if bg is not None:
            bgc = self._rgb(self.role(bg))
            if bgc is not None:
                codes.append(f"48;2;{bgc[0]};{bgc[1]};{bgc[2]}")
        if not codes:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    @staticmethod
    def _rgb(hex_color: str) -> tuple[int, int, int] | None:
        if not hex_color.startswith("#") or len(hex_color) != 7:
            return None
        return (
            int(hex_color[1:3], 16),
            int(hex_color[3:5], 16),
            int(hex_color[5:7], 16),
        )

    # ── construction from JSON ───────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Theme":
        name = str(data.get("name", "default"))
        raw_steps = data.get("steps")
        steps = tuple(str(s) for s in raw_steps) if isinstance(raw_steps, (list, tuple)) else _DEFAULT_STEPS
        if len(steps) != 12:
            steps = _DEFAULT_STEPS
        raw_roles = data.get("roles")
        roles = dict(_DEFAULT_ROLES)
        if isinstance(raw_roles, Mapping):
            for k, v in raw_roles.items():
                roles[str(k)] = str(v)
        frames = cls.spinner_frames
        raw_frames = data.get("spinner_frames")
        if isinstance(raw_frames, (list, tuple)) and raw_frames:
            frames = tuple(str(f) for f in raw_frames)
        return cls(
            name=name,
            steps=steps,
            roles=roles,
            spinner_frames=frames,
            streaming_markdown=bool(data.get("streaming_markdown", True)),
            batch_window_ms=_as_int(data.get("batch_window_ms", 16), 16),
        )

    @classmethod
    def load(cls, name_or_path: str | None = None) -> "Theme":
        """Load a theme by built-in name or JSON file path; fall back to default."""
        if not name_or_path or name_or_path in ("default", "dark"):
            return cls()
        path = Path(name_or_path).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return cls()
            if isinstance(data, Mapping):
                return cls.from_dict(data)
        return cls()


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


DEFAULT_THEME = Theme()


__all__ = ["Theme", "DEFAULT_THEME"]
