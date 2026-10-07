"""Real-API smoke test against OpenRouter (OpenAI-compatible wire).

Throwaway script — exercises the Phase-1 LLM machinery (Router/Codec/Transport/
WireProvider/Fallback) end-to-end over a real network call. Not part of the test
suite; run manually with OPENROUTER_API_KEY set.
"""

from __future__ import annotations

import asyncio
import os

from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.fallback import (
    FallbackPlan,
    ModelFallbackStrategy,
    ProtocolSwitchStrategy,
    RetryStrategy,
)
from autoverse.infra.llm.provider import WireProvider
from autoverse.infra.llm.registry import ModelSpec, TransportSpec
from autoverse.infra.llm.router import Router
from autoverse.infra.llm.transport.credential import (
    CredentialResolver,
    bearer,
)
from autoverse.infra.llm.transport.http import HttpTransport
from autoverse.types import (
    AbortController,
    EventType,
    LLMRequest,
    Message,
    ProviderCapabilities,
    TextBlock,
    ToolSchema,
    WireKind,
)

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL_ID = "deepseek/deepseek-v4-pro"  # OpenRouter DeepSeek V4 Pro


def make_openrouter_spec() -> ModelSpec:
    return ModelSpec(
        model_id=MODEL_ID,
        label="OpenRouter gpt-4o-mini",
        description="Real-API smoke via OpenRouter (openai wire, bearer).",
        context_window=128_000,
        capabilities=ProviderCapabilities(
            streaming=True, thinking=False, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="openrouter.gpt4omini.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint=OPENROUTER_ENDPOINT,
                real_streaming=True,
            ),
        ),
        fallback_model_id=None,
    )


def build_provider() -> WireProvider:
    creds = CredentialResolver()
    spec = make_openrouter_spec()
    codec = OpenAIWireCodec()
    transports = [HttpTransport(ts, ts.auth, creds) for ts in spec.transports]
    plan = FallbackPlan(
        retry=RetryStrategy(),
        protocol_switch=ProtocolSwitchStrategy(spec),
        model_fallback=ModelFallbackStrategy(spec, router=Router(creds)),
    )
    return WireProvider(spec, codec, transports, fallback=plan, recovery=None)


async def test_text() -> bool:
    print("\n=== TEST 1: plain text streaming ===")
    provider = build_provider()
    req = LLMRequest(
        messages=(
            Message(role="user", content=(TextBlock(text="Reply with exactly: PONG"),)),
        ),
        system="You are a terse echo bot.",
        model=MODEL_ID,
        max_tokens=256,
        temperature=0.0,
    )
    ctrl = AbortController()
    text = ""
    saw_request = saw_usage = False
    async for ev in provider.stream(req, ctrl.signal):
        if ev.type is EventType.REQUEST:
            saw_request = True
        elif ev.type is EventType.TEXT_DELTA:
            text += ev.payload.text
        elif ev.type is EventType.USAGE:
            saw_usage = True
            print(f"  usage: model={ev.payload.model} usage={ev.payload.usage}")
    await provider.aclose()
    print(f"  text   = {text!r}")
    print(f"  REQUEST event emitted: {saw_request}")
    ok = bool(text.strip()) and saw_request
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_tool_call() -> bool:
    print("\n=== TEST 2: tool calling (openai wire) ===")
    provider = build_provider()
    weather = ToolSchema(
        name="get_weather",
        description="Get current weather for a city.",
        json_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    req = LLMRequest(
        messages=(
            Message(
                role="user",
                content=(TextBlock(text="What is the weather in Paris? Use the tool."),),
            ),
        ),
        system="You are a weather assistant. Always call the provided tool.",
        tools=(weather,),
        model=MODEL_ID,
        max_tokens=512,
        temperature=0.0,
    )
    ctrl = AbortController()
    tool_starts: list[tuple[str, dict]] = []
    async for ev in provider.stream(req, ctrl.signal):
        if ev.type is EventType.TOOL_START:
            tool_starts.append((ev.payload.tool_name, ev.payload.tool_input))
    await provider.aclose()
    print(f"  tool_starts = {tool_starts}")
    ok = any(name == "get_weather" for name, _ in tool_starts)
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    print(f"OPENROUTER_API_KEY present: {bool(key)} (len={len(key)})")
    if not key:
        print("ERROR: OPENROUTER_API_KEY not set")
        return 2
    results = []
    results.append(await test_text())
    results.append(await test_tool_call())
    print("\n=== SUMMARY ===")
    print(f"  passed {sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
