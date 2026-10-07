"""Static regression checks for the web mobile composer chrome."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = ROOT / "src/autoverse/frontends/web/static/index.html"


def test_mobile_composer_uses_single_left_plus_for_images() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert ".cmp-btn.queue {\n    display: none !important;" in html
    assert ".composer.busy .cmp-btn.queue { display: none !important; }" in html
    assert '.cmp-btn.image::after { content: "＋";' in html
