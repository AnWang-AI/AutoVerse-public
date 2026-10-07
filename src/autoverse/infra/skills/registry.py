"""SkillRegistry (skills.md §Registry and progressive disclosure).

``Skill`` is a named, reusable instruction bundle (name / description / body).
``SkillRegistry`` is the single source of truth (P1):

  - ``register``      first-wins; a bare-name clash is **rejected** (no silent
                      overwrite, §9.4) unless ``overwrite=True``.
  - ``available_section`` renders the Available Skills prompt section purely from
                      the registry (P1) — deleting a skill removes it from prompt.

Layering note: ``command_descriptors`` returns plain data the REPL frontend maps
to ``CommandSpec`` — ``infra`` never imports ``frontends`` (P3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, ClassVar

from autoverse.core.tools.base import Tool
from autoverse.infra.skills.spec import SkillDiagnostic, SkillSource, SkillSpec

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class Skill:
    """A reusable instruction bundle."""

    name: str
    description: str = ""
    body: str = ""
    source: SkillSource = "user"
    source_path: Path | None = None
    package_id: str = ""
    diagnostics: list[SkillDiagnostic] = field(default_factory=list)

    @classmethod
    def from_spec(cls, spec: SkillSpec) -> "Skill":
        """Build the runtime prompt-template object from strict ``SkillSpec``."""
        return cls(
            name=spec.name,
            description=spec.description,
            body=spec.body,
            source=spec.source,
            source_path=spec.source_path,
            package_id=spec.package_id,
        )

    def render(self, args: str = "") -> str:
        """Return the skill body, optionally appending explicit slash-command args."""
        if not args.strip():
            return self.body
        return f"{self.body.rstrip()}\n\nUser arguments: {args.strip()}"

    def matches_request(self, request: str) -> bool:
        """Best-effort match based on the standard ``description`` field."""
        if not self.description:
            return False
        haystack = request.lower()
        phrases = re.findall(r"'([^']+)'|\"([^\"]+)\"", self.description)
        for a, b in phrases:
            phrase = (a or b).strip().lower()
            if phrase and phrase in haystack:
                return True
        keywords = {
            w
            for w in _WORD_RE.findall(self.description.lower())
            if len(w) >= 4 and w not in _STOPWORDS
        }
        request_words = set(_WORD_RE.findall(haystack))
        return bool(keywords & request_words)

    def enabled_for(self, cwd: Path | None) -> bool:
        """All loaded standard skills are enabled; cwd is accepted for API stability."""
        return True


_STOPWORDS = frozenset(
    {
        "when",
        "user",
        "wants",
        "says",
        "this",
        "that",
        "with",
        "from",
        "your",
        "their",
        "into",
        "code",
        "want",
        "uses",
        "after",
        "before",
        "they",
        "them",
    }
)


@dataclass(frozen=True)
class SkillCommandDescriptor:
    """Frontend-agnostic data for a slash command derived from a skill (P1/P3)."""

    name: str
    summary: str


class SkillRegistry:
    """ExtensionSource + single source of truth for skills (P1)."""

    name: ClassVar[str] = "skills"

    def __init__(self, cwd: Path | None = None) -> None:
        self._skills: dict[str, Skill] = {}  # insertion order == priority (first-wins)
        self._aliases: dict[str, str] = {}  # ns:name -> bare key
        self._cwd = cwd
        self._diagnostics: list[SkillDiagnostic] = []

    def register(self, skill: Skill, *, overwrite: bool = False) -> None:
        """Register a skill under its bare name. Clash -> reject (§9.4) unless overwrite."""
        if skill.name in self._skills and not overwrite:
            raise ValueError(
                f"duplicate skill name {skill.name!r}; high-priority skill kept "
                f"(use a namespaced 'ns:name' alias instead of silent overwrite)"
            )
        self._skills[skill.name] = skill

    def register_from_dir(self, namespace: str, skills: list[Skill]) -> list[str]:
        """Register plugin-provided skills as ``ns:name`` (+ bare name first-wins)."""
        registered: list[str] = []
        for skill in skills:
            qualified = f"{namespace}:{skill.name}"
            self._skills[qualified] = skill
            self._aliases[qualified] = skill.name
            registered.append(qualified)
            if skill.name not in self._skills:  # bare name only if free (first-wins)
                self._skills[skill.name] = skill
        return registered

    def add_diagnostic(self, diagnostic: SkillDiagnostic) -> None:
        self._diagnostics.append(diagnostic)

    def add_diagnostics(self, diagnostics: list[SkillDiagnostic]) -> None:
        self._diagnostics.extend(diagnostics)

    def diagnostics(self) -> list[SkillDiagnostic]:
        return list(self._diagnostics)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def enabled_skills(self, cwd: Path | None = None) -> list[Skill]:
        return list(self._skills.values())

    def unique_enabled_skills(self, cwd: Path | None = None) -> list[Skill]:
        """Enabled skills with ns:name aliases collapsed to their bare entry."""
        seen: dict[int, Skill] = {}
        for key, skill in self._skills.items():
            if key in self._aliases:
                continue  # skip qualified alias; its bare entry already present
            seen[id(skill)] = skill
        return list(seen.values())

    def match(self, user_request: str, cwd: Path | None = None) -> list[Skill]:
        """Skills whose standard ``description`` matches the request (E7)."""
        return [
            s
            for s in self.unique_enabled_skills(cwd)
            if s.matches_request(user_request)
        ]

    def available_section(self, cwd: Path | None = None) -> str:
        """Derive the Available Skills prompt section from the registry (P1)."""
        return render_available_skills(self, cwd)

    def command_descriptors(self, cwd: Path | None = None) -> list[SkillCommandDescriptor]:
        """Plain data for the frontend to register as slash commands (P3 layering)."""
        out: list[SkillCommandDescriptor] = []
        for s in self.unique_enabled_skills(cwd):
            out.append(
                SkillCommandDescriptor(
                    name=s.name,
                    summary=s.description,
                )
            )
        return out

    # ── ExtensionSource ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        return None

    def contributed_tools(self) -> list[Tool]:
        # Expose a single model-invocable ``Skill`` tool bound to this registry
        # (docs/reference/skills.md). Imported lazily to avoid a module import cycle
        # (tool.py imports SkillRegistry).
        if not self.unique_enabled_skills():
            return []
        from typing import cast

        from autoverse.infra.skills.tool import SkillTool

        return cast("list[Tool]", [SkillTool(self)])

    def prompt_fragment(self) -> str:
        fragments = [self.available_section(), render_skill_diagnostics(self)]
        return "\n\n".join(fragment for fragment in fragments if fragment)

    async def teardown(self) -> None:
        return None


def render_available_skills(registry: SkillRegistry, cwd: Path | None = None) -> str:
    """Render the Available Skills section (docs/reference/skills.md)."""
    skills = registry.unique_enabled_skills(cwd)
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "Match the user's request against this list before responding; "
        "if a skill applies, invoke the Skill tool BEFORE anything else.",
    ]
    for s in skills:
        source = f" [{s.source}]" if s.source else ""
        lines.append(f"- **{s.name}**{source}: {s.description}")
    return "\n".join(lines)


def render_skill_diagnostics(registry: SkillRegistry, *, limit: int = 5) -> str:
    """Render a compact prompt note for discovered skills that failed to load."""
    diagnostics = [d for d in registry.diagnostics() if d.severity == "error"]
    if not diagnostics:
        return ""
    lines = [
        "## Skill Diagnostics",
        "Some discovered SKILL.md files were not loaded. If the user asks about "
        "skills, report these diagnostics instead of searching Claude-style paths.",
    ]
    for diag in diagnostics[:limit]:
        name = diag.skill or (diag.path.parent.name if diag.path is not None else "unknown")
        path = f" ({diag.path})" if diag.path is not None else ""
        lines.append(f"- {name}{path}: {diag.code}: {diag.message}")
    extra = len(diagnostics) - limit
    if extra > 0:
        lines.append(f"- ... {extra} more skill diagnostic(s)")
    return "\n".join(lines)


__all__ = [
    "Skill",
    "SkillCommandDescriptor",
    "SkillRegistry",
    "render_available_skills",
    "render_skill_diagnostics",
]
