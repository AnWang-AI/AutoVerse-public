from __future__ import annotations

from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[3]


def test_generated_web_and_swift_motion_tokens_are_current() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "generate_motion_tokens.py"))
    render = namespace["render"]

    for path, expected in render().items():
        assert path.read_text(encoding="utf-8") == expected
