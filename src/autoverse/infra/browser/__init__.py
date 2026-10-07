"""Browser automation backends wired by app assembly (docs/reference/tools.md §12).

Core owns only the ``BrowserBackend`` abstraction. This infra package provides
the concrete Playwright driver and the session-scoped lifecycle wrapper that
``EngineBuilder`` installs on ``AppContext.browser``.
"""

from __future__ import annotations

from autoverse.infra.browser.playwright import (
    BrowserRuntimeConfig,
    BrowserSession,
    PlaywrightBrowserBackend,
    playwright_available,
)

__all__ = [
    "BrowserRuntimeConfig",
    "BrowserSession",
    "PlaywrightBrowserBackend",
    "playwright_available",
]
