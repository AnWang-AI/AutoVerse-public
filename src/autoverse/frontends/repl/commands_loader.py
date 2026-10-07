"""Markdown file command loader (docs/reference/commands.md §4.3).

Discovers ``{command,commands}/**/*.md`` under the project and global config
dirs, parses YAML frontmatter (``description`` / ``agent`` / ``model`` /
``subtask``) plus the markdown body (the prompt *template*), and registers each
as a prompt-template :class:`CommandSpec`. The command name is the file stem.

Frontmatter parsing reuses ``infra.skills.loader.parse_frontmatter`` rather than
re-implementing YAML handling (frontends may depend on infra; P1 reuse).
"""

from __future__ import annotations

from pathlib import Path

from autoverse.frontends.repl.commands import CommandRegistry, CommandResult, CommandSpec
from autoverse.frontends.repl.commands_template import derive_hints
from autoverse.infra.skills.loader import parse_frontmatter

# Directory search order (project-local first, then global). Each may use the
# singular ``command/`` or plural ``commands/`` spelling.
_PROJECT_BASES = (".autoverse", ".claude")
_DIR_NAMES = ("command", "commands")


def _candidate_dirs(cwd: Path, home: Path) -> list[Path]:
    dirs: list[Path] = []
    for base in _PROJECT_BASES:
        for name in _DIR_NAMES:
            dirs.append(cwd / base / name)
    for name in _DIR_NAMES:
        dirs.append(cwd / name)  # project root command(s)/
    for name in _DIR_NAMES:
        dirs.append(home / ".config" / "autoverse" / name)
    return dirs


def discover_command_files(cwd: Path, *, home: Path | None = None) -> list[Path]:
    """Return every ``*.md`` under the candidate command directories, in order.

    Files are de-duplicated by resolved path; earlier directories win so the
    first occurrence of a given name is the one registered.
    """
    home = home if home is not None else Path.home()
    seen: dict[Path, None] = {}
    out: list[Path] = []
    for directory in _candidate_dirs(cwd, home):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen[resolved] = None
            out.append(path)
    return out


def load_command_file(path: Path) -> CommandSpec:
    """Parse a single command markdown file into a prompt-template CommandSpec."""
    text = path.read_text(encoding="utf-8")
    front, body = parse_frontmatter(text)
    template = body.strip()
    name = path.stem
    summary = str(front.get("description") or f"custom command: {name}")
    agent = front.get("agent")
    model = front.get("model")
    subtask = bool(front.get("subtask", False))
    return CommandSpec(
        name=name,
        summary=summary,
        handler=_unreachable_handler,  # replaced at registration with a template handler
        group="custom",
        template=template,
        agent=str(agent) if agent is not None else None,
        model=str(model) if model is not None else None,
        subtask=subtask,
        hints=derive_hints(template),
    )


async def _unreachable_handler(ctx: object, args: str) -> CommandResult:  # pragma: no cover
    raise RuntimeError("template command handler not bound — use register_file_commands")


def register_file_commands(
    registry: CommandRegistry, cwd: Path, *, home: Path | None = None
) -> list[str]:
    """Discover, parse and register file commands; return the registered names.

    Name collisions are resolved **first-wins, skip-the-rest** (never raise): a
    file whose name already exists — whether a built-in, a skill, or an
    earlier-discovered file command — is skipped silently. This guarantees a
    stray or duplicate user command file can never crash REPL startup. Discovery
    order (``discover_command_files``) therefore defines precedence: project
    ``.autoverse/`` first, the global config dir last.
    """
    from autoverse.frontends.repl.builtin_cmds import make_template_handler

    registered: list[str] = []
    for path in discover_command_files(cwd, home=home):
        spec = load_command_file(path)
        if registry.get(spec.name) is not None:
            continue  # built-in / earlier-registered name wins; skip silently
        bound = CommandSpec(
            name=spec.name,
            summary=spec.summary,
            handler=make_template_handler(spec, cwd),
            group=spec.group,
            template=spec.template,
            agent=spec.agent,
            model=spec.model,
            subtask=spec.subtask,
            hints=spec.hints,
        )
        registry.register(bound)
        registered.append(spec.name)
    return registered


__all__ = [
    "discover_command_files",
    "load_command_file",
    "register_file_commands",
]
