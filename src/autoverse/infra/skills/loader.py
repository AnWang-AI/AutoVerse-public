"""Portable ``SKILL.md`` disk loader (skills.md §SkillSpec).

Parses ``SKILL.md`` files: a YAML frontmatter block delimited by ``---`` lines
followed by the markdown body. PyYAML is used when available; otherwise a small
pure-Python fallback parser handles the simple ``key: value`` + ``- item`` shape
the skill frontmatter uses (§9.2 — no hard PyYAML dependency).

Pure / network-free. ``infra`` only — no frontends/app imports (P3).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from autoverse.infra.skills.registry import Skill
from autoverse.infra.skills.spec import (
    SkillDiagnostic,
    SkillSource,
    SkillSpec,
    SkillSpecError,
    spec_from_frontmatter,
)

_DELIM = "---"
_IGNORED_SKILL_DIRS = frozenset(
    {
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
    }
)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a document into (frontmatter dict, body).

    A document with no leading ``---`` block yields ({}, whole-text).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIM:
        return {}, text
    # find the closing delimiter
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            end = i
            break
    if end is None:
        return {}, text
    front_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return _parse_yaml(front_text), body


def _parse_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — no PyYAML / parse error → pure fallback
        return _fallback_parse(text)


def _fallback_parse(text: str) -> dict[str, Any]:
    """Minimal pure-Python parser for ``key: value`` and ``key:\\n  - item`` (§9.2)."""
    out: dict[str, Any] = {}
    current_list_key: str | None = None
    current_map_key: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") and current_list_key is not None:
            lst = out.setdefault(current_list_key, [])
            if isinstance(lst, list):
                lst.append(_scalar(stripped[2:].strip()))
            continue
        if raw.startswith(" ") and current_map_key is not None and ":" in stripped:
            key, _, val = stripped.partition(":")
            mapping = out.get(current_map_key)
            if not isinstance(mapping, dict):
                mapping = {}
                out[current_map_key] = mapping
            mapping[key.strip()] = _scalar(val.strip())
            continue
        if ":" in raw and not raw.startswith(" "):
            key, _, val = raw.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                current_list_key = key
                current_map_key = key
                out.setdefault(key, [])
            else:
                current_list_key = None
                current_map_key = None
                out[key] = _scalar(val)
    # collapse empty lists that were never filled into "" only if not declared list
    return out


def _scalar(value: str) -> Any:
    v = value.strip().strip("'\"")
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def skill_spec_from_markdown(
    text: str,
    *,
    source: SkillSource = "user",
    default_name: str = "",
    source_path: Path | None = None,
    package_id: str = "",
) -> SkillSpec:
    front, body = parse_frontmatter(text)
    return spec_from_frontmatter(
        front,
        body,
        source=source,
        source_path=source_path,
        default_name=default_name,
        package_id=package_id,
    )


def skill_from_markdown(
    text: str,
    *,
    source: SkillSource = "user",
    default_name: str = "",
    source_path: Path | None = None,
    package_id: str = "",
) -> Skill:
    """Parse portable ``SKILL.md`` markdown and return runtime ``Skill``."""
    return Skill.from_spec(
        skill_spec_from_markdown(
            text,
            source=source,
            default_name=default_name,
            source_path=source_path,
            package_id=package_id,
        )
    )


def load_skill_file(path: Path, *, source: SkillSource = "user", package_id: str = "") -> Skill:
    text = path.read_text(encoding="utf-8")
    # default name = the skill directory name (claude convention: <dir>/SKILL.md)
    default = path.parent.name if path.name.upper() == "SKILL.MD" else path.stem
    return skill_from_markdown(
        text,
        source=source,
        default_name=default,
        source_path=path,
        package_id=package_id,
    )


def load_skills_dir(root: Path, *, source: SkillSource = "user", package_id: str = "") -> list[Skill]:
    """Discover every ``SKILL.md`` under ``root`` (recursively)."""
    skills, _ = load_skills_dir_with_diagnostics(root, source=source, package_id=package_id)
    return skills


def load_skills_dir_with_diagnostics(
    root: Path,
    *,
    source: SkillSource = "user",
    package_id: str = "",
) -> tuple[list[Skill], list[SkillDiagnostic]]:
    """Discover strict ``SKILL.md`` files, returning load diagnostics."""
    if not root.exists():
        return [], []
    out: list[Skill] = []
    diagnostics: list[SkillDiagnostic] = []
    for path in _iter_skill_files(root):
        try:
            out.append(load_skill_file(path, source=source, package_id=package_id))
        except SkillSpecError as exc:
            diagnostics.append(_skill_spec_diagnostic(path, str(exc)))
        except OSError as exc:
            diagnostics.append(
                SkillDiagnostic(
                    code="skill_read_failed",
                    message=str(exc),
                    path=path,
                )
            )
    return out, diagnostics


def _iter_skill_files(root: Path) -> list[Path]:
    """Return candidate ``SKILL.md`` files while pruning vendored dependency trees."""
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_SKILL_DIRS)
        if "SKILL.md" in files:
            found.append(Path(current) / "SKILL.md")
    return sorted(found)


def _skill_spec_diagnostic(path: Path, error: str) -> SkillDiagnostic:
    try:
        front, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        front = {}
    name = str(front.get("name") or path.parent.name)
    return SkillDiagnostic(
        code="invalid_skill_spec",
        message=error,
        path=path,
        skill=name,
    )


__all__ = [
    "parse_frontmatter",
    "skill_spec_from_markdown",
    "skill_from_markdown",
    "load_skill_file",
    "load_skills_dir",
    "load_skills_dir_with_diagnostics",
]
