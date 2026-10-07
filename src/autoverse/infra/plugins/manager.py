"""PluginManager (docs/reference/extensions.md).

Discovery (two channels, §5.4):
  A. directory scan  — ``<dir>/<name>/`` containing a ``Plugin`` factory, used
     for local/project plugins.
  B. entry-points    — ``importlib.metadata.entry_points(group=...)`` for pip
     installed plugins; each entry resolves to a callable returning a ``Plugin``.

Both channels are injectable so tests stay offline: pass ``entry_point_loader``
(a callable returning ``list[Plugin]``) and/or ``dir_loader``. A real default
entry-point loader (``load_entry_point_plugins``) is provided for production.

Capabilities are *distributed*, not held (P1/P2): ``PluginManager`` collects each
plugin's tools/commands/hooks/skills so the ``EngineBuilder`` can hand them to the
respective subsystem source. Only categories listed in ``manifest.provides`` are
activated (P4 — explicit over "whatever is in the directory", §5.6).

Failure isolation (§3.3/§5.8): a broken plugin (factory raises, bad manifest) is
skipped, recorded as ``failed`` in ``plugin_status``, and the engine still starts.

Conflict detection (E8/§9.4): duplicate **tool** names across collected plugins
raise ``ExtensionConflictError`` — tool names are the model's call key and must
be unambiguous; silent overwrite is the hardest extension bug to debug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable

from autoverse.core.tools.base import Tool
from autoverse.infra.hooks.engine import HookDef
from autoverse.infra.plugins.spec import PluginCapabilityStatus, PluginState
from autoverse.infra.skills.registry import Skill

# Plugin capability categories (manifest.provides, §5.6).
KNOWN_CATEGORIES = frozenset({"tools", "commands", "hooks", "agents", "skills", "mcp", "assets"})


class ExtensionConflictError(Exception):
    """Raised when two extensions claim the same tool name (E8/§9.4)."""


@dataclass(frozen=True)
class PluginManifest:
    """Parsed plugin manifest — explicit capability declaration (P4)."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    api_version: str = "1"
    provides: frozenset[str] = frozenset()
    publisher: str = "local"
    install_key: str = ""


@dataclass(frozen=True)
class Plugin:
    """A discovered plugin and its (already resolved) capability contributions.

    ``tools_factory`` returns fresh ``Tool`` instances when invoked; commands /
    hooks / skills are plain data the manager forwards to their owning source.
    Only categories present in ``manifest.provides`` are activated (§5.6).
    """

    manifest: PluginManifest
    root: Path
    tools_factory: Callable[[], list[Tool]] | None = None
    commands: list[Any] = field(default_factory=list)
    hooks: list[HookDef] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name


@dataclass(frozen=True)
class PluginStatus:
    name: str
    state: PluginState
    reason: str = ""
    capabilities: tuple[PluginCapabilityStatus, ...] = ()


def detect_tool_name_conflicts(tools: Iterable[Tool]) -> None:
    """Raise ``ExtensionConflictError`` on the first duplicate tool name (E8)."""
    seen: set[str] = set()
    for tool in tools:
        name = tool.name
        if name in seen:
            raise ExtensionConflictError(
                f"duplicate tool name {name!r} contributed by extensions; "
                f"refusing to register (no silent overwrite, §9.4)"
            )
        seen.add(name)


# Supported plugin API version range (§9.2).
_API_MIN, _API_MAX = 1, 2

EntryPointLoader = Callable[[], list[Plugin]]
DirLoader = Callable[[list[Path]], list[Plugin]]


def load_entry_point_plugins(group: str = "autoverse.plugins") -> list[Plugin]:
    """Default entry-point loader (channel B). Broken entries are skipped."""
    from importlib.metadata import entry_points

    out: list[Plugin] = []
    eps = entry_points(group=group)
    for ep in eps:
        try:
            factory = ep.load()
            plugin = factory()
            if isinstance(plugin, Plugin):
                out.append(plugin)
        except Exception:  # noqa: BLE001 — skip broken plugin, keep the rest
            continue
    return out


class PluginManager:
    """ExtensionSource that discovers plugins and distributes their capabilities."""

    name: ClassVar[str] = "plugins"

    def __init__(
        self,
        *,
        plugin_dirs: list[Path] | None = None,
        disabled: set[str] | None = None,
        entry_point_loader: EntryPointLoader | None = None,
        dir_loader: DirLoader | None = None,
    ) -> None:
        self._plugin_dirs = plugin_dirs or []
        self._disabled = disabled or set()
        self._entry_point_loader = entry_point_loader
        self._dir_loader = dir_loader
        self._plugins: list[Plugin] = []
        self._status: dict[str, PluginStatus] = {}
        self._tools: list[Tool] = []
        self._commands: list[Any] = []
        self._hooks: list[HookDef] = []
        self._skills: dict[str, list[Skill]] = {}

    @classmethod
    def from_config(cls, config: Any, cwd: Path, **overrides: Any) -> "PluginManager":
        """Build from the app config, reading ``config.plugins.*`` defensively.

        Workstream D owns the ``plugins`` config section; this reads it via
        ``getattr`` so the manager works whether or not that section exists yet.
        """
        plugins_cfg = getattr(config, "plugins", None)
        disabled = set(getattr(plugins_cfg, "disabled", ()) or ())
        extra = [Path(p) for p in (getattr(plugins_cfg, "extra_paths", ()) or ())]
        dirs = [cwd / ".autoverse" / "plugins", Path.home() / ".autoverse" / "plugins", *extra]
        return cls(plugin_dirs=dirs, disabled=disabled, **overrides)

    # ── ExtensionSource ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        discovered = self._discover()
        for plugin in discovered:
            self._activate(plugin)
        # E8: cross-plugin tool-name conflict detection (no silent overwrite).
        detect_tool_name_conflicts(self._tools)

    def _discover(self) -> list[Plugin]:
        raw: list[Plugin] = []
        loader = self._dir_loader or _default_dir_loader
        raw.extend(loader(self._plugin_dirs))
        if self._entry_point_loader is not None:
            raw.extend(self._entry_point_loader())
        kept: list[Plugin] = []
        for plugin in raw:
            if plugin.name in self._disabled:  # E4: disabled filtered at discovery
                self._status[plugin.name] = PluginStatus(plugin.name, "disabled")
                continue
            if not _api_compatible(plugin.manifest.api_version):
                self._status[plugin.name] = PluginStatus(
                    plugin.name,
                    "failed",
                    f"plugin API version {plugin.manifest.api_version} not in "
                    f"supported range {_API_MIN}..{_API_MAX}",
                )
                continue
            kept.append(plugin)
        return kept

    def _activate(self, plugin: Plugin) -> None:
        cap_status: list[PluginCapabilityStatus] = []
        try:
            provides = plugin.manifest.provides
            if "tools" in provides and plugin.tools_factory is not None:
                self._tools.extend(plugin.tools_factory())
                cap_status.append(PluginCapabilityStatus("tools", "loaded"))
            if "commands" in provides:
                self._commands.extend(plugin.commands)
                cap_status.append(PluginCapabilityStatus("commands", "loaded"))
            if "hooks" in provides:
                self._hooks.extend(plugin.hooks)
                cap_status.append(PluginCapabilityStatus("hooks", "loaded"))
            if "skills" in provides:
                self._skills[plugin.name] = list(plugin.skills)
                cap_status.append(PluginCapabilityStatus("skills", "loaded"))
        except ExtensionConflictError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolate a broken plugin (§5.8)
            self._status[plugin.name] = PluginStatus(
                plugin.name, "failed", f"{type(exc).__name__}: {exc}"
            )
            return
        self._plugins.append(plugin)
        self._status[plugin.name] = PluginStatus(
            plugin.name, "loaded", capabilities=tuple(cap_status)
        )

    def contributed_tools(self) -> list[Tool]:
        return list(self._tools)

    def contributed_commands(self) -> list[Any]:
        return list(self._commands)

    def contributed_hooks(self) -> list[HookDef]:
        return list(self._hooks)

    def contributed_skills(self) -> dict[str, list[Skill]]:
        return {ns: list(skills) for ns, skills in self._skills.items()}

    def prompt_fragment(self) -> str:
        # Plugins project nothing directly; their tools/skills/hooks each project
        # via their own source (P1 — avoid duplicate prompt entries, §5.4).
        return ""

    async def teardown(self) -> None:
        return None

    # ── observability ─────────────────────────────────────────────────────────
    @property
    def plugin_status(self) -> dict[str, PluginStatus]:
        return dict(self._status)


def _api_compatible(api_version: str) -> bool:
    try:
        major = int(str(api_version).split(".")[0])
    except (ValueError, IndexError):
        return False
    return _API_MIN <= major <= _API_MAX


def _default_dir_loader(dirs: list[Path]) -> list[Plugin]:
    """Default directory channel: scan Codex ``.codex-plugin/plugin.json`` packages."""
    from autoverse.infra.plugins.loader import load_plugin_dirs

    return load_plugin_dirs(dirs)


__all__ = [
    "ExtensionConflictError",
    "Plugin",
    "PluginManager",
    "PluginManifest",
    "PluginStatus",
    "detect_tool_name_conflicts",
    "load_entry_point_plugins",
]
