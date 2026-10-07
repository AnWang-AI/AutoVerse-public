"""NullDiagnostics — no-op DiagnosticsPort adapter (docs/reference/tools.md §8.2 seam)."""

from __future__ import annotations

from pathlib import Path

from autoverse.ports.diagnostics import Diagnostic


class NullDiagnostics:
    """Default diagnostics adapter: always returns no findings."""

    async def check(self, path: Path) -> tuple[Diagnostic, ...]:
        return ()


__all__ = ["NullDiagnostics"]
