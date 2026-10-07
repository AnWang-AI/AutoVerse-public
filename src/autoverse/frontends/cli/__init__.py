"""Terminal CLI frontend (docs/guides/using-cli-and-tui.md §3.7).

One-shot execution entry point. Assembles an engine via the explicit
:class:`~autoverse.app.builder.EngineBuilder` (returning a single
:class:`~autoverse.ports.EngineHandle`, never a positional tuple) and renders the
resulting ``StreamEvent`` stream.
"""

from __future__ import annotations

__all__: list[str] = []
