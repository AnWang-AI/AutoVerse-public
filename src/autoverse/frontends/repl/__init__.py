"""Terminal REPL frontend (docs/guides/using-cli-and-tui.md).

A consumer-side Adapter over ``core``: it renders strongly-typed ``StreamEvent``
streams into human-readable terminal output and forwards user input to an
``EngineHandle``. It holds no domain logic and never reflects engine privates
(P3); it depends only on ``autoverse.ports`` / ``autoverse.types`` / ``autoverse.app``.
"""

from __future__ import annotations

__all__: list[str] = []
