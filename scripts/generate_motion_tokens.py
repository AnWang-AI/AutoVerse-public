"""Generate Web and Swift motion constants from the shared token source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "design-tokens" / "motion.json"
CSS = ROOT / "src" / "autoverse" / "frontends" / "web" / "static" / "motion-tokens.css"
SWIFT = ROOT / "apple" / "Sources" / "Shared" / "MotionTokenValues.swift"


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def _curve(values: list[float]) -> str:
    rendered: list[str] = []
    for value in values:
        component = f"{value:.2f}".rstrip("0").rstrip(".")
        rendered.append(component if "." in component else f"{component}.0")
    return ", ".join(rendered)


def render() -> dict[Path, str]:
    """Render every generated target from the canonical JSON source."""
    tokens = json.loads(SOURCE.read_text(encoding="utf-8"))
    curve = tokens["panel"]["curve"]
    css = f"""/* Generated from design-tokens/motion.json by scripts/generate_motion_tokens.py. */
:root {{
  --motion-press: {tokens['press']['duration_ms']}ms;
  --motion-hover: {tokens['hover']['duration_ms']}ms;
  --motion-state: {tokens['state']['duration_ms']}ms;
  --motion-panel: {tokens['panel']['duration_ms']}ms;
  --motion-activity: {tokens['activity']['duration_ms']}ms;
  --motion-ease: cubic-bezier({curve[0]:g}, {curve[1]:g}, {curve[2]:g}, {curve[3]:g});
}}
"""
    swift = f"""// Generated from design-tokens/motion.json by scripts/generate_motion_tokens.py.
import Foundation

enum MotionTokenValues {{
    static let pressDuration = {_seconds(tokens['press']['duration_ms'])}
    static let hoverDuration = {_seconds(tokens['hover']['duration_ms'])}
    static let stateDuration = {_seconds(tokens['state']['duration_ms'])}
    static let panelDuration = {_seconds(tokens['panel']['duration_ms'])}
    static let activityDuration = {_seconds(tokens['activity']['duration_ms'])}
    static let stateCurve = ({_curve(tokens['state']['curve'])})
    static let panelCurve = ({_curve(tokens['panel']['curve'])})
    static let activityCurve = ({_curve(tokens['activity']['curve'])})
}}
"""
    return {CSS: css, SWIFT: swift}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale: list[str] = []
    for path, content in render().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        parser.error("generated motion tokens are stale: " + ", ".join(stale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "render"]
