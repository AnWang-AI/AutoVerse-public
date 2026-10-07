"""Unit: URL guard (SSRF hardening for outbound network tools)."""

from __future__ import annotations

import pytest

from autoverse.core.tools.url_guard import check_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path",
        "http://example.com",
        "https://93.184.216.34/",  # public IP literal
    ],
)
def test_allows_public_http_urls(url: str) -> None:
    ok, reason = check_url(url)
    assert ok is True and reason == ""


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/plain,hi",
    ],
)
def test_rejects_non_http_schemes(url: str) -> None:
    ok, reason = check_url(url)
    assert ok is False and "scheme" in reason


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
    ],
)
def test_rejects_private_and_loopback_hosts(url: str) -> None:
    ok, reason = check_url(url)
    assert ok is False and reason


def test_rejects_url_without_host() -> None:
    ok, reason = check_url("http://")
    assert ok is False
