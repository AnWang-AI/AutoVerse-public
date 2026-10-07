"""Example AutoVerse hook that blocks writes to dotenv-style secret files."""

from __future__ import annotations

import json
import sys
from pathlib import PurePosixPath
from typing import Any


def decision_for_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Return a hook stdout object for a PreToolUse payload."""
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return {"decision": "proceed"}
    raw_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    normalized = raw_path.replace("\\", "/")
    name = PurePosixPath(normalized).name
    if name == ".env" or name.startswith(".env."):
        return {
            "decision": "deny",
            "reason": "secret files must not be edited by tools",
        }
    return {"decision": "proceed"}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    print(json.dumps(decision_for_payload(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["decision_for_payload", "main"]
