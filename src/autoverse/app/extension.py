"""ExtensionSource — the app-layer assembly contract for extensions (docs/reference/extensions.md).

A thin structural Protocol so :class:`~autoverse.app.builder.EngineBuilder` treats the
four extension mechanisms (MCP / Skills / Hooks / Plugins) **symmetrically** along one
lifeline: register → discover → lifecycle → prompt projection. It lives in ``app/``
(the assembly layer) so it may name :class:`~autoverse.core.tools.base.Tool`; ``core``
never imports it (P3). The infra managers (``MCPManager`` / ``SkillRegistry`` /
``HookEngine`` / ``PluginManager``) already match this shape structurally — the Protocol
just documents and type-checks the contract the builder relies on.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from autoverse.core.tools.base import Tool


@runtime_checkable
class ExtensionSource(Protocol):
    """One class of extension, uniformly wired into the engine."""

    name: str

    async def setup(self) -> None:
        """② discover + ③ start. Idempotent; failures isolated (do not raise to assembly)."""
        ...

    def contributed_tools(self) -> list[Tool]:
        """① The ``Tool`` instances this source contributes (MCP/plugin tools). [] if none."""
        ...

    def prompt_fragment(self) -> str:
        """④ The system-prompt fragment for this source (Available Skills / Hook
        constraints / Connected MCP Tools). '' when nothing changes model behaviour."""
        ...

    async def teardown(self) -> None:
        """③ Close. Release subprocesses / HTTP connections / pending tasks."""
        ...


__all__ = ["ExtensionSource"]
