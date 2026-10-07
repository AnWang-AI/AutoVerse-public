"""Frontend-agnostic skill/plugin administration service (skills.md §管理面).

The service returns plain data for TUI, WebUI, and CLI callers. It deliberately
does not import frontend widgets or web types.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from autoverse.infra.plugins.manager import PluginManager
from autoverse.infra.skills.installer import SkillInstallResult, install_skill_package
from autoverse.infra.skills.registry import SkillRegistry


@dataclass(frozen=True)
class ExtensionListItem:
    id: str
    kind: Literal["skill", "plugin"]
    display_name: str
    source: str
    state: str
    enabled: bool
    trusted: bool
    summary: str
    diagnostics_count: int = 0


@dataclass(frozen=True)
class ExtensionDetail:
    item: ExtensionListItem
    body: str = ""
    path: str = ""
    diagnostics: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()


class ExtensionAdminService:
    """Query and manage extension state for frontends."""

    def __init__(
        self,
        *,
        skills: SkillRegistry | None,
        plugins: PluginManager | None,
        user_skill_root: Path,
    ) -> None:
        self._skills = skills
        self._plugins = plugins
        self._user_skill_root = user_skill_root
        self._disabled_skills: set[str] = set()
        self._trusted_skills: set[str] = set()
        self._disabled_plugins: set[str] = set()
        self._trusted_plugins: set[str] = set()

    def list_skills(self) -> list[ExtensionListItem]:
        if self._skills is None:
            return []
        diags = self._skills.diagnostics()
        by_skill: dict[str, int] = {}
        diag_by_skill: dict[str, list[str]] = {}
        for diag in diags:
            if diag.skill:
                by_skill[diag.skill] = by_skill.get(diag.skill, 0) + 1
                diag_by_skill.setdefault(diag.skill, []).append(diag.message)
        items: list[ExtensionListItem] = []
        loaded: set[str] = set()
        for skill in self._skills.unique_enabled_skills():
            loaded.add(skill.name)
            enabled = skill.name not in self._disabled_skills
            items.append(
                ExtensionListItem(
                    id=skill.name,
                    kind="skill",
                    display_name=skill.name,
                    source=skill.source,
                    state="enabled" if enabled else "disabled",
                    enabled=enabled,
                    trusted=skill.name in self._trusted_skills or skill.source in {"system", "admin"},
                    summary=skill.description,
                    diagnostics_count=by_skill.get(skill.name, 0),
                )
            )
        for name in sorted(diag_by_skill):
            if name in loaded:
                continue
            items.append(
                ExtensionListItem(
                    id=name,
                    kind="skill",
                    display_name=name,
                    source="discovered",
                    state="invalid",
                    enabled=False,
                    trusted=False,
                    summary=diag_by_skill[name][0],
                    diagnostics_count=by_skill.get(name, 0),
                )
            )
        return items

    def get_skill(self, name: str) -> ExtensionDetail | None:
        if self._skills is None:
            return None
        skill = self._skills.get(name)
        item = next((i for i in self.list_skills() if i.id == name), None)
        if item is None:
            return None
        if skill is None:
            matched = [d for d in self._skills.diagnostics() if d.skill == name]
            diagnostics = tuple(f"{d.code}: {d.message}" for d in matched)
            path = str(matched[0].path) if matched and matched[0].path is not None else ""
            return ExtensionDetail(item=item, path=path, diagnostics=diagnostics)
        diagnostics = tuple(
            f"{d.code}: {d.message}"
            for d in self._skills.diagnostics()
            if d.skill in ("", name) and (d.path is None or d.path == skill.source_path)
        )
        return ExtensionDetail(
            item=item,
            body=skill.body,
            path=str(skill.source_path or ""),
            diagnostics=diagnostics,
        )

    def diagnose_skill(self, name: str, prompt: str | None = None) -> str:
        detail = self.get_skill(name)
        if detail is None:
            return f"skill {name!r} not found"
        lines = [
            f"skill: {detail.item.id}",
            f"source: {detail.item.source}",
            f"state: {detail.item.state}",
            f"trusted: {detail.item.trusted}",
        ]
        if prompt and self._skills is not None:
            matches = [s.name for s in self._skills.match(prompt)]
            lines.append(f"matches prompt: {name in matches}")
        lines.extend(f"diagnostic: {d}" for d in detail.diagnostics)
        return "\n".join(lines)

    def install_skill(self, source: Path, *, overwrite: bool = False) -> SkillInstallResult:
        return install_skill_package(source, self._user_skill_root, overwrite=overwrite)

    def enable_skill(self, name: str) -> None:
        self._disabled_skills.discard(name)

    def disable_skill(self, name: str) -> None:
        self._disabled_skills.add(name)

    def trust_skill(self, name: str) -> None:
        self._trusted_skills.add(name)

    def list_plugins(self) -> list[ExtensionListItem]:
        if self._plugins is None:
            return []
        items: list[ExtensionListItem] = []
        for name, status in self._plugins.plugin_status.items():
            enabled = status.state != "disabled" and name not in self._disabled_plugins
            items.append(
                ExtensionListItem(
                    id=name,
                    kind="plugin",
                    display_name=name,
                    source="plugin",
                    state=status.state,
                    enabled=enabled,
                    trusted=name in self._trusted_plugins,
                    summary=status.reason,
                    diagnostics_count=1 if status.reason else 0,
                )
            )
        return items

    def get_plugin(self, name: str) -> ExtensionDetail | None:
        item = next((i for i in self.list_plugins() if i.id == name), None)
        if item is None or self._plugins is None:
            return None
        status = self._plugins.plugin_status.get(name)
        capabilities = tuple(c.capability for c in status.capabilities) if status else ()
        diagnostics = (status.reason,) if status and status.reason else ()
        return ExtensionDetail(item=item, diagnostics=diagnostics, capabilities=capabilities)

    def diagnose_plugin(self, name: str) -> str:
        detail = self.get_plugin(name)
        if detail is None:
            return f"plugin {name!r} not found"
        lines = [
            f"plugin: {detail.item.id}",
            f"state: {detail.item.state}",
            f"enabled: {detail.item.enabled}",
            f"trusted: {detail.item.trusted}",
        ]
        lines.extend(f"capability: {c}" for c in detail.capabilities)
        lines.extend(f"diagnostic: {d}" for d in detail.diagnostics)
        return "\n".join(lines)

    def enable_plugin(self, name: str) -> None:
        self._disabled_plugins.discard(name)

    def disable_plugin(self, name: str) -> None:
        self._disabled_plugins.add(name)

    def trust_plugin(self, name: str) -> None:
        self._trusted_plugins.add(name)


__all__ = [
    "ExtensionAdminService",
    "ExtensionDetail",
    "ExtensionListItem",
]
