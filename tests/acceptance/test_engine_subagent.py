"""Acceptance tests for EngineBuilder production sub-agent wiring.

Proves production wiring: EngineBuilder.build() installs a
real engine_factory + SubAgentRunner, registers the `agent` + `JobOutput` shells
into the main registry, and wires app.spawn/app.jobs — so a model-issued agent()
call really spawns a child engine and its summary flows back as a tool result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.core.subagent.jobs import InMemoryJobStore
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.infra.tools.job_output import JobOutputArgs, JobOutputTool
from autoverse.types import (
    AbortSignal,
    ProviderCapabilities,
    StreamEvent,
    TextDeltaPayload,
    ToolStartPayload,
    TurnEndPayload,
    Usage,
    UsagePayload,
    make_event,
)
from autoverse.types.abort import AbortController
from autoverse.types.llm import LLMRequest
from autoverse.types.messages import Message, ToolResultBlock

from tests._doubles import ScriptedProvider


def _agent_turn(tid: str, inp: dict[str, Any]) -> list[Any]:
    return [
        make_event(ToolStartPayload(tool_use_id=tid, tool_name="agent", tool_input=inp)),
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
    raise AssertionError("no tool-result block found")


# ── gap8-A2: a foreground scout sub-agent runs and its summary flows back ─────
async def test_foreground_subagent_result_flows_back(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _agent_turn("a1", {"subagent_type": "scout", "task": "find the foo"}),
            _text_turn("SCOUT REPORT: found foo in bar.py"),  # child's only turn
            _text_turn("parent done"),  # parent resumes after the tool result
        ]
    )
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "scout for foo")

    block = _last_tool_result(engine._conversation)
    assert not block.is_error
    assert "SCOUT REPORT" in block.content
    assert block.metadata.get("subagent_type") == "scout"


# ── gap8-A2: agent + JobOutput shells reach the main LLMRequest.tools ─────────
async def test_agent_and_joboutput_registered(tmp_path: Path) -> None:
    provider = ScriptedProvider([_text_turn("hi")])
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "hello")
    names = {t.name for t in provider.requests[0].tools}
    assert {"agent", "JobOutput"} <= names


# ── gap8-B3: JobOutput collects a finished background job from the store ──────
async def test_joboutput_tool_collects(tmp_path: Path) -> None:
    provider = ScriptedProvider([_text_turn("hi")])
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    app = engine._app
    assert isinstance(app.jobs, InMemoryJobStore)
    job_id = await app.jobs.create(subject="scout: find foo")
    await app.jobs.complete(job_id, "background scout finished: 3 hits")

    ctx = ToolContext(
        cwd=tmp_path,
        permissions=_AllowAll(),
        tracker=FileTracker(),
        signal=AbortController().signal,
        events=app.events,
        app=app,
    )
    result = await JobOutputTool().run(JobOutputArgs(block=False), ctx)
    assert not result.is_error
    assert "background scout finished" in result.output
    assert result.metadata.get("collected") == 1


# ── gap8-A2: a background sub-agent dispatch is collected via JobOutput ───────
async def test_background_subagent_collected(tmp_path: Path) -> None:
    parent_turns = [
        _agent_turn("a1", {"subagent_type": "scout", "task": "bg find", "run_in_background": True}),
        [
            make_event(ToolStartPayload(tool_use_id="j1", tool_name="JobOutput", tool_input={"timeout": 10.0})),
            make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="TOOL_USE")),
        ],
        _text_turn("parent done"),
    ]
    provider = _RoutingProvider(parent_turns, child_text="BG SCOUT done: ok")
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, llm_provider=provider)
    ).build()
    await _drain(engine, "background scout")

    block = _last_tool_result(engine._conversation)
    assert not block.is_error
    assert "BG SCOUT done" in block.content


class _AllowAll:
    async def check(self, call: Any) -> Any:
        from autoverse.types.tools import Decision

        return Decision.allow()

    def check_sync(self, call: Any) -> Any:
        from autoverse.types.tools import Decision

        return Decision.allow()

    async def request_approval(self, call: Any) -> Any:
        from autoverse.types.tools import Decision

        return Decision.allow()

    def cancel_all(self) -> None:
        return None


class _RoutingProvider:
    """Routes by request system prompt: child requests (no main intro) get a fixed
    text turn; parent requests replay ``parent_turns`` by call order. This keeps
    the background test deterministic despite concurrent child execution."""

    def __init__(self, parent_turns: list[Any], *, child_text: str) -> None:
        self._parent = parent_turns
        self._pi = 0
        self._child_text = child_text
        self.requests: list[LLMRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def stream(self, req: LLMRequest, signal: AbortSignal) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        signal.raise_if_aborted()
        is_child = "你是 AutoVerse" not in req.system
        turn = _text_turn(self._child_text) if is_child else self._parent[
            min(self._pi, len(self._parent) - 1)
        ]
        if not is_child:
            self._pi += 1
        for ev in turn:
            signal.raise_if_aborted()
            assert isinstance(ev, StreamEvent)
            yield ev

    async def count_tokens(self, req: LLMRequest) -> int:
        return max(1, len(req.system) // 4)

    async def aclose(self) -> None:
        return None
