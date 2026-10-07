"""DiagnosticsPort — optional post-write code-quality feedback seam (docs/reference/tools.md §8.2).

core depends only on this Protocol; concrete adapters (LSP/Ruff) live in infra
and are out of scope for this phase. A `NullDiagnostics` no-op adapter is the
default wired into ToolContext.diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Diagnostic:
    """A single diagnostic finding for a file."""

    path: str
    line: int
    column: int
    severity: str  # error | warning | info | hint
    message: str
    source: str = ""


@runtime_checkable
class DiagnosticsPort(Protocol):
    async def check(self, path: Path) -> tuple[Diagnostic, ...]: ...


__all__ = ["Diagnostic", "DiagnosticsPort"]
