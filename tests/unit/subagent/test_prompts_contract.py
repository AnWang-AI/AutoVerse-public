"""P8 prompt-contract consistency tests (docs/reference/subagents.md §11, S11 four+ assertions).

Every agent that declares a behaviour-changing mechanism must disclose it in its
prompt. This pins P8 as a CI red line —防回归.
"""

from __future__ import annotations

from autoverse.core.subagent.definition import AGENT_DEFINITIONS
from autoverse.types.capability import CapabilityTier


def test_post_audit_agents_warn_in_prompt() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        if d.isolation and d.isolation.post_audit:
            p = d.system_prompt.lower()
            assert "audit" in p or "审计" in p, f"{name} has post_audit but no audit disclosure"
            assert "verification_tainted" in p, f"{name} omits VERIFICATION_TAINTED"


def test_bound_lifecycle_agents_warn_against_detach() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        if d.isolation and d.isolation.process_lifecycle == "bound":
            p = d.system_prompt.lower()
            assert "detach" in p or "nohup" in p, f"{name} bound but no detach warning"


def test_forced_isolation_agents_state_it_in_prompt() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        if d.isolation and d.isolation.force_isolation:
            p = d.system_prompt.lower()
            assert (
                "worktree" in p or "isolat" in p or "隔离" in p
            ), f"{name} forced isolation but prompt silent"


def test_filtered_agents_declare_their_limits() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        if d.max_tier < CapabilityTier.PROCESS or d.bash_check_mode:
            p = d.system_prompt
            assert (
                "NO " in p or "Do NOT" in p or "do not" in p or "check_mode" in p
            ), f"{name} has a boundary but prompt does not state it"


def test_check_mode_agents_mention_it() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        if d.bash_check_mode:
            p = d.system_prompt.lower()
            assert (
                "check_mode" in p or "check-mode" in p
            ), f"{name} bash_check_mode but prompt never mentions check_mode"
            assert "bash runs in check_mode" in p, f"{name} missing the canonical disclosure"
