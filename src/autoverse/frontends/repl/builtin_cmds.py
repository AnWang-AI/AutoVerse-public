"""Built-in slash commands — registered via @command (single source of truth).

Each command is declared once; help/completion/keybinding/palette all derive
from the registry (R3, P1). Skills are registered as *ordinary* commands by
:func:`register_skill_commands`, so ``/skillname`` dispatches through the same
table lookup — not a fallback special-case (R4).

``/rewind`` performs the joint file + history revert per docs/architecture/runtime-flow.md §3.3a
(KDR-12): preview → approval → atomic revert, all driven through the public
``EngineHandle`` facade (the frontend never reflects engine privates, R8).
"""

from __future__ import annotations

import platform
import shlex
import shutil
import sys
import time
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, cast

from autoverse.app.context import AppContext
from autoverse.frontends.repl import queries
from autoverse.frontends.repl.commands import (
    CommandContext,
    CommandRegistry,
    CommandResult,
    CommandSpec,
)

_EXTENSION_BUILTIN_COMMANDS = frozenset({"skills", "mcp", "plugin"})

if TYPE_CHECKING:
    from autoverse.frontends.repl.dialogs import SelectOption


def _engine(ctx: object) -> object:
    cc = cast(CommandContext, ctx)
    return cc.engine


def _registry_of(ctx: object) -> CommandRegistry:
    cc = cast(CommandContext, ctx)
    return cc.registry


def _app(ctx: object) -> AppContext:
    cc = cast(CommandContext, ctx)
    return cast(AppContext, cc.app)


def _idle(ctx: object) -> bool:
    return not getattr(ctx, "is_running", False)


def _usd(value: float) -> str:
    return f"${value:.4f}"


def _feedback(ctx: object, message: str, variant: str = "info") -> CommandResult:
    """Transient feedback: a Toast when a DialogHost is present (§8 I7), else
    a printed line (headless / non-TUI). Keeps short status out of the scroll
    region in the TUI while staying script-friendly elsewhere."""
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is not None:
        host.toast(message, variant=variant)
        return CommandResult(display="")
    return CommandResult(display=message)


async def _report(ctx: object, title: str, body: str, *, min_lines: int = 4) -> CommandResult:
    """Read-only report → scrollable viewer when it would otherwise flood the
    scroll region (§8 I9): if a DialogHost is present **and** the body is at
    least ``min_lines`` lines, open ``TextScreen``; otherwise print to the scroll
    region. The line threshold (default 4) means short reports (a 1-line tree, a
    "3 servers" status) stay inline — and auto-promote to a viewer once they grow
    — so no per-command hand-classification is needed (avoids visual pollution
    without popping a viewer for a one-liner)."""
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    lines = body.count("\n") + 1 if body else 0
    if host is not None and lines >= min_lines:
        await host.show_text(body, title=title)
        return CommandResult(display="")
    return CommandResult(display=body)


async def _confirm(ctx: object, message: str, *, title: str = "Confirm") -> bool | None:
    """Ask for confirmation via the overlay (§8 I8) when a DialogHost is present;
    returns None when no host (caller decides the headless default)."""
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is None:
        return None
    return await host.confirm(message, title=title)


# ── command handlers (registered into a fresh registry by build_default_registry) ─

async def _cmd_help(ctx: object, args: str) -> CommandResult:
    # The grouped command list can be 30+ lines — show it in a scrollable viewer
    # (§8 I9) rather than flooding the conversation. ctrl+p is the *interactive*
    # variant; /help is the full reference. Headless → printed.
    return await _report(ctx, "Commands", "\n".join(_registry_of(ctx).help_lines()))


async def _cmd_clear(ctx: object, args: str) -> CommandResult:
    engine = _engine(ctx)
    clear = getattr(engine, "clear_in_place", None)
    if clear is None:
        return CommandResult(display="clear unavailable", is_error=True)
    # Destructive: confirm via overlay when a DialogHost is present (§8 I8).
    approved = await _confirm(
        ctx, "Clear all conversation history? The session is kept.",
        title="Clear history",
    )
    if approved is False:
        return CommandResult(display="")  # cancelled in the overlay
    n = await cast(Callable[[], Awaitable[int]], clear)()
    # Engine history is now empty; ask the frontend to wipe the visible
    # scrollback too (the TUI owns its widgets — see CommandResult.should_clear).
    feedback = _feedback(ctx, f"cleared {n} messages (session preserved)", "success")
    return replace(feedback, should_clear=True)


async def _cmd_compact(ctx: object, args: str) -> CommandResult:
    engine = _engine(ctx)
    compact = getattr(engine, "compact", None)
    if compact is None:
        return CommandResult(display="compact unavailable", is_error=True)
    # Folding earlier turns into a summary mutates the live context and cannot be
    # undone, so confirm via overlay when a DialogHost is present (§8 I8), exactly
    # like /clear. Headless (_confirm → None) proceeds so scripts aren't blocked.
    approved = await _confirm(
        ctx,
        "Compact the conversation now? Earlier messages are folded into a "
        "summary to reclaim the context window — this can't be undone.",
        title="Compact context",
    )
    if approved is False:
        return CommandResult(display="")  # cancelled in the overlay
    stats = await cast(Callable[[], Awaitable[dict[str, int]]], compact)()
    before = stats.get("tokens_before", 0)
    after = stats.get("tokens_after", 0)
    # The compaction emits a COMPACTING event which the TUI renders as a calm
    # boundary divider in the scrollback (the single, persistent visual cue —
    # MiMo parity). So in the TUI we add NO toast and KEEP the history visible
    # (no should_clear): the divider already marks where context was folded,
    # avoiding the old duplicate "compacting … / compacted …" pair. Headless /
    # non-TUI still gets a one-line confirmation for scriptability.
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    if get_dialog_host(ctx) is not None:
        return CommandResult(display="")
    return CommandResult(display=f"compacted {before:,}→{after:,} tok")


def _fmt_price(in_p: float, out_p: float) -> str:
    """Render per-1M-token pricing, or a placeholder when unpriced."""
    if in_p <= 0 and out_p <= 0:
        return "price n/a"
    return f"${in_p:g}/${out_p:g} per 1M (in/out)"


async def _cmd_model(ctx: object, args: str) -> CommandResult:
    engine = _engine(ctx)
    model_options = _sorted_model_options()
    current = _current_model(ctx, engine)

    # No args: show the current main model, sub-agent tier mapping, and catalogue.
    # Use /tiers to edit the sub-agent tier mapping; /model only switches the
    # main agent model so the two controls stay independent.
    if not args:
        lines = [f"current model: {current}" if current else "model: (unknown)"]
        tiers = _current_tiers(ctx)
        if tiers is not None:
            lines.append("sub-agent tiers (set via /tiers):")
            for name in ("deep", "balanced", "fast"):
                val = getattr(tiers, name, "") or "(unset → main model)"
                lines.append(f"  {name:<9} {val}")
        if model_options:
            lines.append("available models:")
            for opt in model_options:
                mid = str(opt.get("model_id", ""))
                marker = "*" if current and (mid == current) else " "
                price = _fmt_price(
                    float(cast(float, opt.get("input_price_per_mtok", 0.0) or 0.0)),
                    float(cast(float, opt.get("output_price_per_mtok", 0.0) or 0.0)),
                )
                ctx_win = int(cast(int, opt.get("context_window", 0) or 0))
                source = str(opt.get("source", "") or "?")
                lines.append(
                    f" {marker} {mid:<28} {source:<20} {price} · {ctx_win:,} tok ctx"
                )
        return await _report(ctx, "Models", "\n".join(lines))

    # Explicit `/model <id>`: a direct main-agent switch (tiers untouched).
    return await _do_switch_model(engine, args.strip(), ctx)


async def _cmd_tiers(ctx: object, args: str) -> CommandResult:
    """View or edit sub-agent model tiers without changing the main model."""
    model_options = _sorted_model_options()
    if args.strip():
        return CommandResult(display="usage: /tiers", is_error=True)

    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is not None and model_options:
        return await _select_model_tiers(ctx, model_options)

    tiers = _current_tiers(ctx)
    if tiers is None:
        return CommandResult(display="sub-agent tiers unavailable", is_error=True)
    lines = ["sub-agent tiers:"]
    for name in ("deep", "balanced", "fast"):
        val = getattr(tiers, name, "") or "(unset → main model)"
        lines.append(f"  {name:<9} {val}")
    return CommandResult(display="\n".join(lines))


def _current_model(ctx: object, engine: object) -> object:
    """Live main-agent model; ``switch_model`` writes AppContext.model."""
    return getattr(_app(ctx), "model", None) or getattr(engine, "model", None)


def _sorted_model_options() -> list[dict[str, object]]:
    from autoverse.infra.llm.registry import MODEL_OPTIONS

    # Show the picker cheapest-first: sort by input price, then output price.
    def _price_key(opt: dict[str, object]) -> tuple[float, float]:
        return (
            float(cast(float, opt.get("input_price_per_mtok", 0.0) or 0.0)),
            float(cast(float, opt.get("output_price_per_mtok", 0.0) or 0.0)),
        )

    return sorted(MODEL_OPTIONS, key=_price_key)


def _current_tiers(ctx: object) -> object | None:
    """The live ``config.model_tiers`` sub-config, or None when unavailable."""
    config = getattr(_app(ctx), "config", None)
    return getattr(config, "model_tiers", None)


def _build_model_options(
    model_options: list[dict[str, object]], selected: str | None
) -> list[SelectOption[str]]:
    """Build the SelectOption rows for the model catalogue (current → selected)."""
    from autoverse.frontends.repl.dialogs import SelectOption

    options: list[SelectOption[str]] = []
    for opt in model_options:
        mid = str(opt.get("model_id", ""))
        price = _fmt_price(
            float(cast(float, opt.get("input_price_per_mtok", 0.0) or 0.0)),
            float(cast(float, opt.get("output_price_per_mtok", 0.0) or 0.0)),
        )
        ctx_win = int(cast(int, opt.get("context_window", 0) or 0))
        modality = "multimodal" if bool(opt.get("vision", False)) else "text-only"
        source = str(opt.get("source", "") or "?")
        options.append(
            SelectOption(
                title=mid,
                value=mid,
                description=f"[{source}]",
                footer=f"{price} · {ctx_win:,} tok · {modality}",
                category="models",
                current=bool(selected and mid == selected),
                # Let the source be fuzzy-searchable too (e.g. type "official").
                search_key=f"{mid} {source}",
            )
        )
    return options


async def _select_model_tiers(ctx: object, model_options: list[dict[str, object]]) -> CommandResult:
    """Pick a model for each tier (deep → balanced → fast), then apply (§3.4).

    The three picks populate ``config.model_tiers`` only; the main agent keeps
    its own ``model.model_id``. Cancelling any prompt aborts the whole flow with
    no change."""
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is None:
        return CommandResult(display="interactive picker unavailable", is_error=True)

    tiers = _current_tiers(ctx)
    # (tier key, human label, current value) — order = deep → balanced → fast.
    prompts = (
        ("deep", "DEEP tier — orchestration / design / debugging"),
        ("balanced", "BALANCED tier — execute a given plan"),
        ("fast", "FAST tier — search / CI-run / templated writes"),
    )
    picks: dict[str, str] = {}
    for key, blurb in prompts:
        existing = str(getattr(tiers, key, "") or "") if tiers is not None else ""
        options = _build_model_options(model_options, existing or None)
        chosen = await host.select(
            options,
            title=f"Select {key} model  ({blurb})",
            placeholder="search models…",
        )
        if chosen is None:
            return CommandResult(display="model tier selection cancelled")
        picks[key] = str(chosen)

    applied = _apply_model_tiers(ctx, picks)
    summary = (
        f"tiers set — deep={picks['deep']}  balanced={picks['balanced']}  "
        f"fast={picks['fast']}"
    )
    if not applied:
        return CommandResult(display=summary + "\n(warning: could not apply tiers)")
    # Write-through: the picker records only sub-agent tier defaults in the
    # global config file. The main model remains controlled by /model.
    saved, warn = _persist_model_defaults(ctx, tiers=picks)
    extra = ["saved as default (global config)"] if saved else []
    if warn:
        extra.append(warn)
    if host is not None:
        host.toast("model tiers updated", variant="success")
        if warn:
            host.toast(warn, variant="warning")
    if extra:
        return CommandResult(display=summary + "\n" + "\n".join(extra))
    return CommandResult(display=summary)


def _persist_model_defaults(
    ctx: object, *, model_id: str | None = None, tiers: dict[str, str] | None = None
) -> tuple[bool, str | None]:
    """Persist the default main model and/or sub-agent tiers to the global config.

    Only the changed dotted keys are merged into ``~/.autoverse/config.json``
    (the ``global`` layer) — never a full effective-config dump — so project/env/
    cli overrides keep winning per the layering contract. Returns
    ``(saved, warning)`` where *warning* names any higher layer that shadows the
    saved value here (so the user knows the default won't take effect in this
    context). Persistence failures degrade gracefully into ``(False, reason)``."""
    import os

    from autoverse.infra.config.loader import load_config
    from autoverse.infra.config.writer import (
        ConfigPersistError,
        merge_into_config_file,
        resolve_global_config_path,
    )

    changes: dict[str, object] = {}
    if model_id:
        changes["model.model_id"] = model_id
    if tiers:
        for name in ("deep", "balanced", "fast"):
            if name in tiers:
                changes[f"model_tiers.{name}"] = tiers[name]
    if not changes:
        return (False, None)

    try:
        merge_into_config_file(resolve_global_config_path(), changes)
    except (ConfigPersistError, OSError, ValueError) as exc:
        return (False, f"could not save default ({exc})")

    # Provenance (best-effort): re-load the layered config and check whether a
    # higher layer (project/env) shadows any key we just wrote to global.
    shadowed: list[str] = []
    try:
        app = _app(ctx)
        cwd = Path(getattr(app, "cwd", Path.cwd()))
        loaded = load_config(cwd, os.environ, {})
        layers: set[str] = set()
        for key in changes:
            src = loaded.sources.get(key)
            if src is not None and src not in ("global", "default"):
                layers.add(src)
        shadowed = sorted(layers)
    except Exception:  # noqa: BLE001 — provenance is advisory, never fatal
        shadowed = []
    if shadowed:
        return (
            True,
            f"note: saved to global config but overridden by the "
            f"{', '.join(shadowed)} layer here (won't take effect)",
        )
    return (True, None)


def _apply_model_tiers(ctx: object, picks: dict[str, str]) -> bool:
    """Swap the live config's ``model_tiers`` without changing the main model.

    Config is a frozen pydantic tree (P4: hot-swap = new tree), so we build a new
    tree via ``model_copy`` and reassign ``AppContext.config`` (a mutable attr the
    sub-agent runner reads fresh on each spawn). Returns False if the config could
    not be swapped."""
    app = _app(ctx)
    config = getattr(app, "config", None)
    copier = getattr(config, "model_copy", None)
    if config is None or not callable(copier):
        return False
    try:
        new_tiers = config.model_tiers.model_copy(update=dict(picks))
        app.config = config.model_copy(update={"model_tiers": new_tiers})
    except Exception:  # noqa: BLE001 — never let a config swap crash the command
        return False
    return True


async def _do_switch_model(engine: object, model_id: str, ctx: object) -> CommandResult:
    switch = getattr(engine, "switch_model", None)
    if switch is None:
        return CommandResult(display="switch_model unavailable", is_error=True)
    cast(Callable[[str], None], switch)(model_id)
    # A direct `/model <id>` switch also becomes the persisted default main model
    # (model.model_id); sub-agent tiers are left untouched.
    saved, warn = _persist_model_defaults(ctx, model_id=model_id)
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is not None:
        host.toast(f"model → {model_id}", variant="success")
        if warn:
            host.toast(warn, variant="warning")
        return CommandResult(display="")  # feedback via toast, not scroll region
    msg = f"model switched to {model_id}"
    if saved:
        msg += " (saved as default)"
    if warn:
        msg += f"\n{warn}"
    return CommandResult(display=msg)


async def _cmd_rewind(ctx: object, args: str) -> CommandResult:
    """Joint file + history revert (docs/architecture/runtime-flow.md §3.3a, KDR-12).

    Preview → approval → atomic revert, all via the public EngineHandle facade.
    Degrades gracefully if the engine does not expose rewind helpers.
    """
    cc = cast(CommandContext, ctx)
    engine = cc.engine
    resolve = getattr(engine, "resolve_rewind_target", None)
    rewind = getattr(engine, "rewind", None)
    if resolve is None or rewind is None:
        return CommandResult(display="rewind not supported by this engine", is_error=True)
    ref = await cast(Callable[[str], Awaitable[object]], resolve)(args.strip())
    if ref is None:
        return CommandResult(display="no workspace snapshot for that message", is_error=True)
    preview = getattr(engine, "preview_rewind", None)
    diff = ""
    if preview is not None:
        diff = await cast(Callable[[object], Awaitable[str]], preview)(ref)
    # Destructive op. Prefer the confirmation overlay (§8 I8) when a DialogHost
    # is present; else fall back to the PermissionPort adapter's text confirm.
    message = "Revert files and truncate the session?"
    if diff.strip():
        message += f"\n\n{diff}"
    approved = await _confirm(ctx, message, title="Rewind")
    if approved is None:  # no overlay host → try the permission adapter
        perms = cc.permissions
        confirm = getattr(perms, "confirm", None) if perms is not None else None
        if confirm is not None:
            approved = await cast(Callable[[str], Awaitable[bool]], confirm)(message)
    if approved is False:
        return CommandResult(display="rewind cancelled")
    await cast(Callable[[object], Awaitable[None]], rewind)(ref)
    return _feedback(ctx, "rewound: files restored + session truncated", "success")


# ── view / observability (read-only, derive from app slots + meter, P3) ──────

async def _cmd_context(ctx: object, args: str) -> CommandResult:
    snap = queries.context_snapshot(_app(ctx))
    if snap is None:
        return CommandResult(display="context: no config available", is_error=True)
    if snap.window <= 0:
        return CommandResult(display="context: window size unknown (no config)")
    pct = 100.0 * snap.tokens_used / snap.window if snap.window else 0.0
    if queries.total_usage(_app(ctx)) is None:
        return CommandResult(
            display=(
                f"context: window {snap.window:,} tok · "
                f"compact at {snap.compact_threshold:,} tok · "
                "live token usage unavailable (no trace meter)"
            )
        )
    return CommandResult(
        display=(
            f"context: {snap.tokens_used:,}/{snap.window:,} tok ({pct:.1f}%) · "
            f"compact threshold {snap.compact_threshold:,} tok"
        )
    )


async def _cmd_cost(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    by_model = queries.cost_by_model(app)
    if not by_model:
        return CommandResult(
            display="cost: no billing data yet (trace meter not reachable)"
        )
    lines = ["cost by model:"]
    for model, cb in sorted(by_model.items()):
        suffix = "" if cb.known else " (unpriced model)"
        lines.append(f"  {model:<24} {_usd(cb.total_usd)}{suffix}")
    total = queries.total_cost(app)
    if total is not None:
        lines.append(f"  {'TOTAL':<24} {_usd(total.total_usd)}")
    return await _report(ctx, "Cost by model", "\n".join(lines), min_lines=3)


async def _cmd_usage(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    by_model = queries.usage_by_model(app)
    if not by_model:
        return CommandResult(
            display="usage: no token data yet (trace meter not reachable)"
        )
    costs = queries.cost_by_model(app)
    lines = ["token usage by model (cache-read 0.10x / cache-write 1.25x):"]
    for model, u in sorted(by_model.items()):
        cb = costs.get(model)
        cost_str = _usd(cb.total_usd) if cb is not None else "n/a"
        lines.append(
            f"  {model:<24} in={u.input_tokens:,} out={u.output_tokens:,} "
            f"cache_r={u.cache_read_tokens:,} cache_w={u.cache_write_tokens:,} "
            f"· {cost_str}"
        )
    total = queries.total_usage(app)
    if total is not None:
        lines.append(f"  total tokens: {total.total:,}")
    return await _report(
        ctx, "Token usage by model", "\n".join(lines), min_lines=3
    )


async def _cmd_diff(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    diff = app.extra.get("last_diff") if isinstance(app.extra, dict) else None
    if isinstance(diff, str) and diff.strip():
        return await _report(ctx, "Session diff", diff)
    return CommandResult(display="diff: no changes recorded this session")


async def _cmd_export(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    trace = getattr(app, "trace", None)
    if trace is None:
        return CommandResult(display="export: no trace to export", is_error=True)
    model = getattr(trace, "model", None)
    if model is None:
        return CommandResult(display="export: no trace to export", is_error=True)
    to_dict = getattr(model, "to_dict", None)
    if to_dict is None:
        return CommandResult(display="export: trace not serialisable", is_error=True)

    # The output format follows the target extension: ``.html``/``.htm`` and
    # ``.md``/``.markdown`` render the rich presentation via the observ exporters
    # (O4, §4.6); anything else (default ``.json``) dumps the raw trace model.
    target = Path(args.strip()) if args.strip() else Path("autoverse-trace.json")
    suffix = target.suffix.lower()
    try:
        if suffix in (".html", ".htm"):
            from autoverse.infra.observ.export import export_html

            target.write_text(export_html(model), encoding="utf-8")
        elif suffix in (".md", ".markdown"):
            from autoverse.infra.observ.export import export_markdown

            target.write_text(export_markdown(model), encoding="utf-8")
        else:
            import json

            payload = cast(Callable[[], object], to_dict)()
            target.write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
    except OSError as exc:
        return CommandResult(display=f"export failed: {exc}", is_error=True)
    return _feedback(ctx, f"exported trace → {target}", "success")


async def _cmd_memory(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    prompt = getattr(app, "prompt", None)
    sources = getattr(app, "extension_sources", ())
    if prompt is None and not sources:
        return CommandResult(display="memory: no memory sources loaded")
    parts: list[str] = []
    if prompt is not None:
        parts.append(f"prompt assembler: {type(prompt).__name__}")
    if sources:
        parts.append(f"{len(sources)} extension source(s) loaded")
    return CommandResult(display="memory: " + "; ".join(parts))


async def _cmd_sessions(ctx: object, args: str) -> CommandResult:
    store = getattr(_app(ctx), "store", None)
    lister = getattr(store, "list_sessions", None)
    if lister is None:
        return CommandResult(display="sessions: no store configured", is_error=True)
    try:
        ids = await cast(Callable[[], Awaitable[list[str]]], lister)()
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the REPL
        return CommandResult(display=f"sessions: store error: {exc}", is_error=True)
    if not ids:
        return CommandResult(display="sessions: none stored")

    # Interactive: pick a session in an overlay (§8 I9). Fallback to text list.
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is not None:
        from autoverse.frontends.repl.dialogs import SelectOption

        current = queries.session_id_of(_app(ctx))
        options = [
            SelectOption(title=sid, value=sid, category="sessions",
                         current=bool(current and sid == current))
            for sid in ids
        ]
        chosen = await host.select(options, title="Sessions",
                                   placeholder="search sessions…")
        if chosen is None:
            return CommandResult(display="")
        return CommandResult(display=f"session selected: {chosen} (use --resume {chosen} to load)")

    # Text fallback (no DialogHost, e.g. the WebUI): cap the dump so a large
    # store doesn't flood the scrollback — the full set stays reachable elsewhere
    # (TUI overlay / WebUI sidebar).
    shown = ids[:20]
    lines = [f"stored sessions ({len(ids)} total):"] + [f"  {sid}" for sid in shown]
    if len(ids) > len(shown):
        lines.append(f"  … and {len(ids) - len(shown)} more")
    return CommandResult(display="\n".join(lines))


async def _cmd_session_tree(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    sid = queries.session_id_of(app)
    if sid is None:
        return CommandResult(
            display="session-tree: no active session (trace not reachable)"
        )
    jobs = getattr(app, "jobs", None)
    note = "" if jobs is not None else " (no job store → sub-agent tree unavailable)"
    return await _report(ctx, "Session tree", f"{sid} (root){note}")


# ── system / diagnostics ─────────────────────────────────────────────────────

async def _cmd_doctor(ctx: object, args: str) -> CommandResult:
    lines = ["environment self-check:"]
    lines.append(f"  python      {platform.python_version()} ({sys.executable})")
    for dep in ("textual", "PIL", "redis"):
        present = find_spec(dep) is not None
        lines.append(f"  {dep:<11} {'present' if present else 'absent'}")

    # ── core dependency versions ──
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    for dep in ("pydantic", "httpx", "rich", "textual"):
        try:
            ver = _pkg_version(dep)
            lines.append(f"  {dep:<11} {ver}")
        except PackageNotFoundError:
            lines.append(f"  {dep:<11} absent")

    app = _app(ctx)
    cwd = getattr(app, "cwd", Path.cwd())
    is_git = (Path(cwd) / ".git").exists()
    lines.append(f"  cwd         {cwd}")
    lines.append(f"  git repo    {'yes' if is_git else 'no'}")
    lines.append(f"  ripgrep     {'present' if shutil.which('rg') else 'absent'}")

    # ── network connectivity ──
    import time as _time

    import httpx

    start = _time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            await client.get("https://pypi.org")
        elapsed = (_time.monotonic() - start) * 1000
        lines.append(f"  network     ok ({elapsed:.0f}ms)")
    except Exception as exc:  # noqa: BLE001 — diagnostics are best-effort
        lines.append(f"  network     unreachable: {exc}")

    return await _report(ctx, "Environment self-check", "\n".join(lines))


def _config_sources(app: object) -> dict[str, str]:
    """Best-effort provenance map (dotted key → winning layer) for the live config.

    Re-runs the layered loader purely to read its ``sources`` view; any failure
    (corrupt file, missing cwd) degrades to an empty map so ``/config`` never
    crashes on introspection."""
    import os

    from autoverse.infra.config.loader import load_config

    try:
        cwd = Path(getattr(app, "cwd", Path.cwd()))
        return dict(load_config(cwd, os.environ, {}).sources)
    except Exception:  # noqa: BLE001 — provenance is advisory
        return {}


def _render_models_section(app: object) -> list[str]:
    """Human-readable main model + sub-agent tier mapping (with provenance).

    Unset tiers ("") are shown as "unset → main model" rather than a bare empty
    string, matching ``/model`` and ``/tiers`` output. The live
    ``AppContext.model`` is the source of truth for the main model (it reflects
    ``/model`` switches)."""
    config = getattr(app, "config", None)
    model_cfg = getattr(config, "model", None)
    tiers = getattr(config, "model_tiers", None)
    if model_cfg is None or tiers is None:
        return []
    sources = _config_sources(app)

    def _src(key: str) -> str:
        layer = sources.get(key)
        return f"   [source: {layer}]" if layer else ""

    main = getattr(app, "model", None) or getattr(model_cfg, "model_id", "") or "(unset)"
    lines = ["models:", f"  main model   {main}{_src('model.model_id')}"]
    lines.append("  sub-agent tiers:")
    for name in ("deep", "balanced", "fast"):
        val = str(getattr(tiers, name, "") or "")
        shown = val if val else "unset → main model"
        suffix = _src(f"model_tiers.{name}") if val else ""
        lines.append(f"    {name:<9} {shown}{suffix}")
    return lines


async def _cmd_config(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    config = getattr(app, "config", None)
    dump = getattr(config, "model_dump", None)
    if dump is None:
        return CommandResult(display="config: no config tree", is_error=True)
    import json

    sections: list[str] = []
    models = _render_models_section(app)
    if models:
        sections.append("\n".join(models))
    data = cast(Callable[[], object], dump)()
    sections.append("effective config:\n" + json.dumps(data, indent=2, default=str))
    return await _report(ctx, "Effective config", "\n\n".join(sections))


async def _cmd_permissions(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    mode = getattr(getattr(app, "config", None), "default_permission_mode", None)
    cc = cast(CommandContext, ctx)
    perms = cc.permissions
    if perms is None:
        line = "permissions: no adapter active"
    else:
        line = f"permissions: adapter {type(perms).__name__}"
    if mode is not None:
        line += f" · default mode: {mode}"
    sections: list[str] = [line]

    rules = getattr(perms, "rules", None) if perms is not None else None
    if rules:
        sections.append("active rules:")
        for r in cast("list[object]", rules):
            sections.append(f"  {r}")

    log = getattr(perms, "approval_log", None) if perms is not None else None
    if log is not None:
        entries = getattr(log, "entries", [])
        if entries:
            sections.append("recent decisions:")
            for e in entries[-15:]:
                ts = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
                sections.append(f"  [{ts}] {e.action} {e.tool} {e.pattern} {e.reason}")

    if len(sections) > 1:
        return await _report(ctx, "Permissions", "\n".join(sections))
    return CommandResult(display=line)


async def _cmd_secret(ctx: object, args: str) -> CommandResult:
    """Manage model API keys without ever printing secret values.

    Usage:
      /secret                         status table
      /secret set KEY [VALUE]         set a session-only key (masked prompt in TUI)
      /secret save KEY [VALUE]        persist a key to the encrypted vault
      /secret clear KEY               remove the session-only override
      /secret delete KEY              remove the encrypted-vault key
    """
    parts = _split_secret_args(args)
    if parts is None:
        return CommandResult(display="secret: could not parse arguments", is_error=True)
    if not parts or parts[0] in ("list", "status"):
        return await _report(ctx, "Model API keys", _render_secret_status(ctx))

    action = parts[0]
    if action in ("set", "session"):
        return await _set_secret(ctx, parts[1:], persistent=False)
    if action in ("save", "file", "vault"):
        return await _set_secret(ctx, parts[1:], persistent=True)
    if action in ("clear", "unset"):
        return _delete_secret(ctx, parts[1:], session=True)
    if action in ("delete", "remove", "rm"):
        return _delete_secret(ctx, parts[1:], session=False)
    return CommandResult(
        display="secret: unknown action (use: set/save/clear/delete)",
        is_error=True,
    )


def _split_secret_args(args: str) -> list[str] | None:
    try:
        return shlex.split(args)
    except ValueError:
        return None


async def _set_secret(
    ctx: object, args: list[str], *, persistent: bool
) -> CommandResult:
    if not args:
        return CommandResult(
            display="usage: /secret set KEY [VALUE] or /secret save KEY [VALUE]",
            is_error=True,
        )
    key = args[0].strip()
    if not key:
        return CommandResult(display="secret: key is required", is_error=True)
    value = " ".join(args[1:]).strip()
    if not value:
        from autoverse.frontends.repl.dialogs.host import get_dialog_host

        host = get_dialog_host(ctx)
        prompt = getattr(host, "prompt", None) if host is not None else None
        if callable(prompt):
            value = str(
                await prompt(
                    f"Enter value for {key}",
                    placeholder="API key (hidden)",
                    password=True,
                )
                or ""
            ).strip()
        if not value:
            return CommandResult(
                display="secret: value is required (omit VALUE only in the TUI overlay)",
                is_error=True,
            )

    secrets = getattr(_app(ctx), "secrets", None)
    if secrets is None:
        return CommandResult(display="secret: no secret provider configured", is_error=True)
    method_name = "set" if persistent else "set_session"
    method = getattr(secrets, method_name, None)
    if method is None:
        target = "encrypted vault" if persistent else "session"
        return CommandResult(display=f"secret: {target} writes unavailable", is_error=True)
    try:
        cast(Callable[[str, str], None], method)(key, value)
    except Exception as exc:  # noqa: BLE001 - user-facing command error
        return CommandResult(display=f"secret: could not save {key}: {exc}", is_error=True)
    where = "encrypted file" if persistent else "this session"
    return _feedback(ctx, f"secret {key} saved to {where}", "success")


def _delete_secret(ctx: object, args: list[str], *, session: bool) -> CommandResult:
    if not args:
        return CommandResult(
            display="usage: /secret clear KEY or /secret delete KEY",
            is_error=True,
        )
    key = args[0].strip()
    secrets = getattr(_app(ctx), "secrets", None)
    if secrets is None:
        return CommandResult(display="secret: no secret provider configured", is_error=True)
    method_name = "delete_session" if session else "delete"
    method = getattr(secrets, method_name, None)
    if method is None:
        target = "session" if session else "encrypted vault"
        return CommandResult(display=f"secret: {target} delete unavailable", is_error=True)
    try:
        ok = bool(cast(Callable[[str], bool], method)(key))
    except Exception as exc:  # noqa: BLE001
        return CommandResult(display=f"secret: could not delete {key}: {exc}", is_error=True)
    target = "session" if session else "encrypted file"
    return _feedback(ctx, f"secret {key} removed from {target}" if ok else f"secret {key} not present in {target}")


def _render_secret_status(ctx: object) -> str:
    import os

    app = _app(ctx)
    secrets = getattr(app, "secrets", None)
    usage = _model_secret_usage()
    session_aliases = _secret_aliases_from(secrets, "session_aliases")
    vault_aliases = _secret_aliases_from(secrets, "vault_aliases")
    aliases = set(queries.secret_aliases(app) or ())
    if not vault_aliases and not session_aliases:
        # Older/custom providers may only expose list_aliases; treat those as
        # stored aliases of unknown non-env source.
        vault_aliases = aliases
    names = sorted(set(usage) | session_aliases | vault_aliases | aliases)
    current = str(getattr(app, "model", "") or "")
    lines = [
        "model API keys (values hidden)",
        "priority: env > session > encrypted file",
    ]
    path = _vault_path(app)
    if path is not None:
        lines.append(f"file: {path}")
    if not names:
        lines.append("  no model key aliases known")
        return "\n".join(lines)

    for name in names:
        has_env = bool(os.environ.get(name))
        has_session = name in session_aliases
        has_file = name in vault_aliases
        source = (
            "env" if has_env else
            "session" if has_session else
            "file" if has_file else
            "missing"
        )
        used_by = sorted(usage.get(name, set()))
        marker = "*" if current in used_by else " "
        sources = ", ".join(
            label for label, present in (
                ("env", has_env), ("session", has_session), ("file", has_file)
            )
            if present
        ) or "-"
        models = ", ".join(used_by) if used_by else "(not used by registry)"
        lines.append(f"{marker} {name:<22} effective={source:<7} sources={sources}")
        lines.append(f"    used by: {models}")
    lines.append("commands: /secret set KEY · /secret save KEY · /secret clear KEY · /secret delete KEY")
    return "\n".join(lines)


def _secret_aliases_from(secrets: object, method_name: str) -> set[str]:
    method = getattr(secrets, method_name, None) if secrets is not None else None
    if method is None:
        return set()
    try:
        return {str(x) for x in cast(Callable[[], list[str]], method)()}
    except Exception:  # noqa: BLE001 - status must never crash
        return set()


def _vault_path(app: object) -> Path | None:
    config = getattr(app, "config", None)
    session_dir = getattr(config, "session_dir", "") if config is not None else ""
    if not session_dir:
        return None
    try:
        return Path(str(session_dir)).expanduser().parent / "secrets.enc"
    except Exception:  # pragma: no cover - defensive
        return None


def _model_secret_usage() -> dict[str, set[str]]:
    try:
        from autoverse.infra.llm.registry import MODEL_REGISTRY
    except Exception:  # pragma: no cover - defensive
        return {}
    usage: dict[str, set[str]] = {}
    for model_id, spec in MODEL_REGISTRY.items():
        for transport in getattr(spec, "transports", ()) or ():
            for name in _credential_secret_names(getattr(transport, "auth", None)):
                usage.setdefault(name, set()).add(str(model_id))
    return usage


def _credential_secret_names(cred: object) -> set[str]:
    if cred is None:
        return set()
    names = {
        str(value)
        for attr in ("secret_name", "app_id_name", "app_key_name")
        for value in (getattr(cred, attr, None),)
        if isinstance(value, str) and value
    }
    for child_attr in ("creds", "primary", "fallback"):
        child = getattr(cred, child_attr, None)
        if isinstance(child, tuple):
            for item in child:
                names.update(_credential_secret_names(item))
        elif child is not None:
            names.update(_credential_secret_names(child))
    return names


async def _cmd_trace(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    sid = queries.session_id_of(app)
    if sid is None:
        return CommandResult(display="trace: no trace collector active")
    usage = queries.total_usage(app)
    tok = f" · {usage.total:,} tok" if usage is not None else ""
    return CommandResult(display=f"trace: session {sid}{tok}")


async def _cmd_skills(ctx: object, args: str) -> CommandResult:
    admin = getattr(_app(ctx), "extra", {}).get("extension_admin")
    if admin is not None:
        return _admin_skill_command(admin, args)
    reg = _registry_of(ctx)
    specs = _skill_command_specs(reg, ctx)
    if not specs:
        return CommandResult(display="skills: none registered")

    # Interactive: pick a skill in an overlay and run it (§8 I9). Selecting a
    # skill that is a prompt-template command submits its expanded prompt.
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is not None:
        from autoverse.frontends.repl.dialogs import SelectOption

        options = [
            SelectOption(title=f"/{s.name}", value=s.name, description=s.summary,
                         category="skills")
            for s in specs
        ]
        chosen = await host.select(options, title="Skills",
                                   placeholder="search skills…")
        if chosen is None:
            return CommandResult(display="")
        chosen_spec = reg.get(str(chosen))
        if chosen_spec is not None:
            return await chosen_spec.handler(ctx, "")  # run the selected skill
        return CommandResult(display="")

    return CommandResult(display="skills:\n" + "\n".join(f"  /{s.name}" for s in specs))


def _skill_command_specs(reg: CommandRegistry, ctx: object) -> list[CommandSpec]:
    """User-invocable skill slash commands, excluding extension-management builtins."""
    return sorted(
        (
            entry.spec
            for entry in reg.palette_entries(ctx)
            if entry.spec.group == "extend"
            and entry.spec.name not in _EXTENSION_BUILTIN_COMMANDS
        ),
        key=lambda s: s.name,
    )


async def _cmd_mcp(ctx: object, args: str) -> CommandResult:
    sources = getattr(_app(ctx), "extension_sources", ())
    managers = [src for src in sources if getattr(src, "name", None) == "mcp"]
    if not managers:
        return CommandResult(display="mcp: no MCP servers configured")

    lines: list[str] = []
    for manager in managers:
        statuses = getattr(manager, "server_status", {})
        if isinstance(statuses, dict) and statuses:
            lines.append("mcp servers:")
            for name, status in sorted(statuses.items()):
                lines.append(f"  {name}: {status}")
        contributed = getattr(manager, "contributed_tools", None)
        tools = list(contributed()) if callable(contributed) else []
        resource_tools = getattr(manager, "resource_tools", None)
        if callable(resource_tools):
            tools.extend(resource_tools())
        if tools:
            lines.append("mcp tools:")
            for tool in sorted(tools, key=lambda item: getattr(item, "name", "")):
                desc = str(getattr(tool, "description", "") or "").strip()
                suffix = f" - {desc}" if desc else ""
                lines.append(f"  {getattr(tool, 'name', '?')}{suffix}")
    return CommandResult(display="\n".join(lines) if lines else "mcp: no MCP tools connected")


async def _cmd_plugin(ctx: object, args: str) -> CommandResult:
    admin = getattr(_app(ctx), "extra", {}).get("extension_admin")
    if admin is not None:
        return _admin_plugin_command(admin, args)
    sources = getattr(_app(ctx), "extension_sources", ())
    if not sources:
        return CommandResult(display="plugin: no plugins loaded")
    lines = ["loaded plugins:"]
    lines.extend(f"  {type(s).__name__}" for s in sources)
    # Grows one line per plugin → route through _report (viewer when long enough).
    return await _report(ctx, "Loaded plugins", "\n".join(lines))


def _admin_skill_command(admin: object, args: str) -> CommandResult:
    parts = args.split()
    action = parts[0] if parts else "list"
    target = parts[1] if len(parts) > 1 else ""
    if action in {"list", "ls"}:
        items = admin.list_skills()  # type: ignore[attr-defined]
        if not items:
            return CommandResult(display="skills: none registered")
        return CommandResult(
            display="skills:\n"
            + "\n".join(
                f"  {i.id} [{i.source}] {i.state} - {i.summary}" for i in items
            )
        )
    if not target and action not in {"list", "ls"}:
        return CommandResult(display="skills: expected action and skill name", is_error=True)
    if action == "show":
        detail = admin.get_skill(target)  # type: ignore[attr-defined]
        if detail is None:
            return CommandResult(display=f"skill {target!r} not found", is_error=True)
        return CommandResult(display=f"{detail.item.id}\n{detail.path}\n\n{detail.body}")
    if action == "doctor":
        prompt = " ".join(parts[2:]) or None
        return CommandResult(display=admin.diagnose_skill(target, prompt))  # type: ignore[attr-defined]
    if action == "enable":
        admin.enable_skill(target)  # type: ignore[attr-defined]
        return CommandResult(display=f"skill {target}: enabled")
    if action == "disable":
        admin.disable_skill(target)  # type: ignore[attr-defined]
        return CommandResult(display=f"skill {target}: disabled")
    if action == "trust":
        admin.trust_skill(target)  # type: ignore[attr-defined]
        return CommandResult(display=f"skill {target}: trusted")
    return CommandResult(display=f"skills: unknown action {action!r}", is_error=True)


def _admin_plugin_command(admin: object, args: str) -> CommandResult:
    parts = args.split()
    action = parts[0] if parts else "list"
    target = parts[1] if len(parts) > 1 else ""
    if action in {"list", "ls"}:
        items = admin.list_plugins()  # type: ignore[attr-defined]
        if not items:
            return CommandResult(display="plugin: no plugins loaded")
        return CommandResult(
            display="plugins:\n"
            + "\n".join(f"  {i.id} {i.state} - {i.summary}" for i in items)
        )
    if not target:
        return CommandResult(display="plugin: expected action and plugin name", is_error=True)
    if action == "show":
        detail = admin.get_plugin(target)  # type: ignore[attr-defined]
        if detail is None:
            return CommandResult(display=f"plugin {target!r} not found", is_error=True)
        caps = ", ".join(detail.capabilities) or "none"
        return CommandResult(display=f"{detail.item.id}\nstate: {detail.item.state}\ncapabilities: {caps}")
    if action == "doctor":
        return CommandResult(display=admin.diagnose_plugin(target))  # type: ignore[attr-defined]
    if action == "enable":
        admin.enable_plugin(target)  # type: ignore[attr-defined]
        return CommandResult(display=f"plugin {target}: enabled")
    if action == "disable":
        admin.disable_plugin(target)  # type: ignore[attr-defined]
        return CommandResult(display=f"plugin {target}: disabled")
    if action == "trust":
        admin.trust_plugin(target)  # type: ignore[attr-defined]
        return CommandResult(display=f"plugin {target}: trusted")
    return CommandResult(display=f"plugin: unknown action {action!r}", is_error=True)


async def _cmd_queue(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    queue = app.extra.get("message_queue") if isinstance(app.extra, dict) else None
    size = getattr(queue, "qsize", None)
    if size is None:
        return CommandResult(display="queue: no inbound queue active")
    n = cast(Callable[[], int], size)()
    return CommandResult(display=f"queue: {n} message(s) pending")


# ── session control ──────────────────────────────────────────────────────────

async def _cmd_goal(ctx: object, args: str) -> CommandResult:
    from autoverse.app.workflow.goal import CLEAR_ALIASES

    app = _app(ctx)
    goal = getattr(app, "goal", None)
    sid = queries.session_id_of(app) or ""
    if goal is None:
        return CommandResult(display="goal: no goal store configured")
    condition = args.strip()
    if condition and condition.lower() in CLEAR_ALIASES:
        clearer = getattr(goal, "clear", None)
        if clearer is None:
            return CommandResult(display="goal: store does not support clear", is_error=True)
        old = cast(Callable[[str], object], clearer)(sid)
        persist = getattr(goal, "persist", None)
        if callable(persist):
            await persist(sid)
        if old is None:
            return _feedback(ctx, "No active goal to clear.", "info")
        return _feedback(ctx, "Goal cleared.", "success")
    if condition:
        setter = getattr(goal, "set", None)
        if setter is None:
            return CommandResult(display="goal: store does not support set", is_error=True)
        try:
            cast(Callable[[str, str], object], setter)(sid, condition)
        except ValueError as exc:
            return CommandResult(display=str(exc), is_error=True)
        persist = getattr(goal, "persist", None)
        if callable(persist):
            await persist(sid)
        feedback = _feedback(ctx, f"Goal set: {condition}", "success")
        # Setting a goal is an action, not just configuration: immediately submit
        # the condition as the first user turn so the executor knows what to do.
        return replace(feedback, prompt=condition)
    formatter = getattr(goal, "format_status", None)
    if callable(formatter):
        return CommandResult(display=str(formatter(sid)))
    getter = getattr(goal, "get", None)
    if getter is None:
        return CommandResult(display="goal: store does not support query", is_error=True)
    entry = cast(Callable[[str], object], getter)(sid)
    if entry is None:
        return CommandResult(display="goal: none set")
    entry_condition = getattr(entry, "condition", None)
    if entry_condition is None:
        entry_condition = entry
    return CommandResult(display=f"goal: {entry_condition}")


async def _dispatch_workflow(ctx: object, name: str, args: str) -> CommandResult:
    engine = _engine(ctx)
    run = getattr(engine, "run_workflow", None)
    if run is None:
        return CommandResult(display=f"{name}: not supported by this engine", is_error=True)
    out = await cast(Callable[[str, str], Awaitable[str]], run)(name, args.strip())
    return CommandResult(display=out, display_markdown=True)


def _make_workflow_cmd(
    name: str,
) -> Callable[[object, str], Awaitable[CommandResult]]:
    """Build a handler that dispatches a built-in pipeline via ``run_workflow``."""

    async def handler(ctx: object, args: str) -> CommandResult:
        return await _dispatch_workflow(ctx, name, args)

    return handler


async def _cmd_workflows(ctx: object, args: str) -> CommandResult:
    app = _app(ctx)
    workflows = app.extra.get("workflows") if isinstance(app.extra, dict) else None
    lister = getattr(workflows, "all", None)
    if lister is None:
        return CommandResult(display="workflows: none registered")
    defs = cast(Callable[[], "list[object]"], lister)()
    if not defs:
        return CommandResult(display="workflows: none registered")
    ordered = sorted(defs, key=lambda x: str(getattr(x, "name", "")))
    reg = _registry_of(ctx)

    def label(definition: object) -> str:
        name = str(getattr(definition, "name", "?"))
        return f"/{name}" if reg.get(name) is not None else name

    # Interactive: pick a workflow in an overlay and run it (§8 I9) — symmetric
    # with /skills. Registered workflows without a direct slash command still run
    # through EngineAdapter.run_workflow; the UI must not expose fake /fix-like
    # commands.
    from autoverse.frontends.repl.dialogs.host import get_dialog_host

    host = get_dialog_host(ctx)
    if host is not None:
        from autoverse.frontends.repl.dialogs import SelectOption

        options = [
            SelectOption(
                title=label(d),
                value=str(getattr(d, "name", "")),
                description=str(getattr(d, "summary", "")),
                category="workflows",
            )
            for d in ordered
        ]
        chosen = await host.select(options, title="Workflows",
                                   placeholder="search workflows…")
        if chosen is None:
            return CommandResult(display="")
        return await _dispatch_workflow(ctx, str(chosen), "")

    lines = ["registered workflows:"]
    for d in ordered:
        entry = label(d)
        suffix = "" if entry.startswith("/") else " (run_workflow target)"
        lines.append(f"  {entry:<12} {getattr(d, 'summary', '')}{suffix}")
    return CommandResult(display="\n".join(lines))


async def _cmd_stop(ctx: object, args: str) -> CommandResult:
    engine = _engine(ctx)
    abort = getattr(engine, "abort", None)
    if abort is None or not getattr(ctx, "is_running", False):
        return CommandResult(display="stop: nothing is running")
    cast(Callable[[str], None], abort)("interrupt")
    return _feedback(ctx, "stop: interrupt requested", "warning")


async def _cmd_details(ctx: object, args: str) -> CommandResult:
    """Toggle fold verbosity: when on, thinking + long tool blocks render
    expanded by default instead of folded (§3.9.3)."""
    cc = cast(CommandContext, ctx)
    sink = cc.extra.get("sink")
    current = (
        getattr(sink, "show_details", False)
        if sink is not None
        else cc.extra.get("show_details", False)
    )
    new_state = not bool(current)
    if sink is not None:
        setattr(sink, "show_details", new_state)
    cc.extra["show_details"] = new_state
    return _feedback(
        ctx,
        f"details: thinking & long output now {'expanded' if new_state else 'folded'} by default",
    )


async def _cmd_expand(ctx: object, args: str) -> CommandResult:
    return CommandResult(
        display="expand: full output lives in the trace (§6); see /trace"
    )


async def _cmd_subagents(ctx: object, args: str) -> CommandResult:
    """Open the sub-agent picker → drill into a sub-agent's captured context
    (same overlay as the ``ctrl+r`` keybinding; docs/guides/using-cli-and-tui.md §3.9.3.1).

    In the TUI this hands off to the App's overlay worker; headless callers get
    a short text listing of dispatched sub-agents instead."""
    cc = cast(CommandContext, ctx)
    ui = cc.extra.get("ui")
    action = getattr(ui, "action_view_subagents", None)
    if callable(action):
        action()  # @work overlay; returns immediately
        return CommandResult(display="")
    # Headless fallback: list from the sink if present.
    sink = cc.extra.get("sink")
    records = list(sink.subagents()) if sink is not None and hasattr(sink, "subagents") else []
    if not records:
        return CommandResult(display="subagents: none dispatched this session")
    lines = ["subagents:"]
    for i, rec in enumerate(records):
        lines.append(
            f"  {i + 1}. [{rec.subagent_type}] {rec.status} — {rec.description or '(no task)'}"
        )
    return CommandResult(display="\n".join(lines))


# ── lifecycle ────────────────────────────────────────────────────────────────

async def _cmd_quit(ctx: object, args: str) -> CommandResult:
    return CommandResult(display="bye", should_quit=True)


def build_default_registry() -> CommandRegistry:
    """Create a registry populated with the built-in commands (R3)."""
    registry = CommandRegistry()
    registry.register(
        CommandSpec(name="help", summary="show all commands", handler=_cmd_help,
                    group="view", keybind="?", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="clear", summary="clear history, keep session", handler=_cmd_clear,
                    group="session", enabled=lambda c: not getattr(c, "is_running", False))
    )
    registry.register(
        CommandSpec(name="compact", summary="manually compact context", handler=_cmd_compact,
                    group="session", busy_label="Compacting…",
                    enabled=lambda c: not getattr(c, "is_running", False))
    )
    registry.register(
        CommandSpec(name="model", summary="view or switch the model", handler=_cmd_model,
                    group="model", keybind="ctrl+m")
    )
    registry.register(
        CommandSpec(name="tiers", summary="view or set sub-agent models",
                    handler=_cmd_tiers,
                    aliases=("subagent-model", "subagent-models"),
                    group="model")
    )
    registry.register(
        CommandSpec(name="rewind", summary="revert files + history to a message",
                    handler=_cmd_rewind, group="session",
                    enabled=lambda c: not getattr(c, "is_running", False))
    )

    # ── view / observability (read-only, run while busy) ──
    registry.register(
        CommandSpec(name="context", summary="context-window token occupancy",
                    handler=_cmd_context, group="view", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="cost", summary="cost grouped by model",
                    handler=_cmd_cost, group="view", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="usage", summary="per-model token usage this session",
                    handler=_cmd_usage, group="view", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="diff", summary="git diff of this session's changes",
                    handler=_cmd_diff, group="view", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="export", summary="export trace to a file (.html/.md/.json)",
                    handler=_cmd_export, group="view", enabled=_idle)
    )
    registry.register(
        CommandSpec(name="memory", summary="show loaded memory sources",
                    handler=_cmd_memory, group="view")
    )
    registry.register(
        CommandSpec(name="session-tree", summary="sub-agent session hierarchy",
                    handler=_cmd_session_tree, group="view", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="subagents", summary="open a sub-agent's captured context",
                    handler=_cmd_subagents, group="view", keybind="ctrl+r",
                    run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="details", summary="toggle showing completed tools",
                    handler=_cmd_details, group="view", keybind="ctrl+o",
                    run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="expand", summary="how to view truncated output (trace)",
                    handler=_cmd_expand, group="view", run_while_busy=True)
    )

    # ── session ──
    registry.register(
        CommandSpec(name="sessions", summary="list/resume stored sessions",
                    handler=_cmd_sessions, group="session", enabled=_idle)
    )
    registry.register(
        CommandSpec(name="goal", summary="show/set the active completion goal",
                    handler=_cmd_goal, group="session")
    )
    registry.register(
        CommandSpec(name="queue", summary="show queued inbound messages",
                    handler=_cmd_queue, group="session", run_while_busy=True)
    )

    # ── workflows (multi-agent pipelines, docs/reference/workflows.md §7.4; idle-only) ──
    registry.register(
        CommandSpec(name="workflows", summary="list registered workflows",
                    handler=_cmd_workflows, group="session", run_while_busy=True)
    )
    for _wf, _summary, _activity in (
        ("commit", "inspect → message → commit pipeline", "Create commit"),
        ("review", "independent code-review pipeline", "Review changes"),
        ("pr", "summarize → draft → open a pull request", "Open pull request"),
        ("init", "scan repo → write AGENTS.md", "Initialize project"),
        (
            "coordinate",
            "architect → builder → reviewer scoring loop",
            "Coordinate implementation",
        ),
    ):
        registry.register(
            CommandSpec(name=_wf, summary=_summary,
                        handler=_make_workflow_cmd(_wf), group="session",
                        busy_label=f"Running /{_wf}…",
                        activity_label=_activity,
                        enabled=_idle)
        )
    registry.register(
        CommandSpec(name="stop", summary="interrupt the running turn",
                    handler=_cmd_stop, group="session", run_while_busy=True,
                    enabled=lambda c: getattr(c, "is_running", False))
    )

    # ── system ──
    registry.register(
        CommandSpec(name="doctor", summary="environment self-check",
                    handler=_cmd_doctor, group="system", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="config", summary="show effective config",
                    handler=_cmd_config, group="system")
    )
    registry.register(
        CommandSpec(name="permissions", summary="show active permission rules",
                    handler=_cmd_permissions, group="system")
    )
    registry.register(
        CommandSpec(name="secret", summary="manage model API keys (values hidden)",
                    handler=_cmd_secret, aliases=("secrets",), group="system",
                    enabled=_idle)
    )
    registry.register(
        CommandSpec(name="trace", summary="current session trace summary",
                    handler=_cmd_trace, group="system", run_while_busy=True)
    )

    # ── extend ──
    registry.register(
        CommandSpec(name="skills", summary="list loaded skills / manage skill state",
                    handler=_cmd_skills, group="extend", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="mcp", summary="show MCP server status/tools",
                    handler=_cmd_mcp, group="extend", run_while_busy=True)
    )
    registry.register(
        CommandSpec(name="plugin", summary="list loaded plugins",
                    handler=_cmd_plugin, group="extend")
    )

    # ── lifecycle (/exit and /quit are siblings — both quit) ──
    registry.register(
        CommandSpec(name="quit", summary="exit after the current turn",
                    handler=_cmd_quit, group="session")
    )
    registry.register(
        CommandSpec(name="exit", summary="exit after the current turn",
                    handler=_cmd_quit, group="session")
    )
    return registry


def make_template_handler(
    spec: CommandSpec, cwd: Path
) -> Callable[[object, str], Awaitable[CommandResult]]:
    """Build a handler that expands *spec.template* into a submitted prompt turn.

    The returned :class:`CommandResult` carries ``prompt`` (not ``display``) plus
    the command's ``agent`` / ``model`` / ``subtask`` routing; the loop submits it
    as one user turn (docs/reference/commands.md §4.2 / §4.4).
    """
    from autoverse.frontends.repl.commands_template import expand_template

    template = spec.template or ""

    async def handler(ctx: object, args: str) -> CommandResult:
        prompt = await expand_template(template, args, cwd=cwd)
        if not prompt:
            return CommandResult(display=f"/{spec.name}: empty prompt", is_error=True)
        return CommandResult(
            prompt=prompt,
            agent=spec.agent,
            model=spec.model,
            subtask=spec.subtask,
        )

    return handler


def _make_skill_handler(name: str, runner: Callable[[str], Awaitable[str]]) -> Callable[[object, str], Awaitable[CommandResult]]:
    async def handler(ctx: object, args: str) -> CommandResult:
        out = await runner(args)
        return CommandResult(display=out)

    return handler


def register_skill_commands(
    registry: CommandRegistry,
    skills: dict[str, Callable[[str], Awaitable[str]]],
    *,
    summaries: dict[str, str] | None = None,
) -> None:
    """Register each user-invocable skill as an ordinary command (R4).

    ``skills`` maps a skill name to an async runner ``(args) -> str``. The
    resulting command dispatches through the same table as any built-in — there
    is no fallback skill probe in the dispatcher.
    """
    summaries = summaries or {}
    for name, runner in skills.items():
        registry.register(
            CommandSpec(
                name=name,
                summary=summaries.get(name, f"skill: {name}"),
                handler=_make_skill_handler(name, runner),
                group="extend",
            )
        )


def _make_skill_template_handler(
    skill: object,
) -> Callable[[object, str], Awaitable[CommandResult]]:
    """Handler that submits a skill body as a prompt (G6).

    A skill is a reusable instruction bundle. The rendered text is submitted as
    one turn via ``CommandResult.prompt`` — same path as a file command.
    """
    render = cast(Callable[[str], str], getattr(skill, "render"))

    async def handler(ctx: object, args: str) -> CommandResult:
        prompt = render(args).strip()
        if not prompt:
            name = getattr(skill, "name", "skill")
            return CommandResult(display=f"/{name}: empty prompt", is_error=True)
        return CommandResult(prompt=prompt)

    return handler


def register_skill_template_commands(registry: CommandRegistry, skills: object) -> list[str]:
    """Register each loaded skill as a prompt-template command (G6).

    *skills* is a ``SkillRegistry``-like object exposing ``unique_enabled_skills``
    (or ``all_skills``). Names already taken (built-ins / file commands) win and
    the skill is skipped — never raises, so a clashing skill can't crash startup.
    Returns the registered skill command names.
    """
    lister = getattr(skills, "unique_enabled_skills", None) or getattr(
        skills, "all_skills", None
    )
    if lister is None:
        return []
    registered: list[str] = []
    for skill in cast(Callable[[], list[object]], lister)():
        name = str(getattr(skill, "name", ""))
        if not name or registry.get(name) is not None:
            continue  # built-in / file command / earlier skill wins; skip
        registry.register(
            CommandSpec(
                name=name,
                summary=str(getattr(skill, "description", "") or f"skill: {name}"),
                handler=_make_skill_template_handler(skill),
                group="extend",
            )
        )
        registered.append(name)
    return registered


__all__ = [
    "build_default_registry",
    "register_skill_commands",
    "register_skill_template_commands",
    "make_template_handler",
]
