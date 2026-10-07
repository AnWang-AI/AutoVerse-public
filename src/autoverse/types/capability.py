"""Capability tiers (docs/architecture/contracts.md §4.1, authoritative).

Filesystem/execution capability is a *linear total order* of four tiers
(higher includes lower). Network access is NOT part of this chain; it is an
orthogonal boolean (`needs_network` on tools / `can_network` on agents).
"""

from __future__ import annotations

from enum import IntEnum


class CapabilityTier(IntEnum):
    """Linear total order of filesystem/execution capability (higher ⊇ lower)."""

    READ_ONLY = 0          # pure observation: read/glob/grep/think/tool_search
    CONTROLLED_WRITE = 1   # reversible in-workspace writes: write/edit/todo/notebook
    ARBITRARY_WRITE = 2    # escape-workspace / irreversible delete: delete_file
    PROCESS = 3            # process execution: bash (most dangerous)


__all__ = ["CapabilityTier"]
