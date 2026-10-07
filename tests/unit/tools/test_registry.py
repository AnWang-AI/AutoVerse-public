"""Unit: ToolRegistry build/register/rollback (T9)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from autoverse.core.tools import ToolRegistry
from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class _A(BaseModel):
    pass


def _mk(tool_name: str) -> BaseTool:
    class _T(BaseTool):
        name = tool_name
        description = "d"
        capability = CapabilityTier.READ_ONLY
        Args = _A

        async def run(self, args: _A, ctx: ToolContext) -> ToolResult:
            return ToolResult.ok("ok")

    return _T()


def test_t9_build_includes_builtins() -> None:
    import autoverse.core.tools.builtin  # noqa: F401 — trigger registration

    reg = ToolRegistry.build()
    assert "read_file" in reg.names()
    assert "bash" in reg.names()


def test_t9_build_filter_include() -> None:
    import autoverse.core.tools.builtin  # noqa: F401

    reg = ToolRegistry.build(include={"read_file"})
    assert reg.names() == ["read_file"]


def test_t9_runtime_register_and_dispose() -> None:
    reg = ToolRegistry.build(include=set())
    handle = reg.register("ext", [_mk("ext_tool")])
    assert "ext_tool" in reg.names()
    handle.dispose()
    assert "ext_tool" not in reg.names()
    handle.dispose()  # idempotent


def test_t9_register_rolls_back_on_duplicate() -> None:
    reg = ToolRegistry.build(include=set())
    reg.register("ext", [_mk("a")])
    with pytest.raises(ValueError):
        reg.register("ext2", [_mk("b"), _mk("a")])  # 'a' duplicate -> rollback
    assert "b" not in reg.names()
    assert "a" in reg.names()


def test_t9_schemas_derived() -> None:
    reg = ToolRegistry.build(include={"think"})
    schemas = reg.schemas()
    assert schemas[0]["name"] == "think"
