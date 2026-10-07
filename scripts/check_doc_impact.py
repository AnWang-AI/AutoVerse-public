#!/usr/bin/env python3
"""Check whether code changes handled documentation impact.

The checker reads docs/doc-impact.yml, finds changed files, and reports the
documentation areas that must be considered before committing. It intentionally
depends only on the standard library and supports the small YAML subset used by
the impact map.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "docs" / "doc-impact.yml"
DOC_PREFIXES = ("docs/", "README.md", "AGENTS.md")


@dataclass(frozen=True)
class Area:
    name: str
    code: tuple[str, ...]
    required_docs: tuple[str, ...]
    conditional_docs: tuple[str, ...]
    tests: tuple[str, ...]


def _run_git(args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(args)} failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_files(base: str, *, staged: bool = False) -> set[str]:
    if staged:
        changed = set(_run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB", "--"]))
        return {path for path in changed if path}

    changed = set(_run_git(["diff", "--name-only", "--diff-filter=ACMRTUXB", base, "--"]))
    changed.update(_run_git(["ls-files", "--others", "--exclude-standard"]))
    return {path for path in changed if path}


def parse_impact_map(path: Path = MAP_PATH) -> tuple[Area, ...]:
    areas: list[Area] = []
    current_area: str | None = None
    current_key: str | None = None
    data: dict[str, dict[str, list[str]]] = {}

    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped == "areas:":
            continue
        if indent == 2 and stripped.endswith(":"):
            current_area = stripped[:-1]
            current_key = None
            data[current_area] = {}
            continue
        if indent == 4 and stripped.endswith(":"):
            if current_area is None:
                raise ValueError(f"{path}:{lineno}: key outside area")
            current_key = stripped[:-1]
            data[current_area][current_key] = []
            continue
        if indent == 6 and stripped.startswith("- "):
            if current_area is None or current_key is None:
                raise ValueError(f"{path}:{lineno}: list item outside key")
            data[current_area][current_key].append(stripped[2:].strip())
            continue
        raise ValueError(f"{path}:{lineno}: unsupported YAML subset: {raw_line!r}")

    for name, values in data.items():
        areas.append(
            Area(
                name=name,
                code=tuple(values.get("code", ())),
                required_docs=tuple(values.get("required_docs", ())),
                conditional_docs=tuple(values.get("conditional_docs", ())),
                tests=tuple(values.get("tests", ())),
            )
        )
    return tuple(areas)


def matches_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-3].rstrip("/") + "/")
    return fnmatch.fnmatchcase(path, pattern)


def is_doc_path(path: str) -> bool:
    return path.startswith(DOC_PREFIXES)


def existing_paths(paths: Iterable[str]) -> list[str]:
    missing = [path for path in paths if not (ROOT / path).exists()]
    return missing


def impacted_areas(files: set[str], areas: Iterable[Area]) -> list[Area]:
    impacted: list[Area] = []
    for area in areas:
        if any(matches_pattern(path, pattern) for path in files for pattern in area.code):
            impacted.append(area)
    return impacted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Git revision to diff against. Defaults to HEAD for pre-commit use.",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Check only staged changes. Useful in a dirty worktree before committing.",
    )
    parser.add_argument(
        "--allow-none-reason",
        default="",
        help="Explicit reason when impacted docs do not need changes.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when code changes map to docs but no mapped docs changed.",
    )
    args = parser.parse_args(argv)

    areas = parse_impact_map()
    missing = existing_paths(
        path
        for area in areas
        for path in (*area.required_docs, *area.conditional_docs, *area.tests)
    )
    if missing:
        print("docs/doc-impact.yml references missing paths:", file=sys.stderr)
        for path in missing:
            print(f"- {path}", file=sys.stderr)
        return 2

    files = changed_files(args.base, staged=args.staged)
    impacted = impacted_areas(files, areas)
    if not impacted:
        print("Documentation impact: no mapped code areas changed.")
        return 0

    changed_docs = {path for path in files if is_doc_path(path)}
    mapped_docs = {
        doc
        for area in impacted
        for doc in (*area.required_docs, *area.conditional_docs)
    }
    touched_mapped_docs = sorted(changed_docs & mapped_docs)

    print("Documentation impact areas:")
    for area in impacted:
        print(f"- {area.name}")
        print("  required docs:")
        for doc in area.required_docs:
            marker = "changed" if doc in changed_docs else "check"
            print(f"  - {doc} ({marker})")
        if area.conditional_docs:
            print("  conditional docs:")
            for doc in area.conditional_docs:
                marker = "changed" if doc in changed_docs else "consider"
                print(f"  - {doc} ({marker})")

    if touched_mapped_docs:
        print("Documentation impact: mapped documentation changed.")
        return 0

    if args.allow_none_reason.strip():
        print(f"Documentation impact explicitly waived: {args.allow_none_reason.strip()}")
        return 0

    message = (
        "Documentation impact unresolved: mapped code changed but no mapped docs "
        "changed. Update the relevant docs or rerun with --allow-none-reason "
        "for an internal-only change."
    )
    if args.strict:
        print(message, file=sys.stderr)
        return 1
    print(f"WARNING: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
