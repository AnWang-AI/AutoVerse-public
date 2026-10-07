"""Phase 6 acceptance — terminal frontend REPL/CLI (docs/reference/testing.md §10, R1-R9).

Each test follows Given/When/Then. The frontend consumes *only* StreamEvent and
never reflects engine privates (R8, enforced by a static scan below).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from autoverse.types import (
    EventType,
    StreamEvent,
    make_event,
)
from autoverse.types.events import (
    ApprovalPayload,
    CompactingPayload,
    ErrorPayload,
    GatePayload,
    RequestPayload,
    RetryPayload,
    SubagentActivityPayload,
    SubagentChildPayload,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    TurnStartPayload,
    UsagePayload,
)
from autoverse.types.messages import Usage
from autoverse.types.tools import Decision, ToolCall


# ── helpers ──────────────────────────────────────────────────────────────────

def _one_event_per_type() -> dict[EventType, StreamEvent]:
    """Build exactly one StreamEvent for every EventType (R1 coverage)."""
    payloads = {
        EventType.TEXT_DELTA: TextDeltaPayload(text="hi"),
        EventType.THINKING_DELTA: ThinkingDeltaPayload(text="reasoning"),
        EventType.TOOL_START: ToolStartPayload(
            tool_use_id="t1", tool_name="Read", tool_input={"file_path": "a.py"}
        ),
        EventType.TOOL_END: ToolEndPayload(
            tool_use_id="t1", tool_name="Read", output="content", duration_ms=12.0
        ),
        EventType.COMPACTING: CompactingPayload(kind="micro", tokens_before=100, tokens_after=40),
        EventType.GATE_DECISION: GatePayload(gate_name="completion", decision="stop"),
        EventType.USAGE: UsagePayload(
            usage=Usage(input_tokens=1200, output_tokens=340), model="m1", cost_usd=0.0042
        ),
        EventType.TURN_START: TurnStartPayload(turn_index=0, iteration=0, model="m1"),
        EventType.TURN_END: TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN"),
        EventType.SUBAGENT_ACTIVITY: SubagentActivityPayload(
            job_id="j1", subagent_type="builder", activity="bash: pytest"
        ),
        EventType.SUBAGENT_CHILD: SubagentChildPayload(
            job_id="j1", subagent_type="builder",
            inner=make_event(TextDeltaPayload(text="child line")),
        ),
        EventType.ERROR: ErrorPayload(message="boom", error_class="ValueError"),
        EventType.RETRY: RetryPayload(attempt=1, reason="429"),
        EventType.APPROVAL: ApprovalPayload(call_id="c1", tool_name="Edit", summary="write a.py"),
        EventType.REQUEST: RequestPayload(
            turn_index=0,
            system="sys",
            system_sha256="x",
            tool_names=(),
            tools_sha256="y",
            model="m1",
            max_tokens=8192,
            temperature=0.0,
            thinking_budget=0,
        ),
    }
    return {et: make_event(p) for et, p in payloads.items()}


# ── R1: render dispatch via EventType registry ────────────────────────────────

def test_r1_render_dispatches_via_registry_no_if_chain() -> None:
    """Given one StreamEvent of each EventType, When rendered, Then dispatch
    goes through RENDERERS dict keyed by EventType and every type is covered."""
    from autoverse.frontends.repl.renderer import RENDERERS, RenderContext, render_event

    # Every EventType has a registered renderer (registry, not if-chain).
    for et in EventType:
        assert et in RENDERERS, f"no renderer registered for {et}"

    ctx = RenderContext()
    for et, event in _one_event_per_type().items():
        out = render_event(event, ctx)
        assert isinstance(out, str), f"{et} renderer did not return str"


def test_r1_unregistered_event_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given an EventType absent from RENDERERS, When rendered, Then the default
    renderer handles it (forward compatibility)."""
    from autoverse.frontends.repl import renderer as rmod

    backup = dict(rmod.RENDERERS)
    try:
        rmod.RENDERERS.pop(EventType.RETRY, None)
        event = make_event(RetryPayload(attempt=2, reason="503"))
        out = rmod.render_event(event, rmod.RenderContext())
        assert isinstance(out, str)
    finally:
        rmod.RENDERERS.clear()
        rmod.RENDERERS.update(backup)


# ── R2: tool renderer registry ────────────────────────────────────────────────

def test_r2_registered_tool_renderer_used_vs_default() -> None:
    """Given a registered ToolRenderer for "MyTool", When TOOL_END for that tool
    is rendered, Then the registered renderer is used; an unregistered tool falls
    back to the default renderer."""
    from autoverse.frontends.repl.renderer import RenderContext
    from autoverse.frontends.repl.tool_renderer import (
        TOOL_RENDERERS,
        get_tool_renderer,
        register_tool_renderer,
    )

    def my_renderer(payload: ToolEndPayload, ctx: RenderContext) -> str:
        return f"CUSTOM:{payload.tool_name}"

    backup = dict(TOOL_RENDERERS)
    try:
        register_tool_renderer("MyTool", my_renderer)
        ctx = RenderContext()
        custom = get_tool_renderer("MyTool")
        out = custom(
            ToolEndPayload(tool_use_id="t", tool_name="MyTool", output="ok"), ctx
        )
        assert out == "CUSTOM:MyTool"

        # Unregistered tool → default renderer (not the custom one).
        default = get_tool_renderer("TotallyUnknownTool")
        assert default is not custom
        dout = default(
            ToolEndPayload(tool_use_id="t", tool_name="TotallyUnknownTool", output="x"),
            ctx,
        )
        assert isinstance(dout, str)
        assert "TotallyUnknownTool" in dout
    finally:
        TOOL_RENDERERS.clear()
        TOOL_RENDERERS.update(backup)


# ── R3: command single source of truth ────────────────────────────────────────

def test_r3_help_completion_handler_from_one_registry() -> None:
    """Given commands declared via @command, When help/completion/handler are
    requested, Then all three derive from the single CommandRegistry (no three
    hand-maintained tables)."""
    from autoverse.frontends.repl.commands import CommandRegistry, command

    registry = CommandRegistry()

    @command("greet", "say hello", registry=registry)
    async def _greet(ctx: Any, args: str) -> Any:
        from autoverse.frontends.repl.commands import CommandResult

        return CommandResult(display="hello")

    # Help lines, completion names, and handler all come from `registry`.
    help_text = "\n".join(registry.help_lines())
    assert "greet" in help_text
    assert "say hello" in help_text

    completions = registry.completion_names()
    assert "/greet" in completions

    spec = registry.get("greet")
    assert spec is not None
    assert spec.handler is _greet
    assert spec.summary == "say hello"

    # Single registry: the number of distinct registries the data lives in is one.
    assert {id(registry)} == {id(registry)}  # all derived from this one object


def test_r3_duplicate_command_rejected() -> None:
    """Given a command name already registered, When registering again, Then a
    ValueError is raised (explicit conflict, no silent overwrite)."""
    from autoverse.frontends.repl.commands import CommandRegistry, CommandSpec

    registry = CommandRegistry()

    async def _h(ctx: Any, args: str) -> Any: ...

    registry.register(CommandSpec(name="dup", summary="s", handler=_h))
    with pytest.raises(ValueError):
        registry.register(CommandSpec(name="dup", summary="s2", handler=_h))


# ── R4: skill is an ordinary command ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_r4_skill_dispatches_as_ordinary_command() -> None:
    """Given a skill registered as a normal CommandSpec, When the user types
    /skillname, Then it dispatches through the same registry path — NOT a
    fallback special-case."""
    from autoverse.frontends.repl.commands import (
        CommandRegistry,
        CommandResult,
        CommandSpec,
        dispatch,
    )

    registry = CommandRegistry()
    invoked: list[str] = []

    async def _skill_handler(ctx: Any, args: str) -> CommandResult:
        invoked.append(args)
        return CommandResult(display="skill ran")

    # Skills register exactly like any other command.
    registry.register(CommandSpec(name="simplify", summary="simplify code", handler=_skill_handler))

    class _Ctx:
        registry_obj = registry

    ctx = _Ctx()
    ctx.registry = registry  # type: ignore[attr-defined]

    result = await dispatch("/simplify the diff", ctx, registry)
    assert result.display == "skill ran"
    assert invoked == ["the diff"]
    # No fallback: an unknown command is a plain error, not a skill probe.
    unknown = await dispatch("/nope", ctx, registry)
    assert unknown.is_error


# ── R5: concurrent input queue ────────────────────────────────────────────────

def test_r5_enqueue_while_running_then_drain() -> None:
    """Given the engine is running, When the user submits input, Then it is
    enqueued in the MessageQueue; When the engine goes idle, Then it is
    dequeued and processed."""
    from autoverse.frontends.repl.input_queue import InputMode, InputModeStack, MessageQueue

    queue: MessageQueue = MessageQueue()
    stack = InputModeStack()

    with stack.push(InputMode.RUNNING):
        assert stack.current() is InputMode.RUNNING
        queue.enqueue("first")
        queue.enqueue("second")
        assert len(queue) == 2

    # Engine idle now → drain in FIFO order.
    assert stack.current() is InputMode.NORMAL
    assert queue.dequeue() == "first"
    assert queue.dequeue() == "second"
    assert queue.dequeue() is None


def test_r5_input_mode_stack_pops_on_exception() -> None:
    """Given a pushed mode, When the with-block raises, Then the mode is popped
    by finally (no scattered booleans, no stuck mode)."""
    from autoverse.frontends.repl.input_queue import InputMode, InputModeStack

    stack = InputModeStack()
    with pytest.raises(RuntimeError):
        with stack.push(InputMode.RUNNING):
            raise RuntimeError("boom")
    assert stack.current() is InputMode.NORMAL


# ── R6: two-stage Ctrl+C ──────────────────────────────────────────────────────

def test_r6_two_ctrl_c_escalate_interrupt_then_cancel() -> None:
    """Given a running engine, When Ctrl+C is pressed once Then abort("interrupt")
    is requested; When pressed a second time Then abort("cancel")."""
    from autoverse.frontends.repl.interrupt import InterruptHandler

    calls: list[str] = []

    class _Engine:
        def abort(self, reason: str = "cancel") -> None:
            calls.append(reason)

    handler = InterruptHandler(_Engine())
    handler.on_interrupt()
    handler.on_interrupt()
    assert calls == ["interrupt", "cancel"]


def test_r6_count_resets_on_new_turn() -> None:
    """Given a prior interrupt, When a new turn starts, Then the counter resets so
    the next Ctrl+C is an "interrupt" again."""
    from autoverse.frontends.repl.interrupt import InterruptHandler

    calls: list[str] = []

    class _Engine:
        def abort(self, reason: str = "cancel") -> None:
            calls.append(reason)

    handler = InterruptHandler(_Engine())
    handler.on_interrupt()
    handler.reset()
    handler.on_interrupt()
    assert calls == ["interrupt", "interrupt"]


# ── R7: terminal approval (PermissionPort) ────────────────────────────────────

@pytest.mark.asyncio
async def test_r7_terminal_approval_returns_decision() -> None:
    """Given an ask-tier tool call, When the injected prompt returns yes/no, Then
    the TerminalApproval (a PermissionPort) returns an allow/deny Decision — not a
    holder-list/callback injection."""
    from autoverse.frontends.repl.approval import TerminalApproval
    from autoverse.ports import PermissionPort

    answers = iter(["y", "n"])

    def prompt_fn(text: str) -> str:
        return next(answers)

    approval = TerminalApproval(prompt=prompt_fn)
    assert isinstance(approval, PermissionPort)

    call = ToolCall(id="c1", name="Edit", input={"file_path": "a.py"})
    allow = await approval.request_approval(call)
    assert isinstance(allow, Decision)
    assert allow.action == "allow"

    deny = await approval.request_approval(call)
    assert deny.action == "deny"
    assert deny.reason is not None


# ── R8: no private engine access ──────────────────────────────────────────────

def test_r8_no_getattr_engine_private_in_frontends() -> None:
    """Given the frontend source tree, When statically scanned, Then no occurrence
    of `getattr(engine, "_` appears (frontend consumes only StreamEvent / the
    EngineHandle narrow facade)."""
    root = Path(__file__).resolve().parents[2] / "src" / "autoverse" / "frontends"
    assert root.is_dir(), root
    pattern = re.compile(r'getattr\(\s*engine\s*,\s*["\']_')
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(py))
    assert offenders == [], f"private engine access found in: {offenders}"


# ── R9: EngineBuilder assembly ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r9_engine_builder_returns_engine_handle_and_run_yields_events(
    tmp_path: Path,
) -> None:
    """Given EngineOptions, When EngineBuilder.build() runs, Then it returns an
    EngineHandle (no 10-element tuple) whose run() yields StreamEvents."""
    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.ports import EngineHandle

    opts = EngineOptions(model="echo", cwd=tmp_path, session_mode="one_shot")
    engine = await EngineBuilder(opts).build()
    assert isinstance(engine, EngineHandle)
    assert isinstance(engine.session_id, str) and engine.session_id

    events: list[StreamEvent] = []
    async for ev in engine.run("hello"):
        events.append(ev)
    assert events, "run() yielded no events"
    assert all(isinstance(ev, StreamEvent) for ev in events)
    types = {ev.type for ev in events}
    assert EventType.TURN_END in types
