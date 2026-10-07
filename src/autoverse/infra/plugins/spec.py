"""Plugin specification models (skills.md §Codex plugin manifest).

Plugins are package/lifecycle objects. They distribute capabilities to the
owning registries; they do not execute those capabilities themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PLUGIN_MANIFEST = ".codex-plugin/plugin.json"
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

PluginState = Literal[
    "discovered",
    "disabled",
    "needs_review",
    "missing_dependencies",
    "loaded",
    "partial",
    "failed",
    "shadowed",
]


@dataclass(frozen=True)
class PluginCapabilities:
    """Capability categories declared by a plugin."""

    skills: bool = False
    tools: bool = False
    hooks: bool = False
    mcp: bool = False
    commands: bool = False
    assets: bool = False

    def names(self) -> frozenset[str]:
        return frozenset(
            name
            for name, enabled in (
                ("skills", self.skills),
                ("tools", self.tools),
                ("hooks", self.hooks),
                ("mcp", self.mcp),
                ("commands", self.commands),
                ("assets", self.assets),
            )
            if enabled
        )


@dataclass(frozen=True)
class PluginTrust:
    """Trust metadata declared by plugin manifest."""

    level: Literal["system", "admin", "user", "repo", "marketplace"] = "user"
    requires_review: bool = True
    managed: bool = False


@dataclass(frozen=True)
class PluginSkillRoot:
    path: str
    namespace: str = ""


@dataclass(frozen=True)
class PluginToolEntry:
    entry_point: str
    capability: str = "read_only"


@dataclass(frozen=True)
class PluginHookEntry:
    path: str


@dataclass(frozen=True)
class PluginCommandEntry:
    path: str


@dataclass(frozen=True)
class PluginSpec:
    """Strict representation of Codex ``.codex-plugin/plugin.json``."""

    id: str
    name: str
    version: str
    description: str = ""
    publisher: str = "local"
    license: str = ""
    homepage: str = ""
    min_autoverse: str = ""
    capabilities: PluginCapabilities = field(default_factory=PluginCapabilities)
    trust: PluginTrust = field(default_factory=PluginTrust)
    skills: tuple[PluginSkillRoot, ...] = ()
    tools: tuple[PluginToolEntry, ...] = ()
    hooks: tuple[PluginHookEntry, ...] = ()
    commands: tuple[PluginCommandEntry, ...] = ()
    root: Path | None = None

    @property
    def install_key(self) -> str:
        return f"{self.id}@{self.publisher}"


@dataclass(frozen=True)
class PluginCapabilityStatus:
    capability: str
    state: PluginState
    reason: str = ""


class PluginSpecError(ValueError):
    """Raised when ``.codex-plugin/plugin.json`` violates the plugin contract."""


__all__ = [
    "PLUGIN_ID_RE",
    "PLUGIN_MANIFEST",
    "PluginCapabilities",
    "PluginCapabilityStatus",
    "PluginCommandEntry",
    "PluginHookEntry",
    "PluginSkillRoot",
    "PluginSpec",
    "PluginSpecError",
    "PluginState",
    "PluginToolEntry",
    "PluginTrust",
]
