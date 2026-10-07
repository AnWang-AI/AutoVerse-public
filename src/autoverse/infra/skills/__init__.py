"""Skills extension adapter (docs/reference/skills.md).

A ``SkillRegistry`` is the single source of truth (P1) for reusable
Claude/Codex-style instruction bundles. ``available_section`` derives the
"Available Skills" prompt section straight from the registry (P1) — there is no
second hand-maintained list.

Layering: this registry lives in ``infra`` and must not force an
``infra -> frontends`` import. It therefore exposes plain
``SkillCommandDescriptor`` data via ``command_descriptors`` that the REPL
frontend turns into ``CommandSpec`` objects itself.
"""

from __future__ import annotations

from autoverse.infra.skills.loader import (
    load_skill_file,
    load_skills_dir,
    load_skills_dir_with_diagnostics,
    parse_frontmatter,
    skill_from_markdown,
    skill_spec_from_markdown,
)
from autoverse.infra.skills.discovery import (
    SkillDiscoveryResult,
    SkillRoot,
    default_skill_roots,
    discover_skills,
)
from autoverse.infra.skills.installer import (
    SkillInstallRecord,
    SkillInstallResult,
    install_skill_package,
)
from autoverse.infra.skills.registry import (
    Skill,
    SkillCommandDescriptor,
    SkillRegistry,
    render_available_skills,
)
from autoverse.infra.skills.spec import (
    SkillDiagnostic,
    SkillSpec,
    SkillSpecError,
)
from autoverse.infra.skills.tool import SkillTool

__all__ = [
    "Skill",
    "SkillCommandDescriptor",
    "SkillRegistry",
    "render_available_skills",
    "SkillTool",
    "parse_frontmatter",
    "skill_spec_from_markdown",
    "skill_from_markdown",
    "load_skill_file",
    "load_skills_dir",
    "load_skills_dir_with_diagnostics",
    "SkillDiscoveryResult",
    "SkillRoot",
    "default_skill_roots",
    "discover_skills",
    "SkillInstallRecord",
    "SkillInstallResult",
    "install_skill_package",
    "SkillDiagnostic",
    "SkillSpec",
    "SkillSpecError",
]
