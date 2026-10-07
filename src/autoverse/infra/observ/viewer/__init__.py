"""Trace viewer — read-only JSON data API + external static SPA assets.

The server (``server.py``) serves only data; the SPA assets live as external
files under ``static/`` and are read from disk, never embedded as Python string
literals (docs/reference/events-and-storage.md §5, O5).
"""

from __future__ import annotations

from autoverse.infra.observ.viewer.server import TraceViewer, static_dir

__all__ = ["TraceViewer", "static_dir"]
