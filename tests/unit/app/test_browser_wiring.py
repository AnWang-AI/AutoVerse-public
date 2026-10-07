"""Unit tests for browser backend assembly in EngineBuilder."""

from __future__ import annotations

import pytest

from autoverse.app import builder
from autoverse.app.config import BrowserConfig, Config
from autoverse.infra.browser import BrowserSession


def test_browser_wiring_disabled() -> None:
    config = Config(browser=BrowserConfig(backend="none"))

    assert builder._build_browser_session(config) is None  # noqa: SLF001


def test_browser_wiring_auto_without_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builder, "playwright_available", lambda: False)

    assert builder._build_browser_session(Config()) is None  # noqa: SLF001


def test_browser_wiring_auto_with_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builder, "playwright_available", lambda: True)

    session = builder._build_browser_session(Config())  # noqa: SLF001

    assert isinstance(session, BrowserSession)
