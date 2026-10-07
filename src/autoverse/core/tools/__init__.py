"""autoverse.core.tools — tool execution subsystem (docs/reference/tools.md).

Public surface for the tool layer: the Tool Protocol, BaseTool, the execution
context, the dispatcher middleware chain, the registry, and the cancellation
helpers. Builtin tools live in the `builtin` subpackage and self-register on
import.
"""

from __future__ import annotations

from autoverse.core.tools.abort_race import abort_result, race_with_abort
from autoverse.core.tools.base import BaseTool, Tool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.dispatcher import ToolDispatcher
from autoverse.core.tools.registry import (
    Registration,
    ToolRegistry,
    register_tool,
)
from autoverse.core.tools.tracker import FileTracker

__all__ = [
    "Tool",
    "BaseTool",
    "ToolContext",
    "ToolDispatcher",
    "ToolRegistry",
    "Registration",
    "register_tool",
    "FileTracker",
    "race_with_abort",
    "abort_result",
]
