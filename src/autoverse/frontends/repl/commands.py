"""Command system — the @command decorator is the single source of truth (R3, D2).

One ``CommandSpec`` declares a command's name, summary, handler, aliases,
keybinding, group, visibility and busy policy *in one place*. Help text, the
completion list, the command palette and keybinding map are all *derived* from
the single ``CommandRegistry`` — there is no second hand-maintained table (P1).

Skills are ordinary commands: a skill registers a normal ``CommandSpec`` and is
dispatched through the same table lookup — ``/skillname`` is **not** a fallback
special-case (R4).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a command handler — plain data, rendered by the loop.

    A *prompt-template* command (docs/reference/commands.md §4.2) sets ``prompt`` instead
    of ``display``: the loop then submits ``prompt`` as one user turn rather than
    printing it. ``agent`` / ``model`` / ``subtask`` carry optional per-command
    routing for that turn (§4.4).

    ``should_clear`` signals the frontend to wipe the on-screen conversation log
    (the engine history is cleared separately by the handler); the TUI owns its
    own widgets, so clearing the visible scrollback can only happen here.
    """

    display: str = ""
    display_markdown: bool = False
    is_error: bool = False
    should_quit: bool = False
    should_clear: bool = False
    prompt: str | None = None
    agent: str | None = None
    model: str | None = None
    subtask: bool = False


CommandHandler = Callable[..., Awaitable[CommandResult]]
ArgCompleter = Callable[[str], list[str]]
EnabledPredicate = Callable[..., bool]


def _normalize_keybind(key: str) -> str:
    return key.strip().lower().replace(" ", "")


@dataclass(frozen=True)
class CommandSpec:
    """Single declaration site for a command (name/summary/handler/keybind/...)."""

    name: str
    summary: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    arg_completer: ArgCompleter | None = None
    run_while_busy: bool = False
    # A short present-tense label (e.g. "Compacting…") shown as a transient
    # status-bar spinner while a *long-running* handler executes in the TUI, so
    # capitalised to match the engine phase labels (Thinking… / Responding… /
    # Running tool…) the StatusBar renders for the main agent. See widgets.py.
    # pressing the command yields an immediate visual cue instead of an apparent
    # freeze. The label is purely a frontend hint: it never touches the
    # scrollback (no toast / no log line), so the only persistent record stays
    # whatever the handler itself emits (e.g. compaction's boundary divider).
    busy_label: str | None = None
    # Optional persistent heading written immediately before a long-running
    # command starts (e.g. ``/init · Initialize project``). This is intentionally
    # simpler than a workflow event/card hierarchy: the real sub-agent cards
    # remain the detailed execution record underneath it.
    activity_label: str | None = None
    keybind: str | None = None
    group: str = "general"
    hidden: bool = False
    enabled: EnabledPredicate | None = None
    suggested: bool = False
    # Prompt-template command fields (docs/reference/commands.md §4.2). ``template`` set
    # marks the command as a prompt-template; ``hints`` is derived from it for
    # display. Plain command/skill specs leave these at their defaults.
    template: str | None = None
    agent: str | None = None
    model: str | None = None
    subtask: bool = False
    hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaletteEntry:
    spec: CommandSpec
    enabled: bool


class CommandRegistry:
    """The one place command data lives; help/completion/palette/keybind derive."""

    def __init__(self) -> None:
        self._by_name: dict[str, CommandSpec] = {}
        self._by_key: dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec) -> None:
        for key in (spec.name, *spec.aliases):
            if key in self._by_name:
                raise ValueError(f"duplicate command: /{key}")
        # Only mutate after all-keys validated, so a conflict leaves state clean.
        for key in (spec.name, *spec.aliases):
            self._by_name[key] = spec
        if spec.keybind is not None:
            norm = _normalize_keybind(spec.keybind)
            if norm in self._by_key:
                raise ValueError(
                    f"duplicate keybind {norm!r}: /{self._by_key[norm].name} vs /{spec.name}"
                )
            self._by_key[norm] = spec

    def get(self, name: str) -> CommandSpec | None:
        return self._by_name.get(name.lstrip("/"))

    def by_keybind(self, key_event: str) -> CommandSpec | None:
        return self._by_key.get(_normalize_keybind(key_event))

    def _unique_specs(self) -> list[CommandSpec]:
        """Distinct specs (aliases collapse to one), ordered by name."""
        seen: dict[int, CommandSpec] = {}
        for spec in self._by_name.values():
            seen[id(spec)] = spec
        return sorted(seen.values(), key=lambda s: s.name)

    # ── derived views (single source of truth) ───────────────────────────────
    def help_lines(self) -> list[str]:
        groups: dict[str, list[CommandSpec]] = defaultdict(list)
        for spec in self._unique_specs():
            if not spec.hidden:
                groups[spec.group].append(spec)
        out: list[str] = []
        for group in sorted(groups):
            out.append(f"  {group.upper()}")
            for spec in groups[group]:
                key = f"  [{spec.keybind}]" if spec.keybind else ""
                hint = f" {' '.join(spec.hints)}" if spec.hints else ""
                name = f"/{spec.name}{hint}"
                out.append(f"    {name:<20} {spec.summary}{key}")
        return out

    def completion_names(self) -> list[str]:
        return sorted(
            f"/{name}"
            for name, spec in self._by_name.items()
            if not spec.hidden
        )

    def completion_items(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """``(name, summary, hints)`` per distinct visible command, name-sorted.

        Richer than :meth:`completion_names` for completion UIs that show the
        description and argument hints alongside each ``/name``.
        """
        return [
            (f"/{spec.name}", spec.summary, spec.hints)
            for spec in self._unique_specs()
            if not spec.hidden
        ]

    def palette_entries(self, ctx: object) -> list[PaletteEntry]:
        entries: list[PaletteEntry] = []
        for spec in self._unique_specs():
            if spec.hidden:
                continue
            enabled = spec.enabled is None or bool(spec.enabled(ctx))
            entries.append(PaletteEntry(spec=spec, enabled=enabled))
        return entries


# Default process-level registry used by the bare @command decorator.
DEFAULT_REGISTRY = CommandRegistry()


def command(
    name: str,
    summary: str,
    *,
    registry: CommandRegistry | None = None,
    aliases: tuple[str, ...] = (),
    arg_completer: ArgCompleter | None = None,
    run_while_busy: bool = False,
    busy_label: str | None = None,
    keybind: str | None = None,
    group: str = "general",
    hidden: bool = False,
    enabled: EnabledPredicate | None = None,
    suggested: bool = False,
    hints: tuple[str, ...] = (),
) -> Callable[[CommandHandler], CommandHandler]:
    """Decorator: declare a command once; everything else derives (P1)."""

    target = registry if registry is not None else DEFAULT_REGISTRY

    def deco(fn: CommandHandler) -> CommandHandler:
        target.register(
            CommandSpec(
                name=name,
                summary=summary,
                handler=fn,
                aliases=aliases,
                arg_completer=arg_completer,
                run_while_busy=run_while_busy,
                busy_label=busy_label,
                keybind=keybind,
                group=group,
                hidden=hidden,
                enabled=enabled,
                suggested=suggested,
                hints=hints,
            )
        )
        return fn

    return deco


async def dispatch(raw: str, ctx: object, registry: CommandRegistry) -> CommandResult:
    """Resolve and run a slash command via the registry — no skill fallback (R4)."""
    word, _, args = raw.lstrip("/").partition(" ")
    spec = registry.get(word)
    if spec is None:
        return CommandResult(display=f"unknown command /{word} (/help for all)", is_error=True)
    return await spec.handler(ctx, args.strip())


@dataclass
class CommandContext:
    """Strongly-typed context passed to handlers (P4, no Any)."""

    engine: object              # EngineHandle narrow facade (ports.EngineHandle)
    app: object                 # AppContext
    registry: CommandRegistry
    permissions: object | None = None
    is_running: bool = False
    extra: dict[str, object] = field(default_factory=dict)


__all__ = [
    "CommandResult",
    "CommandSpec",
    "CommandRegistry",
    "CommandContext",
    "PaletteEntry",
    "CommandHandler",
    "ArgCompleter",
    "EnabledPredicate",
    "DEFAULT_REGISTRY",
    "command",
    "dispatch",
]
