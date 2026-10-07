"""Tool Protocol & BaseTool (docs/reference/tools.md §2, §8.1).

A Tool is a structural Protocol: anything that exposes the class-level metadata
(name/description/capability/needs_network), an `Args` pydantic model, and an
async `run(args, ctx)` coroutine satisfies it. `BaseTool` is the convenient ABC
that builtin tools subclass; it derives the JSON schema (P1) and the API schema
from `Args`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

if TYPE_CHECKING:
    from autoverse.core.tools.context import ToolContext


@runtime_checkable
class Tool(Protocol):
    """Structural contract every tool must satisfy."""

    name: ClassVar[str]
    description: ClassVar[str]
    capability: ClassVar[CapabilityTier]
    needs_network: ClassVar[bool]
    Args: ClassVar[type[BaseModel]]

    async def run(self, args: BaseModel, ctx: "ToolContext") -> ToolResult: ...


class BaseTool(ABC):
    """ABC base for builtin tools; derives schemas from the `Args` model (P1)."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]]

    @classmethod
    def input_json_schema(cls) -> dict[str, Any]:
        """Derive the JSON schema from the pydantic Args model (single source, P1)."""
        return cls.Args.model_json_schema()

    @classmethod
    def to_api_schema(cls) -> dict[str, Any]:
        """Shape consumed by the LLM tool-use API."""
        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": cls.input_json_schema(),
        }

    @abstractmethod
    async def run(self, args: Any, ctx: "ToolContext") -> ToolResult: ...


__all__ = ["Tool", "BaseTool"]
