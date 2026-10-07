"""C4 — web_fetch cache key includes max_length + no_cache parameter."""

from __future__ import annotations

import httpx
import pytest
import respx

from autoverse.core.tools.builtin.web import WebFetchArgs, WebFetchTool

from tests.conftest import make_ctx


async def test_web_fetch_cache_key_max_length(tmp_path) -> None:
    """Different max_length values produce independent cache entries.

    If the cache key only uses the URL, a request with max_length=500 that
    caches the full text would cause a later request with max_length=8000
    to get truncated output.  After the fix the cache key includes max_length
    so each value gets its own entry.
    """
    ctx = make_ctx(tmp_path)
    tool = WebFetchTool()

    # Pre-populate the cache with key "https://example.com:500"
    tool._cache.put("https://example.com:500", "short body" * 100)

    # Same (url, max_length) pair → cache hit
    res = await tool.run(WebFetchArgs(url="https://example.com", max_length=500), ctx)
    assert not res.is_error
    assert res.metadata["from_cache"] is True

    # Different max_length → cache miss → real HTTP request needed
    async with respx.mock() as mock:
        mock.get("https://example.com").respond(200, text="long body " * 2000)
        res2 = await tool.run(
            WebFetchArgs(url="https://example.com", max_length=8000), ctx
        )
        assert not res2.is_error
        # Should NOT be from cache because the key differed
        assert res2.metadata["from_cache"] is False


async def test_web_fetch_no_cache(tmp_path) -> None:
    """no_cache=True skips the cache read but still writes the response to cache.

    After a successful fetch with no_cache=True, a subsequent normal request
    (no_cache=False) for the same (url, max_length) must hit the cache.
    """
    ctx = make_ctx(tmp_path)
    tool = WebFetchTool()

    cache_key = "https://example.com:8000"

    # First call: no_cache=True — must skip the read.
    async with respx.mock() as mock:
        mock.get("https://example.com").respond(200, text="fresh data")
        res = await tool.run(
            WebFetchArgs(url="https://example.com", max_length=8000, no_cache=True), ctx
        )
    assert not res.is_error
    assert res.metadata["from_cache"] is False
    assert "fresh data" in res.output

    # Even though the first call had no_cache=True, it should have written to cache.
    assert tool._cache.get(cache_key) is not None

    # Second call: no_cache=False (default) — must now hit the cache.
    res2 = await tool.run(WebFetchArgs(url="https://example.com", max_length=8000), ctx)
    assert not res2.is_error
    assert res2.metadata["from_cache"] is True
    assert "fresh data" in res2.output


async def test_web_fetch_extracts_visible_html_and_drops_page_code(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    tool = WebFetchTool()
    html = """
    <!doctype html><html><head><title>Release notes</title>
    <style>.hidden { display: none }</style><script>dangerousCall()</script></head>
    <body><main><h1>Version 0813</h1><p>The GA release is available.</p>
    <ul><li>API enabled</li><li>Weights pending</li></ul></main></body></html>
    """
    async with respx.mock() as mock:
        mock.get("https://example.com/releases").respond(
            200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
        )
        result = await tool.run(
            WebFetchArgs(url="https://example.com/releases"),
            ctx,
        )

    assert "Version 0813" in result.output
    assert "The GA release is available." in result.output
    assert "API enabled" in result.output
    assert "<html>" not in result.output
    assert "dangerousCall" not in result.output
    assert "display: none" not in result.output


async def test_web_fetch_flags_client_rendered_html_shell(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    tool = WebFetchTool()
    shell = (
        "<html><body><div id='app'></div><script>"
        + ("x" * 2_000)
        + "</script></body></html>"
    )
    async with respx.mock() as mock:
        mock.get("https://example.com/app").respond(
            200,
            text=shell,
            headers={"content-type": "text/html"},
        )
        result = await tool.run(WebFetchArgs(url="https://example.com/app"), ctx)

    assert "Readable page text is sparse" in result.output
    assert "Use the browser tool" in result.output
