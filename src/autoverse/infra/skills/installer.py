"""Standalone skill package installer (skills.md §独立 Skill 包).

The installer normalizes local or fetched skill packages into the user skill
store. Network/git fetching is intentionally delegated to the caller for now;
this module implements the deterministic filesystem path used by tests and UI
flows.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from autoverse.infra.skills.loader import load_skills_dir_with_diagnostics
from autoverse.infra.skills.spec import SkillDiagnostic

_IGNORED_INSTALL_NAMES = (
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
)


@dataclass(frozen=True)
class SkillInstallRecord:
    """Install metadata returned to callers; persistence is owned by a future state store."""

    name: str
    source: Literal["local", "git", "marketplace"]
    source_url: str
    installed_path: Path
    enabled: bool
    trusted_hash: str
    installed_at: float


@dataclass(frozen=True)
class SkillInstallResult:
    """Result of installing one source package."""

    records: tuple[SkillInstallRecord, ...]
    diagnostics: tuple[SkillDiagnostic, ...] = ()


def install_skill_package(
    source: Path,
    destination_root: Path,
    *,
    source_url: str | None = None,
    overwrite: bool = False,
) -> SkillInstallResult:
    """Install a local standalone skill package into ``destination_root``.

    A source can be a single-skill package (root contains ``SKILL.md``) or a
    collection package (children contain ``SKILL.md``). The package is copied so
    future reloads are stable even if the source path disappears.
    """
    src = source.expanduser().resolve()
    if not src.exists():
        return SkillInstallResult(
            (),
            (
                SkillDiagnostic(
                    code="install_source_missing",
                    message=f"skill install source does not exist: {src}",
                    path=src,
                ),
            ),
        )
    packages = _package_dirs(src)
    if not packages:
        return SkillInstallResult(
            (),
            (
                SkillDiagnostic(
                    code="no_skill_package",
                    message="source contains no SKILL.md at root or one level below",
                    path=src,
                ),
            ),
        )

    destination_root.mkdir(parents=True, exist_ok=True)
    diagnostics: list[SkillDiagnostic] = []
    records: list[SkillInstallRecord] = []
    for package in packages:
        skills, load_diags = load_skills_dir_with_diagnostics(package, source="standalone")
        diagnostics.extend(load_diags)
        if not skills:
            continue
        if len(skills) != 1:
            diagnostics.append(
                SkillDiagnostic(
                    code="ambiguous_skill_package",
                    message=f"standalone package must contain exactly one SKILL.md: {package}",
                    path=package,
                )
            )
            continue
        skill = skills[0]
        dest = destination_root / skill.name
        if dest.exists() and not overwrite:
            diagnostics.append(
                SkillDiagnostic(
                    code="installed_skill_conflict",
                    message=f"installed skill already exists: {skill.name}",
                    path=dest,
                    skill=skill.name,
                )
            )
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(package, dest, ignore=shutil.ignore_patterns(*_IGNORED_INSTALL_NAMES))
        trusted_hash = _hash_tree(dest)
        records.append(
            SkillInstallRecord(
                name=skill.name,
                source="local",
                source_url=source_url or str(src),
                installed_path=dest,
                enabled=True,
                trusted_hash=trusted_hash,
                installed_at=time.time(),
            )
        )
    return SkillInstallResult(tuple(records), tuple(diagnostics))


def _package_dirs(source: Path) -> list[Path]:
    if (source / "SKILL.md").is_file():
        return [source]
    return sorted(p for p in source.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


__all__ = [
    "SkillInstallRecord",
    "SkillInstallResult",
    "install_skill_package",
]
