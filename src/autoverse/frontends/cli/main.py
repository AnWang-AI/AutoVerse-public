"""CLI entry — one-shot execution via EngineBuilder (docs/guides/using-cli-and-tui.md §3.7).

``EngineBuilder(options).build()`` does the explicit assembly and returns a
single :class:`~autoverse.ports.EngineHandle` — there is no 10-element tuple
(R9/D1). The one-shot path renders the ``StreamEvent`` stream with the shared
``RENDERERS`` table and returns an exit code derived from the final turn.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Protocol

from autoverse.app.builder import EngineBuilder
from autoverse.frontends.cli import capabilities
from autoverse.app.options import EngineOptions
from autoverse.frontends.repl.builtin_cmds import build_default_registry
from autoverse.frontends.repl.renderer import RenderContext, render_event
from autoverse.frontends.repl.theme import Theme
from autoverse.infra.observ.logging import configure_from_config
from autoverse.ports import EngineHandle, PermissionPort
from autoverse.types import EventType
from autoverse.types.events import TurnEndPayload

_LOG = logging.getLogger(__name__)


class _Settable(Protocol):
    def set(self) -> None: ...


def _noninteractive_permissions() -> PermissionPort:
    """Default permission adapter when no frontend can ask the user."""
    from autoverse.infra.permission.fixed import NonInteractivePermissionAdapter

    return NonInteractivePermissionAdapter()


def _resolve_model(model_arg: str | None, cwd: Path) -> str:
    """Effective startup model id.

    An explicit ``--model`` always wins (including ``--model echo``). When the
    flag is omitted we fall back to the configured default
    (``config.model.model_id`` from the layered loader), then to ``echo`` as the
    offline last resort. This mirrors EngineBuilder's own config fallback so the
    persisted default model is honoured on a bare ``autoverse`` launch — and the
    sidebar/status line shows the same model the engine will actually use.
    """
    if model_arg is not None:
        return model_arg
    try:
        import os

        from autoverse.infra.config import load_config

        configured = load_config(cwd, os.environ, {}).config.model.model_id
    except Exception:  # noqa: BLE001 — a corrupt/missing config must not block startup
        configured = ""
    return configured or "echo"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoverse", description="AutoVerse CLI")
    parser.add_argument("prompt", nargs="?", default=None, help="one-shot prompt to run")
    parser.add_argument(
        "--model",
        default=None,
        help="model id (default: the configured model, else 'echo')",
    )
    parser.add_argument("--cwd", default=None, help="working directory")
    parser.add_argument("--resume", default=None, help="resume an existing session id")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="one-shot: print output and exit")
    parser.add_argument("--no-tui", action="store_true", help="force plain output (no TUI)")
    parser.add_argument(
        "--ui",
        choices=("auto", "tui", "inline", "line", "plain"),
        default="auto",
        help=(
            "interactive frontend: auto (probe → fullscreen→inline→line "
            "fallback ladder), tui (full-screen, error if unavailable), inline "
            "(in-place Textual, no alt-screen), line (append-only REPL), plain "
            "(one-shot only)"
        ),
    )
    parser.add_argument(
        "--debug-ui",
        action="store_true",
        help="print the probed terminal capabilities + selected tier to stderr",
    )
    parser.add_argument("--theme", default=None, help="theme name or JSON path")
    return parser


async def _run_one_shot(engine: EngineHandle, prompt: str, ctx: RenderContext) -> int:
    stop_cause = "END_TURN"
    async for event in engine.run(prompt):
        sys.stdout.write(render_event(event, ctx) + "\n")
        if event.type is EventType.TURN_END and isinstance(event.payload, TurnEndPayload):
            stop_cause = event.payload.stop_cause
    return 0 if stop_cause in ("END_TURN", "MAX_TURNS", "PLAN_PROPOSED") else 1


def _supports_tui(args: argparse.Namespace) -> bool:
    """True when the full-screen Textual TUI should launch.

    Requires an interactive TTY, no ``--no-tui`` / ``--print``, and the optional
    ``textual`` dependency installed. When this returns False the line-based REPL
    (:func:`_supports_line_repl`) is the interactive fallback (docs/guides/using-cli-and-tui.md §7).
    """
    if getattr(args, "no_tui", False) or getattr(args, "print_only", False):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:  # pragma: no cover - depends on optional dep being installed
        import textual  # noqa: F401
    except Exception:  # pragma: no cover - minimal install
        return False
    return True


def _supports_line_repl(args: argparse.Namespace) -> bool:
    """True when the append-only line REPL can run (docs/guides/using-cli-and-tui.md §7.1).

    Needs an interactive TTY on both ends and ``rich`` (already a de-facto
    dependency of the renderer); deliberately does NOT require ``textual``.
    """
    if getattr(args, "print_only", False):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:  # pragma: no cover - depends on rich being installed
        import rich  # noqa: F401
    except Exception:  # pragma: no cover - minimal install
        return False
    return True


async def _run_tui(
    args: argparse.Namespace,
    theme: Theme,
    *,
    presentation: str = "fullscreen",
    mounted: "_Settable | None" = None,
) -> int:
    """Launch the Textual TUI (docs/guides/using-cli-and-tui.md §7).

    ``presentation`` selects the form: ``"fullscreen"`` (alt-screen) or
    ``"inline"`` (the *same* :class:`AutoVerseApp` via Textual's inline driver —
    no alt-screen, scrollback preserved). ``mounted`` is an optional
    ``threading.Event``-like the CLI fallback net uses to distinguish a startup
    failure from a session crash (docs/guides/using-cli-and-tui.md §8.2).
    """
    from autoverse.app.session_query import StoreSessionQuery
    from autoverse.frontends.repl.app import AutoVerseApp
    from autoverse.frontends.repl.permission_adapter import TuiPermissionAdapter
    from autoverse.infra.persist import JSONLStore, SQLiteSessionCatalog
    from autoverse.ports import EventSink

    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    registry = build_default_registry()
    # Register user-defined Markdown command files (docs/reference/commands.md §4.3).
    try:
        from autoverse.frontends.repl.commands_loader import register_file_commands

        register_file_commands(registry, cwd)
    except Exception:  # pragma: no cover - a bad command file must not block startup
        pass

    # Resolve the model's context window for the sidebar (CLI may import infra;
    # the frontend itself stays clean — it just receives the number).
    try:
        from autoverse.infra.llm.registry import get_context_window, resolve_alias

        context_window = get_context_window(resolve_alias(args.model))
    except Exception:  # pragma: no cover - defensive
        context_window = 0

    version = getattr(__import__("autoverse"), "__version__", "0.1.0")

    # ── Session rail wiring (docs/architecture/frontend-adapters.md §9.3) ────────────────────────
    # A single shared JSONLStore over the configured session_dir backs BOTH the
    # read-only listing query (the rail) AND every engine the switch factory
    # builds — so the rail's mtime cache and the engine's writes line up, and a
    # switch reuses the same on-disk session tree. Degrades to no rail if the
    # session root can't be resolved (one-shot / odd config) — the app hides it.
    session_query = None
    session_catalog = None
    switch_engine = None
    shared_rule_store = None
    try:
        from autoverse.infra.config import load_config

        session_dir = load_config(cwd, os.environ, {}).config.session_dir
        session_root = Path(session_dir).expanduser()
        shared_store = JSONLStore(session_root)
        session_catalog = SQLiteSessionCatalog(
            session_root.parent / "session-catalog.sqlite3"
        )
        session_query = StoreSessionQuery(
            shared_store,
            base_dir=session_root,
            catalog=session_catalog,
        )
        from autoverse.infra.permission.rule_store import PermissionRuleStore

        shared_rule_store = PermissionRuleStore(session_root.parent)
    except Exception:  # pragma: no cover - a config/store failure just hides the rail
        shared_store = None
        session_query = None

    holder: dict[str, AutoVerseApp] = {}

    async def _factory(sink: "EventSink") -> EngineHandle:
        # The factory is called after the Textual app mounts, so the holder is
        # populated and the permission adapter can show modal approval screens.
        app_for_modal = holder["app"]
        adapter = TuiPermissionAdapter(app_for_modal, rule_store=shared_rule_store)
        opts = EngineOptions(
            model=args.model,
            cwd=cwd,
            session_id=args.resume,
            session_mode="interactive",
            event_sink=sink,
            permission_engine=adapter,
            store=shared_store,
            session_catalog=session_catalog,
        )
        engine = await EngineBuilder(opts).build()
        # Tag the adapter with the resolved session id so a backgrounded turn's
        # approval relays to switch-in instead of the foreground (§5.6.3).
        adapter.session_id = str(getattr(engine, "session_id", "") or "")
        return engine

    if shared_store is not None:
        async def switch_engine(session_id: str, sink: "EventSink") -> EngineHandle:
            # Build a fresh engine bound to *session_id* (empty ⇒ a new session),
            # sharing the rail's store + the live modal permission adapter (§5.1).
            adapter = TuiPermissionAdapter(holder["app"], rule_store=shared_rule_store)
            opts = EngineOptions(
                model=args.model,
                cwd=cwd,
                session_id=session_id or None,
                session_mode="interactive",
                event_sink=sink,
                permission_engine=adapter,
                store=shared_store,
                session_catalog=session_catalog,
            )
            engine = await EngineBuilder(opts).build()
            adapter.session_id = str(getattr(engine, "session_id", "") or "")
            return engine

    app = AutoVerseApp(
        _factory, app_ctx=None, registry=registry, theme=theme,
        cwd=str(cwd), model=args.model, context_window=context_window, version=version,
        presentation=presentation, mounted=mounted,
        session_query=session_query, switch_engine=switch_engine,
    )
    holder["app"] = app
    inline = presentation == "inline"
    # inline_no_clear leaves the final frame in the native scrollback on exit
    # (docs/guides/using-cli-and-tui.md §7.1) — the point of inline over fullscreen.
    result = await app.run_async(inline=inline, inline_no_clear=inline)
    return int(result or 0)


async def _run_line_repl(args: argparse.Namespace, theme: Theme) -> int:
    """Launch the append-only line-based REPL fallback (docs/guides/using-cli-and-tui.md §7.3).

    Mirrors :func:`_run_tui` but feeds the engine a console-backed sink (reusing
    ``TuiEventSink``) and a row-based approval adapter; never imports ``textual``.
    """
    from autoverse.frontends.repl.line_repl import LineRepl
    from autoverse.ports import EventSink, PermissionPort

    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    registry = build_default_registry()
    try:
        from autoverse.frontends.repl.commands_loader import register_file_commands

        register_file_commands(registry, cwd)
    except Exception:  # pragma: no cover - a bad command file must not block startup
        pass

    version = getattr(__import__("autoverse"), "__version__", "0.1.0")

    async def _factory(
        sink: "EventSink", permissions: "PermissionPort"
    ) -> EngineHandle:
        opts = EngineOptions(
            model=args.model,
            cwd=cwd,
            session_id=args.resume,
            session_mode="interactive",
            event_sink=sink,
            permission_engine=permissions,
        )
        return await EngineBuilder(opts).build()

    repl = LineRepl(
        _factory, registry, theme=theme,
        cwd=str(cwd), model=args.model, version=version,
    )
    return await repl.run()


def _resolve_ui_mode(args: argparse.Namespace) -> str:
    """Resolve the interactive frontend preference (docs/guides/using-cli-and-tui.md §10.1).

    Returns one of ``"auto" | "tui" | "inline" | "line" | "plain"``. ``--ui`` is
    authoritative; ``--no-tui`` is the legacy alias for ``line`` (interactive).
    ``auto`` defers tier selection to :func:`capabilities.recommend_tier` + the
    runtime fallback ladder.
    """
    ui = getattr(args, "ui", "auto")
    if ui != "auto":
        return ui  # explicit tui|inline|line|plain
    if getattr(args, "no_tui", False):
        return "line"
    return "auto"


async def _run_interactive_with_fallback(
    args: argparse.Namespace, theme: Theme, start: capabilities.UITier
) -> int:
    """Attempt *start* then descend the ladder on any *startup* failure (§8).

    Walks ``fullscreen → inline → line`` from *start* downward. A tier that
    raises *before* it mounts (driver/terminal/import failure, e.g. ``textual``
    missing) drops to the next tier with a visible stderr note (P5). A tier that
    raises *after* mounting (a mid-session crash) re-raises — we never silently
    re-tier a live conversation (docs/guides/using-cli-and-tui.md §3.2 / §8.2). ``line`` is the
    ladder floor and has no startup-failure surface to speak of.
    """
    last_exc: Exception | None = None
    for tier in capabilities.descend_from(start):
        mounted = threading.Event()
        try:
            if tier in (capabilities.UITier.FULLSCREEN, capabilities.UITier.INLINE):
                presentation = (
                    "fullscreen" if tier is capabilities.UITier.FULLSCREEN else "inline"
                )
                return await _run_tui(
                    args, theme, presentation=presentation, mounted=mounted
                )
            return await _run_line_repl(args, theme)  # UITier.LINE — the floor
        except Exception as exc:  # noqa: BLE001 - startup net is deliberately broad
            if mounted.is_set():  # session crash: don't mask it behind a re-tier
                raise
            last_exc = exc
            sys.stderr.write(
                f"[ui] {tier.value} 启动失败（{exc}），降级到下一档…\n"
            )
            continue
    sys.stderr.write(
        "interactive mode unavailable: needs 'rich' and a TTY "
        "(pip install autoverse); or use --print for one-shot\n"
    )
    if last_exc is not None:  # pragma: no cover - surfaced for debugging
        sys.stderr.write(f"[ui] last error: {last_exc}\n")
    return 2


async def _dispatch(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    try:
        from autoverse.infra.config import load_config

        cfg = load_config(cwd, os.environ, {}).config
        log_path = configure_from_config(
            cfg, Path(cfg.session_dir).expanduser().parent, frontend="cli"
        )
        if log_path is not None:
            _LOG.info("CLI logging initialized at %s", log_path)
    except Exception:  # noqa: BLE001 - logging setup must not block startup
        pass
    # Resolve the effective model once, up front, so every downstream path
    # (TUI/line factories, the sidebar context-window lookup, one-shot opts)
    # sees the configured default instead of the bare argparse fallback.
    args.model = _resolve_model(args.model, cwd)
    theme = Theme.load(args.theme)
    ansi = (not args.no_tui) and sys.stdout.isatty()
    ctx = RenderContext(theme=theme, ansi=ansi)

    prompt = args.prompt
    if prompt is None:
        # No prompt → interactive mode. Tier ladder (docs/guides/using-cli-and-tui.md §10.2):
        # fullscreen → inline → line, chosen by probe + runtime fallback. In a
        # non-TTY (pipes, CI, tests) there is nothing to drive interactively, so
        # return the historical exit code 2.
        if not sys.stdin.isatty():
            sys.stderr.write("no prompt given (interactive mode needs a TTY)\n")
            return 2
        ui = _resolve_ui_mode(args)
        if ui == "tui":
            # Explicit fullscreen request: strict — error if unavailable, never
            # silently downgrade (docs/guides/using-cli-and-tui.md §3.2 / §8.3).
            if _supports_tui(args):
                return await _run_tui(args, theme, presentation="fullscreen")
            sys.stderr.write(
                "TUI unavailable (needs 'textual' and a TTY); try --ui inline / --ui line\n"
            )
            return 2
        if ui == "line":
            if _supports_line_repl(args):
                return await _run_line_repl(args, theme)
            sys.stderr.write(
                "interactive mode unavailable: needs 'rich' and a TTY; "
                "or use --print for one-shot\n"
            )
            return 2
        if ui == "plain":
            sys.stderr.write(
                "--ui plain is one-shot only; provide a prompt (or use --print)\n"
            )
            return 2
        if ui == "inline":
            return await _run_interactive_with_fallback(
                args, theme, capabilities.UITier.INLINE
            )
        # auto: probe the terminal, pick a starting tier, then run with the net.
        caps = capabilities.probe()
        start = capabilities.recommend_tier(caps)
        if getattr(args, "debug_ui", False) or os.environ.get("AUTOVERSE_DEBUG_UI"):
            sys.stderr.write(f"[ui] {capabilities.describe(caps, start)}\n")
        return await _run_interactive_with_fallback(args, theme, start)

    opts = EngineOptions(
        model=args.model,
        cwd=cwd,
        session_id=args.resume,
        session_mode="one_shot",
        permission_engine=_noninteractive_permissions(),
    )
    engine = await EngineBuilder(opts).build()
    return await _run_one_shot(engine, prompt, ctx)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_dispatch(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "build_parser"]
