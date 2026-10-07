"""Immutable browser observation values shared across browser adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserPageObservation:
    """One internally consistent observation of a rendered browser page."""

    page_id: str
    revision: int
    url: str
    title: str
    is_loading: bool
    active: bool
    semantic_snapshot: str
    semantic_truncated: bool = False
    visual_hint: bool = False
    screenshot: bytes | None = None
    screenshot_media_type: str | None = None
    screenshot_error: str | None = None


__all__ = ["BrowserPageObservation"]
