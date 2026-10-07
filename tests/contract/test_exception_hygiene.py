"""Exception-handling hygiene contracts for broad catch blocks.

Broad exception handling is sometimes intentional in an agent runtime, but it
must be justified where it appears so future edits do not hide real failures by
accident.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BROAD_EXCEPT_RE = re.compile(r"except (?:Exception|BaseException)(?: as [^:]+)?:")


def test_broad_exception_handlers_carry_inline_rationale() -> None:
    offenders: list[str] = []
    for path in (ROOT / "src" / "autoverse").rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if BROAD_EXCEPT_RE.search(line) and "#" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
