"""ToolRegistry & registration (docs/reference/tools.md §8, §9).

Builtin tools self-register via the `@register_tool` decorator into a module-level
declaration list. `ToolRegistry.build` instantiates a filtered subset (fail-fast
on bad API schemas). Extension sources can register tools at runtime via
`register`, which returns a `Registration` handle for clean (idempotent) rollback.
"""

from __future__ import annotations

from typing import Iterable, TypeVar, cast

from autoverse.core.tools.base import Tool
from autoverse.types.llm import ToolSchema

_ToolT = TypeVar("_ToolT", bound=type)

# Declaration table populated at import time by @register_tool.
_REGISTRY_DECL: list[type[Tool]] = []


def register_tool(cls: _ToolT) -> _ToolT:
    """Class decorator: add a tool type to the builtin declaration table."""
    _REGISTRY_DECL.append(cast("type[Tool]", cls))
    return cls


class Registration:
    """Handle for a runtime tool registration; `dispose` is idempotent."""

    def __init__(self, registry: "ToolRegistry", source_id: str) -> None:
        self._registry = registry
        self._source_id = source_id
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._registry._unregister(self._source_id)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._owner: dict[str, str] = {}

    @classmethod
    def build(
        cls,
        include: Iterable[str] | None = None,
        extra: Iterable[Tool] | None = None,
    ) -> "ToolRegistry":
        """Instantiate the declared builtin tools (optionally filtered)."""
        reg = cls()
        include_set = set(include) if include is not None else None
        for tool_cls in _REGISTRY_DECL:
            name = tool_cls.name
            if include_set is not None and name not in include_set:
                continue
            inst = tool_cls()
            reg._add("builtin", inst)
        for tool in extra or ():
            reg._add("builtin", tool)
        # fail-fast: every registered tool must produce a valid API schema.
        for tool in reg._tools.values():
            schema = tool.to_api_schema()  # type: ignore[attr-defined]
            if "name" not in schema or "input_schema" not in schema:
                raise ValueError(f"Tool '{tool.name}' produced an invalid API schema")
        return reg

    def _add(self, source_id: str, tool: Tool) -> None:
        name = tool.name
        if name in self._tools:
            raise ValueError(f"Duplicate tool name: {name}")
        self._tools[name] = tool
        self._owner[name] = source_id

    def register(self, source_id: str, tools: Iterable[Tool]) -> Registration:
        """Register runtime tools under a source id; returns a rollback handle."""
        added: list[str] = []
        try:
            for tool in tools:
                self._add(source_id, tool)
                added.append(tool.name)
        except Exception:  # noqa: BLE001 — rollback partial runtime registration
            for name in added:  # roll back partial registration
                self._tools.pop(name, None)
                self._owner.pop(name, None)
            raise
        return Registration(self, source_id)

    def _unregister(self, source_id: str) -> None:
        for name in [n for n, o in self._owner.items() if o == source_id]:
            self._tools.pop(name, None)
            self._owner.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict[str, object]]:
        return [t.to_api_schema() for t in self._tools.values()]  # type: ignore[attr-defined]

    def tool_schemas(self) -> list[ToolSchema]:
        """Project registered tools to LLM-facing ``ToolSchema`` (P1 single source).

        The wire codec consumes ``LLMRequest.tools``; this is the one conversion
        point from the registry's internal Tool objects to that contract. ``tier``
        carries the declared capability so wire codecs that gate by tier can.
        """
        out: list[ToolSchema] = []
        for tool in self._tools.values():
            api = tool.to_api_schema()  # type: ignore[attr-defined]
            out.append(
                ToolSchema(
                    name=str(api.get("name", "")),
                    description=str(api.get("description", "")),
                    json_schema=dict(api.get("input_schema", {})),
                    tier=int(getattr(tool, "capability", 0)),
                )
            )
        return out


__all__ = ["register_tool", "Registration", "ToolRegistry", "_REGISTRY_DECL"]
