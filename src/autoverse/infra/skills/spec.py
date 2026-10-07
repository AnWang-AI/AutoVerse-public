"""Portable skill specification models (skills.md §SkillSpec).

The spec module normalizes Claude/Codex-style ``SKILL.md`` frontmatter into one
runtime contract. It stays in ``infra.skills`` because it describes on-disk
adapter input; core only sees registered tools and prompt fragments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")

SkillSource = Literal["repo", "user", "admin", "system", "plugin", "standalone"]


@dataclass(frozen=True)
class SkillSpec:
    """Strict representation of standard ``SKILL.md`` frontmatter + body."""

    name: str
    description: str
    body: str
    source: SkillSource = "user"
    source_path: Path | None = None
    package_id: str = ""


@dataclass(frozen=True)
class SkillDiagnostic:
    """A load, discovery, install, or runtime diagnostic for a skill."""

    code: str
    message: str
    path: Path | None = None
    skill: str = ""
    severity: Literal["info", "warning", "error"] = "error"


class SkillSpecError(ValueError):
    """Raised when a ``SKILL.md`` file violates the portable skill contract."""


def spec_from_frontmatter(
    front: dict[str, Any],
    body: str,
    *,
    source: SkillSource = "user",
    source_path: Path | None = None,
    default_name: str = "",
    package_id: str = "",
) -> SkillSpec:
    """Build a runtime :class:`SkillSpec` from standard ``SKILL.md`` frontmatter."""

    name = _str(front, "name") or default_name
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillSpecError(f"invalid skill name {name!r}")
    description = _str(front, "description")
    if not description:
        raise SkillSpecError(f"skill {name!r} requires description")

    return SkillSpec(
        name=name,
        description=description,
        body=body,
        source=source,
        source_path=source_path,
        package_id=package_id,
    )


def _str(data: dict[str, Any], key: str) -> str:
    value = data.get(key, "")
    return str(value) if value is not None else ""


__all__ = [
    "SKILL_NAME_RE",
    "SkillDiagnostic",
    "SkillSource",
    "SkillSpec",
    "SkillSpecError",
    "spec_from_frontmatter",
]
