"""Extension wiring (docs/reference/extensions.md).

Builds the four :class:`~autoverse.app.extension.ExtensionSource` managers from the
config tree, runs their lifecycle (``setup`` with per-source failure isolation),
distributes plugin contributions into the skill/hook registries, registers every
contributed tool into the shared :class:`~autoverse.core.tools.registry.ToolRegistry`,
and installs the sources on the :class:`~autoverse.app.context.AppContext` so the prompt
assembler projects their fragments (P1/P8).

Lives in ``app/`` so it may import ``core`` + ``infra`` (P3). The returned
:class:`~autoverse.infra.hooks.engine.HookEngine` lets the builder wrap the tool
dispatch with the PreToolUse/PostToolUse middleware (docs/reference/hooks.md).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from autoverse.app.config import Config, MCPServerConfig
from autoverse.app.context import AppContext
from autoverse.app.extension_admin import ExtensionAdminService
from autoverse.app.options import EngineOptions
from autoverse.core.tools.base import Tool
from autoverse.core.tools.registry import ToolRegistry
from autoverse.infra.hooks.engine import HookEngine
from autoverse.infra.hooks.shell import parse_hooks_config
from autoverse.infra.mcp.manager import MCPManager
from autoverse.infra.mcp.manager import MCPServerConfig as MCPRuntimeConfig
from autoverse.infra.plugins.manager import PluginManager
from autoverse.infra.skills.discovery import default_skill_roots, discover_skills
from autoverse.infra.skills.registry import SkillRegistry
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import ErrorPayload, make_event

_CAP_BY_NAME = {
    "read_only": CapabilityTier.READ_ONLY,
    "controlled_write": CapabilityTier.CONTROLLED_WRITE,
    "arbitrary_write": CapabilityTier.ARBITRARY_WRITE,
    "process": CapabilityTier.PROCESS,
}


async def wire_extensions(
    opts: EngineOptions, config: Config, app: AppContext
) -> HookEngine | None:
    """Wire MCP/Skills/Hooks/Plugins into ``app`` and return the HookEngine (if any).

    The returned engine is used by the builder to wrap tool dispatch with the
    PreToolUse/PostToolUse middleware. Returns ``None`` only if no HookEngine is
    present among the sources (it always is on the default path).
    """
    sources: list[Any]
    if opts.extension_sources is not None:
        sources = list(opts.extension_sources)  # DI seam (tests / embedding)
    else:
        sources = _default_sources(opts, config)

    skills = _first(sources, SkillRegistry)
    hooks = _first(sources, HookEngine)
    plugins = _first(sources, PluginManager)

    # Lifecycle: setup each source, isolating failures (docs/reference/extensions.md).
    for src in sources:
        try:
            await src.setup()
        except Exception as exc:  # noqa: BLE001 — one bad source must not abort assembly
            await _warn(app, getattr(src, "name", "?"), exc)

    # Plugins are a wholesale channel: distribute their hooks/skills into the
    # single source of truth for each (docs/reference/extensions.md) before tools/prompt
    # are derived, so the SkillTool and Hook constraints reflect plugin additions.
    if plugins is not None:
        if hooks is not None:
            try:
                hooks.add_all(plugins.contributed_hooks())
            except Exception as exc:  # noqa: BLE001
                await _warn(app, "plugins->hooks", exc)
        if skills is not None:
            for namespace, plugin_skills in plugins.contributed_skills().items():
                try:
                    skills.register_from_dir(namespace, plugin_skills)
                except Exception as exc:  # noqa: BLE001
                    await _warn(app, f"plugins->skills:{namespace}", exc)

    # Contributed tools → the shared registry (docs/reference/extensions.md). MCP also
    # exposes the two resource tools separately; register both groups.
    registry = app.tools if isinstance(app.tools, ToolRegistry) else None
    if registry is not None:
        for src in sources:
            tools: list[Tool] = []
            try:
                tools.extend(src.contributed_tools())
                resource_tools = getattr(src, "resource_tools", None)
                if callable(resource_tools):
                    tools.extend(resource_tools())
            except Exception as exc:  # noqa: BLE001
                await _warn(app, f"{getattr(src, 'name', '?')} tools", exc)
                continue
            if tools:
                try:
                    registry.register(str(src.name), tools)
                except Exception as exc:  # noqa: BLE001 — conflict/dup is reported, not fatal
                    await _warn(app, f"register {src.name}", exc)

    # ④ persist for prompt projection (builder reads app.extension_sources) + teardown.
    app.extension_sources = tuple(sources)
    app.extra["extension_admin"] = ExtensionAdminService(
        skills=skills,
        plugins=plugins,
        user_skill_root=Path.home() / ".autoverse" / "skills",
    )
    return hooks


def _default_sources(opts: EngineOptions, config: Config) -> list[Any]:
    """Construct the default extension sources from config."""
    cwd: Path = opts.cwd
    sources: list[Any] = []

    # Skills: project + user portable SKILL.md dirs. Discovery is explicit and
    # records diagnostics for invalid packages without rejecting Claude/Codex style.
    skills = SkillRegistry(cwd=cwd)
    discovered = discover_skills(default_skill_roots(cwd))
    skills.add_diagnostics(list(discovered.diagnostics))
    for skill in discovered.skills:
        if skills.get(skill.name) is None:
            skills.register(skill)
    sources.append(skills)

    # Plugins (wholesale; distributes into skills/hooks during wire_extensions).
    sources.append(PluginManager.from_config(config, cwd))

    # MCP: only construct a manager when servers are configured (no idle subprocs).
    mcp_configs = _translate_mcp(config.mcp_servers)
    if mcp_configs:
        sources.append(MCPManager(mcp_configs))

    # Hooks (parsed from config.hooks into shell-command HookDefs).
    sources.append(HookEngine(parse_hooks_config(config.hooks, cwd)))
    return sources


def _translate_mcp(
    servers: dict[str, MCPServerConfig],
) -> dict[str, MCPRuntimeConfig]:
    """Translate config-level MCP entries into runtime manager configs.

    The runtime config carries a single shlex-split command string; stdio
    ``args`` are appended. ``env``/``cwd``/``headers`` are not yet consumed by the
    runtime transport (future enhancement) and are intentionally not silently
    mapped here.
    """
    out: dict[str, MCPRuntimeConfig] = {}
    for name, cfg in servers.items():
        command = cfg.command or ""
        if command and cfg.args:
            command = shlex.join([*shlex.split(command), *cfg.args])
        capability = _CAP_BY_NAME.get(cfg.capability) if cfg.capability else None
        out[name] = MCPRuntimeConfig(
            name=name,
            command=command,
            url=cfg.url or "",
            capability=capability,
            needs_network=cfg.needs_network,
            rpc_timeout=cfg.rpc_timeout,
        )
    return out


def _first(sources: list[Any], kind: type) -> Any | None:
    return next((s for s in sources if isinstance(s, kind)), None)


async def _warn(app: AppContext, where: str, exc: Exception) -> None:
    try:
        await app.events.emit(
            make_event(
                ErrorPayload(
                    message=f"extension {where} failed: {type(exc).__name__}: {exc}",
                    error_class="ExtensionSetupError",
                    recoverable=True,
                    phase="extension",
                )
            )
        )
    except Exception:  # noqa: BLE001 — never let warning emission break assembly
        pass


__all__ = ["wire_extensions"]
