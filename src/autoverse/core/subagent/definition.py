"""AgentDefinition — the single source of truth for roles (docs/reference/subagents.md §3.5, §4).

All role metadata lives here. Prompt variants (e.g. reviewer compliance/quality)
live in separate dicts and do NOT pollute this dataclass schema. The isolation /
audit / process-lifecycle triple is folded into an optional ``IsolationProfile``
subobject — only e2e-verifier sets it; the other 8 roles have ``isolation=None``
(they cannot even see those three fields).

``core`` purity: imports only ``types`` + sibling ``core.subagent`` modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from autoverse.core.subagent.prompts import (
    ARCHITECT_PROMPT,
    BUILDER_PROMPT,
    CHECKPOINT_WRITER_PROMPT,
    DEBUGGER_PROMPT,
    E2E_VERIFIER_PROMPT,
    GENERALIST_PROMPT,
    RESEARCHER_PROMPT,
    REVIEWER_PROMPT,
    SCOUT_PROMPT,
    VALIDATOR_PROMPT,
)
from autoverse.types.capability import CapabilityTier
from autoverse.types.llm import ModelTier


@dataclass(frozen=True)
class IsolationProfile:
    """Isolation + audit + process policy. Only roles whose side-effecting writes
    must be mechanism-constrained (e2e-verifier) fill it."""

    force_isolation: bool = True  # force worktree (overrides caller)
    post_audit: str | None = None  # None / "git_diff_must_be_empty"
    process_lifecycle: str = "inherit"  # "inherit" / "bound"


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    max_tier: CapabilityTier  # filesystem capability ceiling (hard boundary)
    can_network: bool = False  # orthogonal network switch
    bash_check_mode: bool = False  # controlled read-only execution
    max_turns: int = 60  # safety valve, not a task budget
    wall_clock: float | None = None  # wall-clock safety valve (seconds)
    can_spawn: bool = False  # leaf-agent: cannot re-spawn by default
    system_prompt: str = ""
    # tools=None  → all non-blocked tools at this tier; tools=[...] → explicit subset
    tools: tuple[str, ...] | None = None
    # tier = the role's intrinsic intelligence need; config.model_tiers maps it to
    # a concrete model id. ``model`` (below) is a rare explicit hard override that
    # wins over the tier mapping (resolution order in runner._resolve_model).
    tier: ModelTier = ModelTier.BALANCED
    model: str | None = None
    omit_memory: bool = False  # trim project memory (only researcher=True)
    isolation: IsolationProfile | None = None


_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        name="scout",
        description="Fast, precise codebase lookup; a search engine, not an analyst.",
        max_tier=CapabilityTier.READ_ONLY,
        can_network=False,
        max_turns=30,
        tier=ModelTier.FAST,
        system_prompt=SCOUT_PROMPT,
        tools=("read_file", "glob", "grep"),
    ),
    AgentDefinition(
        name="architect",
        description="Produce an implementable design plan; research only, do not modify.",
        max_tier=CapabilityTier.READ_ONLY,
        can_network=False,
        max_turns=60,
        tier=ModelTier.DEEP,
        system_prompt=ARCHITECT_PROMPT,
        tools=("read_file", "glob", "grep"),
    ),
    AgentDefinition(
        name="builder",
        description="Implement changes per a plan; does not create the plan.",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        max_turns=400,
        system_prompt=BUILDER_PROMPT,
        tools=None,
    ),
    AgentDefinition(
        name="validator",
        description="Run tests/lint/build and report; a CI runner, not a developer.",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        bash_check_mode=True,
        max_turns=30,
        tier=ModelTier.FAST,
        system_prompt=VALIDATOR_PROMPT,
        tools=None,
    ),
    AgentDefinition(
        name="reviewer",
        description="Review quality/compliance (review_mode selects the variant).",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        bash_check_mode=True,
        max_turns=50,
        system_prompt=REVIEWER_PROMPT,
        tools=None,
    ),
    AgentDefinition(
        name="e2e-verifier",
        description="End-to-end runtime verification: real services, real requests.",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        bash_check_mode=False,
        max_turns=80,
        system_prompt=E2E_VERIFIER_PROMPT,
        tools=None,
        isolation=IsolationProfile(
            force_isolation=True,
            post_audit="git_diff_must_be_empty",
            process_lifecycle="bound",
        ),
    ),
    AgentDefinition(
        name="debugger",
        description="Systematic root-cause investigation and fix; investigate first.",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        max_turns=150,
        tier=ModelTier.DEEP,
        system_prompt=DEBUGGER_PROMPT,
        tools=None,
    ),
    AgentDefinition(
        name="researcher",
        description="External information gathering + structured report; cannot write code.",
        max_tier=CapabilityTier.READ_ONLY,
        can_network=True,
        max_turns=50,
        system_prompt=RESEARCHER_PROMPT,
        omit_memory=True,
        tools=("read_file", "glob", "grep", "web_fetch", "web_search"),
    ),
    AgentDefinition(
        name="generalist",
        description="General-purpose fallback when no specialized role clearly matches.",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        max_turns=60,
        system_prompt=GENERALIST_PROMPT,
        tools=None,
    ),
    # System-dispatched role (docs/reference/subagents.md §4.2a): not in the selection table; the
    # main agent cannot select it (CompactionPolicy dispatches it).
    AgentDefinition(
        name="checkpoint-writer",
        description="System checkpoint/memory reconciliation writer (not user-selectable).",
        max_tier=CapabilityTier.CONTROLLED_WRITE,
        can_network=False,
        max_turns=20,
        tier=ModelTier.FAST,
        system_prompt=CHECKPOINT_WRITER_PROMPT,
        tools=("read_file", "glob", "grep", "write_file", "edit_file"),
    ),
)

AGENT_DEFINITIONS: dict[str, AgentDefinition] = {d.name: d for d in _DEFINITIONS}

_DEFAULT_ROLE = "generalist"


def get_definition(subagent_type: str) -> AgentDefinition:
    """Resolve a role by name; unknown → generalist fallback (docs/reference/subagents.md §4.2)."""
    return AGENT_DEFINITIONS.get(subagent_type, AGENT_DEFINITIONS[_DEFAULT_ROLE])


def is_known_definition(subagent_type: str) -> bool:
    """True iff ``subagent_type`` is a registered role (vs a fallback resolution)."""
    return subagent_type in AGENT_DEFINITIONS


def _validate_definitions() -> None:
    """Registration-time invariants (docs/reference/subagents.md §3.5, §13).

    - ``tools=[]`` (empty, not None) is a suspicious config → reject.
    - Only e2e-verifier carries an IsolationProfile (the rest are None).
    - post_audit/bound roles must disclose the mechanism in their prompt (P8).
    """
    for name, d in AGENT_DEFINITIONS.items():
        if d.tools is not None and len(d.tools) == 0:
            raise ValueError(f"agent '{name}' declares an empty tools list (suspicious)")
        if d.isolation is not None:
            prof = d.isolation
            if prof.post_audit is not None and prof.post_audit != "git_diff_must_be_empty":
                raise ValueError(
                    f"agent '{name}' has unknown post_audit policy: {prof.post_audit}"
                )


_validate_definitions()


__all__ = [
    "IsolationProfile",
    "AgentDefinition",
    "AGENT_DEFINITIONS",
    "get_definition",
    "is_known_definition",
]
