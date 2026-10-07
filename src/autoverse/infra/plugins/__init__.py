"""Plugins extension adapter (docs/reference/extensions.md).

A plugin is a packaged capability set: a single plugin may contribute tools,
commands, hooks and skills. ``PluginManager`` discovers plugins via entry-points
(``importlib.metadata``) and/or a directory scan, honours the ``plugins.disabled``
switch (E4), and **detects name conflicts** — two extensions providing the same
tool name raise ``ExtensionConflictError`` rather than silently overwriting (E8,
§9.4). Plugins are a "wholesale channel" (P1/P2): they distribute capabilities to
each subsystem's own source rather than holding cross-cutting logic.
"""

from __future__ import annotations

from autoverse.infra.plugins.manager import (
    ExtensionConflictError,
    Plugin,
    PluginManager,
    PluginManifest,
    PluginStatus,
    detect_tool_name_conflicts,
)
from autoverse.infra.plugins.loader import (
    load_plugin_dir,
    load_plugin_dirs,
    load_plugin_spec,
)
from autoverse.infra.plugins.spec import (
    PLUGIN_MANIFEST,
    PluginCapabilities,
    PluginCapabilityStatus,
    PluginSpec,
    PluginSpecError,
)

__all__ = [
    "ExtensionConflictError",
    "Plugin",
    "PluginManager",
    "PluginManifest",
    "PluginStatus",
    "detect_tool_name_conflicts",
    "load_plugin_dir",
    "load_plugin_dirs",
    "load_plugin_spec",
    "PLUGIN_MANIFEST",
    "PluginCapabilities",
    "PluginCapabilityStatus",
    "PluginSpec",
    "PluginSpecError",
]
