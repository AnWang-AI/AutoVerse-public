"""Skill root discovery (skills.md §目录发现).

Discovery keeps source priority explicit and returns data for the registry; it
does not modify prompt state directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autoverse.infra.skills.loader import load_skills_dir_with_diagnostics
from autoverse.infra.skills.registry import Skill
from autoverse.infra.skills.spec import SkillDiagnostic, SkillSource


@dataclass(frozen=True)
class SkillRoot:
    """A discovered skill root and its source kind."""

    path: Path
    source: SkillSource
    namespace: str = ""
    package_id: str = ""


@dataclass(frozen=True)
class SkillDiscoveryResult:
    """Skills plus diagnostics from discovered roots."""

    skills: tuple[Skill, ...]
    diagnostics: tuple[SkillDiagnostic, ...]
    roots: tuple[SkillRoot, ...]


def default_skill_roots(cwd: Path, *, home: Path | None = None) -> list[SkillRoot]:
    """Return repo-local roots from cwd→git-root plus the user root."""
    roots: list[SkillRoot] = []
    for directory in _cwd_to_repo_root(cwd):
        roots.append(SkillRoot(directory / ".autoverse" / "skills", "repo"))
    home_dir = home or Path.home()
    roots.append(SkillRoot(home_dir / ".autoverse" / "skills", "user"))
    return roots


def discover_skills(roots: list[SkillRoot]) -> SkillDiscoveryResult:
    skills: list[Skill] = []
    diagnostics: list[SkillDiagnostic] = []
    for root in roots:
        loaded, diags = load_skills_dir_with_diagnostics(
            root.path,
            source=root.source,
            package_id=root.package_id or root.namespace,
        )
        diagnostics.extend(diags)
        skills.extend(loaded)
    return SkillDiscoveryResult(tuple(skills), tuple(diagnostics), tuple(roots))


def _cwd_to_repo_root(cwd: Path) -> list[Path]:
    try:
        current = cwd.expanduser().resolve()
    except OSError:
        current = cwd.expanduser()
    dirs = [current]
    for parent in current.parents:
        dirs.append(parent)
        if (parent / ".git").exists():
            break
    return dirs


__all__ = [
    "SkillDiscoveryResult",
    "SkillRoot",
    "default_skill_roots",
    "discover_skills",
]
