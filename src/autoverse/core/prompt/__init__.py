"""Main-agent system prompt subsystem (docs/architecture/runtime-flow.md).

Assembles the user-facing engine's session-level system prompt from static
sections (``sections.py``) plus dynamic inputs (tool list / session mode /
environment / memory / extension fragments / user append) via the pure
:class:`PromptAssembler` (``assembler.py``).

``core`` purity: this package imports only ``ports``/``types`` + stdlib.
"""

from __future__ import annotations

from autoverse.core.prompt.assembler import PromptAssembler, build_tool_list
from autoverse.core.prompt.sections import PROMPT_SECTIONS, PromptSections

__all__ = [
    "PromptAssembler",
    "build_tool_list",
    "PROMPT_SECTIONS",
    "PromptSections",
]
