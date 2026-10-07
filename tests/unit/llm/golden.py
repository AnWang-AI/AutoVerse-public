"""Golden-sample loader for codec round-trip tests."""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"


def load_jsonl(wire: str, name: str) -> list[dict[str, object]]:
    """Load a recorded wire chunk stream, skipping the leading _comment line."""
    path = _FIXTURES / wire / name
    chunks: list[dict[str, object]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "_comment" in obj:
            continue
        chunks.append(obj)
    return chunks


def load_json(wire: str, name: str) -> object:
    path = _FIXTURES / wire / name
    return json.loads(path.read_text())


__all__ = ["load_jsonl", "load_json"]
