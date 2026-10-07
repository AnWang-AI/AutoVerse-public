"""Unit: pathresolve resolve_path + is_within (T11)."""

from __future__ import annotations

from pathlib import Path

from autoverse.core.tools.pathresolve import (
    is_within,
    resolve_in_workspace,
    resolve_path,
)


def test_absolute_passthrough() -> None:
    assert resolve_path(Path("/work"), "/etc/passwd") == Path("/etc/passwd")


def test_relative_resolved_against_cwd() -> None:
    assert resolve_path(Path("/work"), "sub/file.txt") == Path("/work/sub/file.txt")


def test_relative_traversal_normalized() -> None:
    # `..` segments are collapsed so escapes are detectable lexically.
    assert resolve_path(Path("/work"), "sub/../x.txt") == Path("/work/x.txt")
    assert resolve_path(Path("/work"), "../../etc/passwd") == Path("/etc/passwd")


def test_resolve_in_workspace_blocks_relative_escape() -> None:
    resolved, escaped = resolve_in_workspace(Path("/work"), "../../etc/passwd")
    assert escaped is True
    assert resolved == Path("/etc/passwd")


def test_resolve_in_workspace_allows_inside() -> None:
    resolved, escaped = resolve_in_workspace(Path("/work"), "sub/file.txt")
    assert escaped is False and resolved == Path("/work/sub/file.txt")


def test_resolve_in_workspace_absolute_not_flagged() -> None:
    # Absolute paths legitimately escape the workspace (docs/reference/tools.md §9.2); they are
    # gated by capability tiers/permissions, not by the path resolver.
    resolved, escaped = resolve_in_workspace(Path("/work"), "/etc/passwd")
    assert escaped is False and resolved == Path("/etc/passwd")


def test_t11_is_within_true_for_descendant() -> None:
    assert is_within(Path("/work"), Path("/work/sub/x.txt")) is True
    assert is_within(Path("/work"), Path("/work")) is True


def test_t11_is_within_false_for_escape() -> None:
    assert is_within(Path("/work"), Path("/etc/passwd")) is False
    assert is_within(Path("/work"), Path("/work/../other")) is False
