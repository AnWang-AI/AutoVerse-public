"""Unit: Tool Protocol & schema derivation (T1, T10, P1)."""

from __future__ import annotations

from pydantic import BaseModel

from autoverse.core.tools import BaseTool, Tool
from autoverse.core.tools.context import ToolContext
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class _DemoArgs(BaseModel):
    x: int
    label: str = "hi"


class DemoTool(BaseTool):
    name = "demo"
    description = "demo tool"
    capability = CapabilityTier.READ_ONLY
    Args = _DemoArgs

    async def run(self, args: _DemoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(str(args.x))


def test_t1_input_json_schema_derived_from_args() -> None:
    schema = DemoTool.input_json_schema()
    assert schema == _DemoArgs.model_json_schema()
    assert "x" in schema["properties"]


def test_t1_to_api_schema_shape() -> None:
    api = DemoTool.to_api_schema()
    assert api["name"] == "demo"
    assert api["description"] == "demo tool"
    assert api["input_schema"]["properties"]["x"]["type"] == "integer"


def test_t10_structural_protocol_satisfied() -> None:
    assert isinstance(DemoTool(), Tool)


def test_t10_metadata_classvars() -> None:
    t = DemoTool()
    assert t.name == "demo"
    assert t.capability == CapabilityTier.READ_ONLY
    assert t.needs_network is False
