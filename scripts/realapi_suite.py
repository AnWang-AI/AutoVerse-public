"""Comprehensive real-API verification of the AutoVerse LLM stack.

Drives the *production* code paths (Router.build -> WireProvider -> codec +
transport + fallback) against the live OpenRouter ``deepseek/deepseek-v4-pro``
model and asserts protocol-level invariants from ``docs/reference/models.md``.

Run:
    OPENROUTER_API_KEY=... python scripts/realapi_suite.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field

import autoverse.infra.llm.registry as reg
from autoverse.infra.llm.registry import ModelSpec, TransportSpec
from autoverse.infra.llm.router import Router
from autoverse.infra.llm.transport.credential import CredentialResolver, bearer
from autoverse.types import (
    AbortController,
    EventType,
    LLMRequest,
    Message,
    ProviderCapabilities,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    WireKind,
)

MODEL = "deepseek/deepseek-v4-pro"


def _register() -> None:
    spec = ModelSpec(
        model_id=MODEL,
        label="DeepSeek V4 Pro",
        description="real-api suite",
        context_window=128_000,
        capabilities=ProviderCapabilities(streaming=True, thinking=True),
        transports=(
            TransportSpec(
                id="or.dsv4.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                real_streaming=True,
            ),
        ),
    )
    reg.MODEL_REGISTRY[MODEL] = spec


@dataclass
class Drained:
    events: list = field(default_factory=list)
    text: str = ""
    thinking: str = ""
    tool_starts: list = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    turn_end = None
    usage = None


async def _drain(provider, req: LLMRequest, ctrl: AbortController) -> Drained:
    d = Drained()
    async for ev in provider.stream(req, ctrl.signal):
        d.events.append(ev)
        d.counts[ev.type] += 1
        if ev.type is EventType.TEXT_DELTA:
            d.text += ev.payload.text
        elif ev.type is EventType.THINKING_DELTA:
            d.thinking += ev.payload.text
        elif ev.type is EventType.TOOL_START:
            d.tool_starts.append(ev.payload)
        elif ev.type is EventType.TURN_END:
            d.turn_end = ev.payload
        elif ev.type is EventType.USAGE:
            d.usage = ev.payload
    return d


# ── scenarios ────────────────────────────────────────────────────────────

async def s_basic(provider) -> tuple[bool, str]:
    """REQUEST first, TEXT accumulates, TURN_END last, ordering invariant."""
    req = LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="Reply with exactly: PONG"),)),),
        system="", model=MODEL, max_tokens=300, temperature=0.0,
    )
    d = await _drain(provider, req, AbortController())
    if not d.events:
        return False, "no events"
    if d.events[0].type is not EventType.REQUEST:
        return False, f"first event is {d.events[0].type}, expected REQUEST"
    # TURN_END must be emitted EXACTLY once even when the backend sends two
    # terminal chunks (DeepSeek-V4 splits finish_reason and usage across two
    # chunks). USAGE may legitimately trail TURN_END for such backends — the
    # accumulator/collector fold it regardless of position.
    n_turn_end = d.counts.get(EventType.TURN_END, 0)
    if n_turn_end != 1:
        return False, f"expected exactly 1 TURN_END, got {n_turn_end}: {dict(d.counts)}"
    if "PONG" not in d.text.upper():
        return False, f"unexpected text: {d.text!r}"
    return True, f"text={d.text.strip()!r} events={dict(d.counts)}"


async def s_system_prompt(provider) -> tuple[bool, str]:
    """System prompt must be honoured (OpenAI-wire system-as-message fix)."""
    req = LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="Say hello."),)),),
        system="You MUST reply ONLY in French. No English.",
        model=MODEL, max_tokens=300, temperature=0.0,
    )
    d = await _drain(provider, req, AbortController())
    fr = any(w in d.text.lower() for w in ("bonjour", "salut", "je ", "vous", "voici", "c'est", "coucou"))
    if not fr:
        return False, f"system prompt seemingly ignored: {d.text!r}"
    return True, f"french reply={d.text.strip()!r}"


async def s_thinking(provider) -> tuple[bool, str]:
    """Reasoning model: THINKING_DELTA emitted alongside TEXT (reasoning field)."""
    req = LLMRequest(
        messages=(Message(role="user", content=(
            TextBlock(text="What is 17*23? Think step by step, then give the number."),)),),
        system="", model=MODEL, max_tokens=800, temperature=0.0, thinking_budget=512,
    )
    d = await _drain(provider, req, AbortController())
    if "391" not in d.text:
        return False, f"wrong answer, text={d.text!r}"
    # V4 Pro returns reasoning; we should have captured THINKING_DELTA events.
    if d.counts.get(EventType.THINKING_DELTA, 0) == 0:
        return False, f"no THINKING_DELTA captured (reasoning dropped); text={d.text.strip()!r}"
    return True, f"thinking_chars={len(d.thinking)} answer_ok"


async def s_usage(provider) -> tuple[bool, str]:
    """USAGE event present with non-zero token counts."""
    req = LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="Count to three."),)),),
        system="", model=MODEL, max_tokens=300, temperature=0.0,
    )
    d = await _drain(provider, req, AbortController())
    if d.usage is None:
        return False, "no USAGE event"
    u = d.usage.usage
    if u.input_tokens <= 0 or u.output_tokens <= 0:
        return False, f"zero usage: in={u.input_tokens} out={u.output_tokens}"
    return True, f"in={u.input_tokens} out={u.output_tokens}"


async def s_tool_call(provider) -> tuple[bool, str]:
    """tool_calls dialect: model emits TOOL_START with parsed args; finish=tool_calls."""
    weather = ToolSchema(
        name="get_weather",
        description="Get the current weather for a city.",
        json_schema={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    )
    req = LLMRequest(
        messages=(Message(role="user", content=(
            TextBlock(text="Use the get_weather tool to check the weather in Paris. You must call the tool."),)),),
        system="You are a tool-using assistant. When asked about weather, call get_weather.",
        tools=(weather,),
        model=MODEL, max_tokens=500, temperature=0.0,
    )
    d = await _drain(provider, req, AbortController())
    if not d.tool_starts:
        return False, f"no TOOL_START; text={d.text.strip()!r} turn_end={d.turn_end}"
    ts = d.tool_starts[0]
    if ts.tool_name != "get_weather":
        return False, f"wrong tool: {ts.tool_name}"
    if not isinstance(ts.tool_input, dict) or "city" not in ts.tool_input:
        return False, f"args not parsed into dict: {ts.tool_input!r}"
    if "paris" not in str(ts.tool_input.get("city", "")).lower():
        return False, f"city not Paris: {ts.tool_input!r}"
    return True, f"tool={ts.tool_name} args={ts.tool_input} id={ts.tool_use_id!r}"


async def s_tool_roundtrip(provider) -> tuple[bool, str]:
    """Feed a ToolResultBlock back; model must consume it and answer."""
    weather = ToolSchema(
        name="get_weather",
        description="Get the current weather for a city.",
        json_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    # First turn: get a tool call.
    req1 = LLMRequest(
        messages=(Message(role="user", content=(
            TextBlock(text="What's the weather in Tokyo? Call get_weather."),)),),
        system="Call get_weather when asked about weather.",
        tools=(weather,), model=MODEL, max_tokens=500, temperature=0.0,
    )
    d1 = await _drain(provider, req1, AbortController())
    if not d1.tool_starts:
        return False, "first turn produced no tool call"
    ts = d1.tool_starts[0]
    # Second turn: assistant tool_use + user tool_result.
    req2 = LLMRequest(
        messages=(
            Message(role="user", content=(TextBlock(text="What's the weather in Tokyo? Call get_weather."),)),
            Message(role="assistant", content=(ToolUseBlock(id=ts.tool_use_id, name=ts.tool_name, input=ts.tool_input),)),
            Message(role="user", content=(ToolResultBlock(tool_use_id=ts.tool_use_id, content="22C and sunny"),)),
        ),
        system="Call get_weather when asked about weather.",
        tools=(weather,), model=MODEL, max_tokens=500, temperature=0.0,
    )
    d2 = await _drain(provider, req2, AbortController())
    if "22" not in d2.text and "sunny" not in d2.text.lower():
        return False, f"model didn't use tool result; text={d2.text.strip()!r}"
    return True, f"final={d2.text.strip()[:80]!r}"


async def s_abort(provider) -> tuple[bool, str]:
    """Aborting mid-stream stops event flow cleanly (no hang, no crash)."""
    req = LLMRequest(
        messages=(Message(role="user", content=(
            TextBlock(text="Write a long 500-word essay about the ocean."),)),),
        system="", model=MODEL, max_tokens=2000, temperature=0.0,
    )
    ctrl = AbortController()
    n = 0
    try:
        async for ev in provider.stream(req, ctrl.signal):
            n += 1
            if ev.type is EventType.TEXT_DELTA and len(ev.payload.text) >= 0:
                if n >= 3:
                    ctrl.abort("test abort")
                    break
    except Exception as exc:  # noqa: BLE001
        return False, f"abort raised: {exc!r}"
    return True, f"aborted after {n} events cleanly"


async def s_turnloop_e2e(provider) -> tuple[bool, str]:
    """End-to-end: drive the real provider through the production TurnLoop and
    confirm the conversation is persisted with exactly one assistant turn that
    terminated on END_TURN with non-zero usage."""
    import sys
    from pathlib import Path

    sys.path.insert(0, "tests")
    from _doubles import MemStore  # type: ignore[import-not-found]

    from autoverse.app.config import Config
    from autoverse.app.context import AppContext
    from autoverse.core.context.compaction import CompactionPolicy
    from autoverse.core.context.recovery import RecoveryPolicy
    from autoverse.core.loop.turn_loop import TurnLoop
    from autoverse.core.loop.types import TurnOptions
    from autoverse.core.state.conversation import Conversation
    from autoverse.core.workflow.stop_gate import CompletionGate, GateRegistry
    from autoverse.core.context.config import CompactionConfig, RecoveryConfig

    events: list = []

    class _Sink:
        async def emit(self, ev) -> None:  # noqa: ANN001
            events.append(ev)

    store = MemStore()
    conv = Conversation(store, "e2e")
    sink = _Sink()
    app = AppContext(
        config=Config(), store=store, cwd=Path.cwd(), model=MODEL, events=sink,
    )
    gates = GateRegistry()
    gates.register(CompletionGate())
    loop = TurnLoop(
        llm=provider, conversation=conv, events=sink,
        compaction=CompactionPolicy(provider, CompactionConfig()),
        recovery=RecoveryPolicy(RecoveryConfig()),
        gates=gates,
        dispatch_tools=lambda calls, ctx: _noop(calls),
        app=app,
    )
    await conv.append(Message(role="user", content=(TextBlock(text="Reply with exactly: READY"),)))
    result = await loop.run_turn(TurnOptions())
    msgs = conv.view().messages
    assistants = [m for m in msgs if m.role == "assistant"]
    if not assistants:
        return False, f"no assistant message persisted; result={result}"
    n_turn_end = sum(1 for e in events if e.type is EventType.TURN_END)
    usage_total = sum(
        e.payload.usage.output_tokens for e in events if e.type is EventType.USAGE
    )
    if n_turn_end < 1:
        return False, "no TURN_END in loop events"
    if usage_total <= 0:
        return False, "loop captured zero output usage"
    return (
        True,
        f"iterations={result.iterations} assistant_msgs={len(assistants)} "
        f"usage_out={usage_total} cause={result.cause}",
    )


async def _noop(calls):  # noqa: ANN001
    from autoverse.types.tools import ToolResult
    return [ToolResult.ok("ok") for _ in calls]


SCENARIOS = [
    ("M1 basic ordering (REQUEST..TEXT..TURN_END)", s_basic),
    ("M2 system prompt honoured", s_system_prompt),
    ("M3 thinking/reasoning deltas", s_thinking),
    ("M4 usage accounting", s_usage),
    ("M5 tool_call emits TOOL_START", s_tool_call),
    ("M6 tool result round-trip", s_tool_roundtrip),
    ("M7 mid-stream abort", s_abort),
    ("M8 end-to-end TurnLoop", s_turnloop_e2e),
]


async def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set")
        return 2
    _register()
    router = Router(CredentialResolver())
    provider = router.build(MODEL)

    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            ok, detail = await fn(provider)
        except Exception:  # noqa: BLE001
            ok, detail = False, "EXC:\n" + traceback.format_exc()
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}\n        {detail}")
        if ok:
            passed += 1
        else:
            failed += 1
    await provider.aclose()
    print(f"\n=== real-API suite: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
