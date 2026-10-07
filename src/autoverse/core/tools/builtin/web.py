"""Builtin web tools (docs/reference/tools.md). WebFetchTool + WebSearchTool.

Both are READ_ONLY + needs_network=True. WebFetch caches with a TTL+LRU and
reports `from_cache` in metadata. WebSearch prefers Tavily, falling back to a
DuckDuckGo-style result, normalising each to {title, url, snippet}. Network
access here is best-effort; failures are returned as tool errors, never raised.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from html.parser import HTMLParser
from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.core.tools.url_guard import check_url
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

_CACHE_TTL_S = 900.0
_CACHE_MAX = 64
_HTML_BLOCK_TAGS = frozenset(
    {
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_HTML_IGNORED_TAGS = frozenset({"canvas", "noscript", "script", "style", "svg"})


class _ReadableHTMLParser(HTMLParser):
    """Small dependency-free HTML-to-visible-text projection."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag in _HTML_IGNORED_TAGS:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif not self._ignored_depth and tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


class _TTLCache:
    def __init__(self, ttl: float = _CACHE_TTL_S, maxsize: int = _CACHE_MAX) -> None:
        self._ttl = ttl
        self._max = maxsize
        self._data: "OrderedDict[str, tuple[float, str]]" = OrderedDict()

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        ts, val = item
        if time.monotonic() - ts > self._ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return val

    def put(self, key: str, value: str) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


class WebFetchArgs(BaseModel):
    url: str = Field(..., description="URL to fetch.")
    max_length: int = Field(8000, description="Max characters to return.")
    no_cache: bool = Field(False, description="Skip cache read (still writes to cache).")


@register_tool
class WebFetchTool(BaseTool):
    name: ClassVar[str] = "web_fetch"
    description: ClassVar[str] = "Fetch a URL and return its text content."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = WebFetchArgs

    def __init__(self) -> None:
        self._cache = _TTLCache()

    async def run(self, args: WebFetchArgs, ctx: ToolContext) -> ToolResult:
        ok, reason = check_url(args.url)
        if not ok:
            return ToolResult.error(
                f"Refusing to fetch {args.url}: {reason}",
                error_kind="blocked_url",
            )
        cache_key = f"{args.url}:{args.max_length}"
        if not args.no_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return ToolResult.ok(
                    cached[: args.max_length], from_cache=True, url=args.url
                )
        try:
            import httpx

            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                resp = await client.get(args.url)
                resp.raise_for_status()
                content_type = str(
                    getattr(resp, "headers", {}).get("content-type", "")
                )
                text = _readable_content(resp.text, content_type=content_type)
        except Exception as exc:  # noqa: BLE001 — network failures become feedback
            return ToolResult.error(
                f"Failed to fetch {args.url}: {type(exc).__name__}: {exc}",
                error_kind="fetch_failed",
            )
        self._cache.put(cache_key, text)
        return ToolResult.ok(
            text[: args.max_length], from_cache=False, url=args.url
        )


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="Search query.")
    max_results: int = Field(8, description="Maximum results.")


@register_tool
class WebSearchTool(BaseTool):
    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = "Search the web and return result snippets."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = WebSearchArgs

    async def run(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        try:
            results = await self._search(args, ctx)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(
                f"Search failed: {type(exc).__name__}: {exc}",
                error_kind="search_failed",
            )
        lines = [
            f"{i}. {r['title']}\n   {r['url']}"
            + (f"\n   Published: {r['published_at']}" if r["published_at"] else "")
            + f"\n   {r['snippet']}"
            for i, r in enumerate(results, 1)
        ]
        return ToolResult.ok("\n".join(lines), query=args.query, count=len(results))

    @staticmethod
    async def _search(args: WebSearchArgs, ctx: ToolContext) -> list[dict[str, str]]:
        import os

        import httpx

        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            secrets = getattr(ctx.app, "secrets", None)
            getter = getattr(secrets, "get", None) if secrets is not None else None
            if callable(getter):
                try:
                    api_key = getter("TAVILY_API_KEY") or None
                except Exception:  # noqa: BLE001
                    api_key = None
        async with httpx.AsyncClient(timeout=30) as client:
            if api_key:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": api_key,
                        "query": args.query,
                        "max_results": args.max_results,
                        "search_depth": "advanced",
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": _compact(str(r.get("content", ""))),
                        "published_at": str(
                            r.get("published_date") or r.get("published_at") or ""
                        ),
                    }
                    for r in data.get("results", [])[: args.max_results]
                ]
            # DuckDuckGo Instant Answer fallback.
            resp = await client.get(
                "https://duckduckgo.com/html/", params={"q": args.query}
            )
            resp.raise_for_status()
            return [
                {
                    "title": args.query,
                    "url": "",
                    "snippet": "(ddg fallback)",
                    "published_at": "",
                }
            ]


def _compact(value: str, limit: int = 500) -> str:
    return " ".join(value.split())[:limit]


def _readable_content(value: str, *, content_type: str) -> str:
    """Return readable page text while preserving non-HTML response bodies."""

    is_html = "html" in content_type.lower() or value.lstrip().lower().startswith(
        ("<!doctype html", "<html")
    )
    if not is_html:
        return value
    parser = _ReadableHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML still yields partial text
        pass
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    visible = "\n".join(line for line in lines if line)
    if len(visible) < 200 and len(value) > 1_000:
        diagnostic = (
            "[Readable page text is sparse; this may be a client-rendered shell. "
            "Use the browser tool to render the page before drawing conclusions.]"
        )
        return f"{visible}\n\n{diagnostic}" if visible else diagnostic
    return visible


__all__ = ["WebFetchArgs", "WebFetchTool", "WebSearchArgs", "WebSearchTool"]
