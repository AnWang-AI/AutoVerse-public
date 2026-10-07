"""Utility tools (docs/reference/tools.md §11.5): tool_search / generate_uuid / hash_text / sleep.

All four are pure/near-pure and READ_ONLY. ``tool_search`` derives its view from
the live registry on ``ctx.app.tools`` (no reflection of private attributes, P3).
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import hashlib
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

_SLEEP_MAX_SECONDS = 300.0


class ToolSearchArgs(BaseModel):
    query: str = Field(..., description="Keyword(s) to match in tool name + description.")
    category: str | None = Field(
        None, description="Optional category filter (substring match on name/description)."
    )


@register_tool
class ToolSearchTool(BaseTool):
    name: ClassVar[str] = "tool_search"
    description: ClassVar[str] = "Search available tools by name or description keyword."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = ToolSearchArgs

    async def run(self, args: ToolSearchArgs, ctx: ToolContext) -> ToolResult:
        registry = getattr(ctx.app, "tools", None)
        if registry is None:
            return ToolResult.error(
                "No tool registry available in this context.", error_kind="no_registry"
            )
        q = args.query.lower().strip()
        cat = (args.category or "").lower().strip()
        rows: list[str] = []
        for tool in self._iter_tools(registry):
            name = getattr(tool, "name", "")
            desc = getattr(tool, "description", "")
            hay = f"{name} {desc}".lower()
            if q and q not in hay:
                continue
            if cat and cat not in hay:
                continue
            rows.append(f"- {name}: {desc}")
        if not rows:
            return ToolResult.ok(f"No tools match {args.query!r}.")
        return ToolResult.ok("\n".join(rows), match_count=len(rows))

    @staticmethod
    def _iter_tools(registry: Any) -> list[Any]:
        # Prefer a public iteration API; fall back to names()+get().
        instances = getattr(registry, "instances", None)
        if callable(instances):
            return list(instances())
        names = getattr(registry, "names", None)
        get = getattr(registry, "get", None)
        if callable(names) and callable(get):
            return [get(n) for n in names()]
        return []


class GenerateUuidArgs(BaseModel):
    count: int = Field(1, ge=1, le=20, description="How many UUIDs (1-20).")
    version: int = Field(4, description="UUID version: 1, 4, or 7.")


@register_tool
class GenerateUuidTool(BaseTool):
    name: ClassVar[str] = "generate_uuid"
    description: ClassVar[str] = "Generate one or more UUIDs (v1/v4/v7)."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = GenerateUuidArgs

    async def run(self, args: GenerateUuidArgs, ctx: ToolContext) -> ToolResult:
        if args.version not in (1, 4, 7):
            return ToolResult.error(
                f"Unsupported UUID version {args.version}; use 1, 4, or 7.",
                error_kind="invalid_args",
            )
        out = [self._one(args.version) for _ in range(args.count)]
        return ToolResult.ok("\n".join(out) + f"\n(version {args.version})")

    @staticmethod
    def _one(version: int) -> str:
        if version == 1:
            return str(uuid.uuid1())
        if version == 7:
            gen7 = getattr(uuid, "uuid7", None)
            if callable(gen7):
                return str(gen7())
            return str(uuid.uuid4())  # pragma: no cover - py<3.14 fallback
        return str(uuid.uuid4())


class HashTextArgs(BaseModel):
    text: str = Field(..., description="The string to hash.")
    algorithm: str = Field("sha256", description="md5/sha1/sha256/sha512.")
    encoding: str = Field("hex", description="hex or base64 output encoding.")


@register_tool
class HashTextTool(BaseTool):
    name: ClassVar[str] = "hash_text"
    description: ClassVar[str] = "Compute a cryptographic hash of a text string."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = HashTextArgs

    _ALGOS: ClassVar[frozenset[str]] = frozenset({"md5", "sha1", "sha256", "sha512"})

    async def run(self, args: HashTextArgs, ctx: ToolContext) -> ToolResult:
        algo = args.algorithm.lower()
        if algo not in self._ALGOS:
            return ToolResult.error(
                f"Unsupported algorithm {args.algorithm!r}; "
                f"use one of {sorted(self._ALGOS)}.",
                error_kind="invalid_args",
            )
        digest = hashlib.new(algo, args.text.encode("utf-8")).digest()
        if args.encoding == "base64":
            enc = _b64.b64encode(digest).decode("ascii")
        elif args.encoding == "hex":
            enc = digest.hex()
        else:
            return ToolResult.error(
                f"Unsupported encoding {args.encoding!r}; use hex or base64.",
                error_kind="invalid_args",
            )
        return ToolResult.ok(
            f"{algo}({args.encoding}): {enc}\n({len(args.text.encode('utf-8'))} bytes)"
        )


class SleepArgs(BaseModel):
    seconds: float = Field(..., ge=0.0, description="Seconds to pause (<=300).")


@register_tool
class SleepTool(BaseTool):
    name: ClassVar[str] = "sleep"
    description: ClassVar[str] = "Pause execution for a number of seconds (max 300)."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = SleepArgs

    async def run(self, args: SleepArgs, ctx: ToolContext) -> ToolResult:
        secs = min(args.seconds, _SLEEP_MAX_SECONDS)
        await asyncio.sleep(secs)
        return ToolResult.ok(f"Slept {secs:.3f}s.")


__all__ = [
    "ToolSearchTool",
    "GenerateUuidTool",
    "HashTextTool",
    "SleepTool",
]
