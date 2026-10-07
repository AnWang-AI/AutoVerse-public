"""Plugin directory loader for Codex ``.codex-plugin/plugin.json`` packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoverse.infra.plugins.spec import (
    PLUGIN_ID_RE,
    PLUGIN_MANIFEST,
    PluginCapabilities,
    PluginCommandEntry,
    PluginHookEntry,
    PluginSkillRoot,
    PluginSpec,
    PluginSpecError,
    PluginToolEntry,
    PluginTrust,
)
from autoverse.infra.skills.loader import load_skills_dir


def load_plugin_spec(path: Path) -> PluginSpec:
    """Parse and validate one Codex ``plugin.json``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PluginSpecError("plugin manifest must be a JSON object")
    plugin_id = str(data.get("name", ""))
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise PluginSpecError(f"invalid plugin id {plugin_id!r}")
    interface = _mapping(data.get("interface", {}))
    author = _mapping(data.get("author", {}))
    display_name = str(interface.get("displayName", "") or plugin_id)
    version = str(data.get("version", ""))
    if not version:
        raise PluginSpecError(f"plugin {plugin_id!r} requires version")
    description = str(
        data.get("description", "")
        or interface.get("shortDescription", "")
        or interface.get("longDescription", "")
        or ""
    )
    skills = tuple(
        PluginSkillRoot(path=p, namespace=plugin_id)
        for p in _path_values(data.get("skills"))
    )
    hooks = tuple(PluginHookEntry(path=p) for p in _path_values(data.get("hooks")))
    commands: tuple[PluginCommandEntry, ...] = ()

    return PluginSpec(
        id=plugin_id,
        name=display_name,
        version=version,
        description=description,
        publisher=str(author.get("name", "") or interface.get("developerName", "") or "local"),
        license=str(data.get("license", "") or ""),
        homepage=str(data.get("homepage", "") or interface.get("websiteURL", "") or ""),
        capabilities=_capabilities(data),
        trust=PluginTrust(),
        skills=skills,
        tools=(),
        hooks=hooks,
        commands=commands,
        root=path.parent.parent,
    )


def load_plugin_dir(path: Path) -> Any:
    """Build the runtime ``Plugin`` object for one plugin directory."""
    from autoverse.infra.plugins.manager import Plugin, PluginManifest

    spec = load_plugin_spec(path / PLUGIN_MANIFEST)
    skills = []
    if spec.capabilities.skills:
        roots = spec.skills
        default_root = path / "skills"
        if default_root.is_dir() and all(_clean_path(r.path) != "skills" for r in roots):
            roots = (*roots, PluginSkillRoot("skills", namespace=spec.id))
        for root in roots:
            if not root.path:
                continue
            skills.extend(
                load_skills_dir(
                    path / root.path,
                    source="plugin",
                    package_id=f"{spec.id}@{spec.publisher}",
                )
            )
            for skill in skills:
                if not skill.package_id:
                    skill.package_id = f"{spec.id}@{spec.publisher}"
    return Plugin(
        manifest=PluginManifest(
            name=spec.id,
            version=spec.version,
            description=spec.description,
            api_version="2",
            provides=spec.capabilities.names(),
            publisher=spec.publisher,
            install_key=spec.install_key,
        ),
        root=path,
        skills=skills,
    )


def load_plugin_dirs(roots: list[Path]) -> list[Any]:
    """Scan plugin roots for child directories containing Codex plugin manifests."""
    plugins: list[Any] = []
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (child / PLUGIN_MANIFEST).is_file():
                continue
            try:
                plugins.append(load_plugin_dir(child))
            except (OSError, PluginSpecError):
                continue
    return plugins


def _capabilities(value: dict[str, Any]) -> PluginCapabilities:
    return PluginCapabilities(
        skills=bool(value.get("skills", False)),
        hooks=bool(value.get("hooks", False)),
        mcp=bool(value.get("mcpServers", False)),
        assets=bool(value.get("assets", False) or value.get("interface", False)),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _path_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (_clean_path(value),)
    if isinstance(value, list):
        return tuple(_clean_path(v) for v in value if isinstance(v, str) and v)
    return ()


def _clean_path(value: str) -> str:
    return value.removeprefix("./").rstrip("/")


__all__ = [
    "load_plugin_dir",
    "load_plugin_dirs",
    "load_plugin_spec",
]
