"""autoverse.infra.llm — the single LLMProvider implementation family.

codec (what to say) and transport (how to send + prove identity) are strictly
split (P6/KDR-2). One logical model may carry multiple transports reusing the
same codec.
"""

from __future__ import annotations

from autoverse.infra.llm.echo import EchoProvider
from autoverse.infra.llm.provider import WireProvider
from autoverse.infra.llm.registry import (
    ModelSpec,
    TransportSpec,
    get_spec,
    resolve_alias,
)
from autoverse.infra.llm.router import Router
from autoverse.infra.llm.transport.credential import CredentialResolver

__all__ = [
    "Router",
    "WireProvider",
    "EchoProvider",
    "CredentialResolver",
    "get_spec",
    "resolve_alias",
    "ModelSpec",
    "TransportSpec",
]
