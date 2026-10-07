"""Acceptance tests for EngineBuilder production tool wiring.

These assert *production wiring*: not that the dispatcher
works in isolation (covered by unit tests) but that ``EngineBuilder.build()``
wires a real ToolRegistry + ToolDispatcher into the TurnLoop, so a tool call
from the model is truly executed, gated by permissions, and the derived tool
schemas reach ``LLMRequest.tools``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.infra.permission.fixed import (
    AllowAllPermissionAdapter,
    NonInteractivePermissionAdapter,
)
from autoverse.infra.persist import TaskStore
from autoverse.types import (
    TextDeltaPayload,
    ToolStartPayload,
    TurnEndPayload,
    Usage,
    UsagePayload,
    make_event,
)
from autoverse.types.messages import Message, ToolResultBlock

from tests._doubles import ScriptedProvider


def _tool_turn(tid: str, name: str, inp: dict[str, Any]) -> list[Any]:
    return [
        make_event(ToolStartPayload(tool_use_id=tid, tool_name=name, tool_input=inp)),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="TOOL_USE")),
    ]


def _text_turn(text: str) -> list[Any]:
    return [
        make_event(TextDeltaPayload(text=text)),
        make_event(UsagePayload(usage=Usage(input_tokens=1, output_tokens=1), model="m")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]


async def _drain(engine: Any, prompt: str) -> None:
    async for _ in engine.run(prompt):
        pass


def _last_tool_result(conv: Any) -> ToolResultBlock:
    for msg in reversed(conv.view().messages):
        if isinstance(msg, Message) and msg.role == "user":
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    return block
    raise AssertionError("no tool-result block found in conversation")


# ── gap7-A1: a read_file tool call is really executed (not echoed) ───────────
async def test_engine_executes_real_tool(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("REAL FILE CONTENT\n", encoding="utf-8")

    provider = ScriptedProvider(
        [
            _tool_turn("t1", "read_file", {"file_path": str(target)}),
            _text_turn("done"),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            llm_provider=provider,
            permission_engine=AllowAllPermissionAdapter(),
        )
    ).build()
    await _drain(engine, "read it")

    block = _last_tool_result(engine._conversation)
    assert not block.is_error
    assert "REAL FILE CONTENT" in block.content  # real read, not "ran read_file"


# ── gap7-A1: derived tool schemas reach LLMRequest.tools (non-empty) ─────────
async def test_request_tools_nonempty(tmp_path: Path) -> None:
    provider = ScriptedProvider([_text_turn("hi")])
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "hello")

    assert provider.requests, "provider was never called"
    names = {t.name for t in provider.requests[0].tools}
    assert {"read_file", "bash", "grep"} <= names


async def test_frontend_owned_browser_port_overrides_default_backend(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([_text_turn("hi")])
    frontend_browser = cast(Any, object())

    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            llm_provider=provider,
            browser=frontend_browser,
        )
    ).build()

    assert engine.app.browser is frontend_browser


async def test_engine_shutdown_keeps_native_host_and_closes_retired_backend(
    tmp_path: Path,
) -> None:
    class CloseTracker:
        close_on_engine_shutdown = False

        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    provider = ScriptedProvider([_text_turn("hi")])
    native = CloseTracker()
    retired = CloseTracker()
    retired.close_on_engine_shutdown = True
    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            llm_provider=provider,
            browser=cast(Any, native),
        )
    ).build()
    engine.app.extra["retired_browser_backends"] = [retired]

    await engine.aclose("test")

    assert native.closed is False
    assert retired.closed is True


async def test_gap12_parity_tools_are_registered_in_request(tmp_path: Path) -> None:
    provider = ScriptedProvider([_text_turn("hi")])
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "hello")

    names = {t.name for t in provider.requests[0].tools}
    assert {
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "memory_read",
        "memory_write",
        "SessionSpawn",
        "SessionTell",
        "SendMessage",
        "SessionInbox",
        "SessionList",
        "SessionAbort",
        "SessionTaskUpdate",
    } <= names


async def test_gap12_memory_tool_executes_through_engine(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(
                "t1",
                "memory_write",
                {"agent_type": "main", "content": "remember this", "append": False},
            ),
            _tool_turn("t2", "memory_read", {"agent_type": "main"}),
            _text_turn("done"),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "memory")

    block = _last_tool_result(engine._conversation)
    assert not block.is_error
    assert "remember this" in block.content


async def test_gap12_plan_tools_execute_through_engine(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn("t1", "EnterPlanMode", {"reason": "large change"}),
            _tool_turn("t2", "ExitPlanMode", {"plan": "1. inspect\n2. edit"}),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "plan")

    plan_mode = cast(dict[str, Any], engine.app.extra["plan_mode"])
    assert plan_mode["active"] is False
    assert plan_mode["pending_approval"] is True
    block = _last_tool_result(engine._conversation)
    assert "waiting for user approval" in block.content
    assert len(provider.requests) == 2


async def test_ask_user_unavailable_is_explicit(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(
                "t1",
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "id": "q1",
                            "prompt": "Choose",
                            "options": [
                                {"id": "a", "label": "A"},
                                {"id": "b", "label": "B"},
                            ],
                        }
                    ]
                },
            ),
            _text_turn("done"),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "ask")

    block = _last_tool_result(engine._conversation)
    assert block.is_error
    assert "No interactive question UI" in block.content


async def test_session_tools_delegate_to_session_ops(tmp_path: Path) -> None:
    class SessionOps:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def tell(self, **payload: Any) -> str:
            self.calls.append(("tell", payload))
            return f"sent:{payload['session_id']}:{payload['message']}"

    ops = SessionOps()
    provider = ScriptedProvider(
        [
            _tool_turn("t1", "SessionTell", {"session_id": "s1", "message": "hello"}),
            _tool_turn("t2", "SendMessage", {"session_id": "s2", "message": "compat"}),
            _text_turn("done"),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            llm_provider=provider,
            permission_engine=AllowAllPermissionAdapter(),
            session_ops=ops,
        )
    ).build()
    await _drain(engine, "sessions")

    assert ops.calls == [
        ("tell", {"session_id": "s1", "message": "hello"}),
        ("tell", {"session_id": "s2", "message": "compat"}),
    ]
    block = _last_tool_result(engine._conversation)
    assert not block.is_error
    assert "sent:s2:compat" in block.content


async def test_todo_write_uses_task_store_seen_by_completion_gate(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _tool_turn(
                "t1",
                "todo_write",
                {
                    "todos": [
                        {
                            "id": "done",
                            "content": "already done",
                            "status": "completed",
                            "priority": "medium",
                        }
                    ]
                },
            ),
            _text_turn("done"),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            llm_provider=provider,
            permission_engine=AllowAllPermissionAdapter(),
        )
    ).build()
    await _drain(engine, "todos")

    session_id = cast(str, engine.app.extra["session_id"])
    tasks = cast(TaskStore, engine.app.tasks)
    items = await tasks.list(session_id)
    assert [i.id for i in items] == ["done"]
    assert tasks.has_pending(session_id) is False


# ── gap7-A7: a destructive tool is blocked by the permission gate ────────────
async def test_permission_gate_blocks_destructive(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")

    provider = ScriptedProvider(
        [
            _tool_turn("t1", "delete_file", {"path": str(victim)}),
            _text_turn("done"),
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            llm_provider=provider,
            permission_engine=NonInteractivePermissionAdapter(),
        )
    ).build()
    await _drain(engine, "delete it")

    block = _last_tool_result(engine._conversation)
    assert block.is_error
    assert "Permission denied" in block.content
    assert victim.exists()  # the gate prevented the deletion
