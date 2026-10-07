"""Unit: TUI permission adapter policy fast-paths."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from autoverse.frontends.repl.permission_adapter import TuiPermissionAdapter  # noqa: E402
from autoverse.types.tools import Decision, ToolCall  # noqa: E402


class _FakeApp:
    def __init__(self, *decisions: Decision) -> None:
        self.decisions = list(decisions)
        self.calls: list[ToolCall] = []

    async def request_permission(
        self, call: ToolCall, *, session_id: str = ""
    ) -> Decision:
        self.calls.append(call)
        return self.decisions.pop(0)


async def test_tui_permission_allows_safe_calls_without_modal() -> None:
    app = _FakeApp()
    perms = TuiPermissionAdapter(app)  # type: ignore[arg-type]

    decision = await perms.check(ToolCall("1", "read_file", {"file_path": "/tmp/x"}))

    assert decision.allowed
    assert app.calls == []


async def test_tui_permission_allows_todo_write_without_modal() -> None:
    app = _FakeApp()
    perms = TuiPermissionAdapter(app)  # type: ignore[arg-type]

    decision = await perms.check(
        ToolCall(
            "1",
            "todo_write",
            {"todos": [{"id": "t1", "content": "check permissions", "status": "pending"}]},
        )
    )

    assert decision.allowed
    assert app.calls == []


async def test_tui_permission_allows_write_file_without_modal(tmp_path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    app = _FakeApp()
    perms = TuiPermissionAdapter(app)  # type: ignore[arg-type]

    decision = await perms.check(
        ToolCall(
            "1",
            "write_file",
            {"file_path": str(target), "content": "new"},
        )
    )

    assert decision.allowed
    assert app.calls == []


async def test_tui_permission_prompts_for_irreversible_call() -> None:
    app = _FakeApp(Decision.deny("no"))
    perms = TuiPermissionAdapter(app)  # type: ignore[arg-type]

    decision = await perms.check(ToolCall("1", "delete_file", {"path": "/tmp/x"}))

    assert not decision.allowed
    assert [call.name for call in app.calls] == ["delete_file"]


async def test_tui_permission_always_allow_installs_session_rule() -> None:
    app = _FakeApp(Decision.allow(rule="user-always"))
    perms = TuiPermissionAdapter(app)  # type: ignore[arg-type]

    first = await perms.check(ToolCall("1", "delete_file", {"path": "/tmp/a"}))
    second = await perms.check(ToolCall("2", "delete_file", {"path": "/tmp/b"}))

    assert first.allowed
    assert second.allowed
    assert len(app.calls) == 1
