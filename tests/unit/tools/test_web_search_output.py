"""web_search returns compact, date-carrying result text."""

from __future__ import annotations

from autoverse.core.tools.builtin.web import WebSearchArgs, WebSearchTool, _compact

from tests.conftest import make_ctx


async def test_search_output_shows_url_and_publication_date(
    monkeypatch, tmp_path
) -> None:
    async def fake_search(args, ctx):
        return [
            {
                "title": "Model card",
                "url": "https://registry.example/models/x",
                "snippet": "bounded summary",
                "published_at": "2026-08-12",
            }
        ]

    monkeypatch.setattr(WebSearchTool, "_search", staticmethod(fake_search))
    result = await WebSearchTool().run(WebSearchArgs(query="model x"), make_ctx(tmp_path))

    assert not result.is_error
    assert "https://registry.example/models/x" in result.output
    assert "Published: 2026-08-12" in result.output
    assert "bounded summary" in result.output


def test_search_snippet_compaction_collapses_whitespace_and_caps_length() -> None:
    compacted = _compact(("evidence  \n\t" * 200), limit=120)
    assert "\n" not in compacted
    assert "\t" not in compacted
    assert len(compacted) == 120
