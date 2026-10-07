"""autoverse.core.subagent — sub-agent orchestration (docs/reference/subagents.md).

``core`` purity: this package imports only ``types``/``ports``/sibling
``core.subagent`` modules + stdlib. It NEVER imports ``infra`` — the
Isolator/engine_factory/resource_lock/job_store are all injected ports.
"""

from __future__ import annotations

from autoverse.core.subagent.definition import (
    AGENT_DEFINITIONS,
    AgentDefinition,
    IsolationProfile,
    get_definition,
)
from autoverse.core.subagent.runner import (
    RunOutcome,
    SpawnRequest,
    SpawnResult,
    SubAgentRunner,
)

__all__ = [
    "AGENT_DEFINITIONS",
    "AgentDefinition",
    "IsolationProfile",
    "get_definition",
    "SpawnRequest",
    "SpawnResult",
    "RunOutcome",
    "SubAgentRunner",
]
