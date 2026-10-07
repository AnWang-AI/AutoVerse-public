"""Phase-1 comprehensive real-API verification against OpenRouter.

Exercises codec / transport / router / fallback / error-classification over real
network calls to deepseek-v4-pro (reasoning model). Throwaway diagnostic script.
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
from autoverse.infra.llm.transport.credential import CredentialResolver, bearer
from autoverse.infra.llm.transport.http import HttpTransport
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

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-v4-pro"


def spec(thinking: bool = True) -> ModelSpec:
    return ModelSpec(
        model_id=MODEL,
        label="DeepSeek V4 Pro",
        description="real-api",
        context_window=128_000,
        capabilities=ProviderCapabilities(
            streaming=True, thinking=thinking, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="or.dsv4.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("OPENROUTER_API_KEY"),
                endpoint=ENDPOINT,
                real_streaming=True,
            ),
        ),
    )


def provider(thinking: bool = True) -> WireProvider:
    creds = CredentialResolver()
    s = spec(thinking)
    plan = FallbackPlan(
        retry=RetryStrategy(),
        protocol_switch=ProtocolSwitchStrategy(s),
        model_fallback=ModelFallbackStrategy(s, router=Router(creds)),
    )
    return WireProvider(
        s,
        OpenAIWireCodec(),
        [HttpTransport(ts, ts.auth, creds) for ts in s.transports],
        fallback=plan,
        recovery=None,
    )


async def collect(p: WireProvider, req: LLMRequest) -> dict:
    ctrl = AbortController()
    out: dict = {
        "text": "",
        "thinking": "",
        "tools": [],
        "usage": None,
        "cost": None,
        "errors": [],
        "types": [],
    }
    async for ev in p.stream(req, ctrl.signal):
        out["types"].append(ev.type.value)
        if ev.type is EventType.TEXT_DELTA:
            out["text"] += ev.payload.text
        elif ev.type is EventType.THINKING_DELTA:
            out["thinking"] += ev.payload.text
        elif ev.type is EventType.TOOL_START:
            out["tools"].append((ev.payload.tool_name, ev.payload.tool_input))
        elif ev.type is EventType.USAGE:
            out["usage"] = ev.payload.usage
            out["cost"] = ev.payload.cost_usd
        elif ev.type is EventType.ERROR:
            out["errors"].append(
                (ev.payload.http_status, ev.payload.error_class, ev.payload.message[:80])
            )
    await p.aclose()
    return out


def msg(role: str, text: str) -> Message:
    return Message(role=role, content=(TextBlock(text=text),))


async def t_text() -> bool:
    print("\n[T1] plain text")
    r = await collect(
        provider(),
        LLMRequest(messages=(msg("user", "Reply with exactly: PONG"),),
                   system="terse", model=MODEL, max_tokens=128, temperature=0.0),
    )
    print(f"  text={r['text']!r} usage={r['usage']}")
    ok = "PONG" in r["text"] and r["usage"] is not None
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def t_thinking() -> bool:
    print("\n[T2] thinking/reasoning delta captured")
    r = await collect(
        provider(thinking=True),
        LLMRequest(messages=(msg("user", "What is 17*23? Think step by step then give the number."),),
                   system="", model=MODEL, max_tokens=400, temperature=0.0),
    )
    print(f"  thinking_len={len(r['thinking'])} text={r['text'][:60]!r}")
    ok = len(r["thinking"]) > 0 and "391" in (r["thinking"] + r["text"])
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def t_tool_once() -> bool:
    print("\n[T3] single tool call (no duplicate)")
    weather = ToolSchema(
        name="get_weather", description="Weather for a city.",
        json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
    r = await collect(
        provider(),
        LLMRequest(messages=(msg("user", "Weather in Paris? Use the tool."),),
                   system="Always call the tool.", tools=(weather,),
                   model=MODEL, max_tokens=512, temperature=0.0),
    )
    print(f"  tools={r['tools']}")
    ok = r["tools"] == [("get_weather", {"city": "Paris"})]
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def t_tool_loop() -> bool:
    print("\n[T4] multi-turn: tool result fed back, model uses it")
    weather = ToolSchema(
        name="get_weather", description="Weather for a city.",
        json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
    # Turn 1: get the tool call
    p = provider()
    r1 = await collect(
        p,
        LLMRequest(messages=(msg("user", "Weather in Paris? Use the tool."),),
                   system="Always call the tool first.", tools=(weather,),
                   model=MODEL, max_tokens=512, temperature=0.0),
    )
    if not r1["tools"]:
        print("  no tool call in turn1 -> FAIL")
        return False
    name, args = r1["tools"][0]
    tool_use_id = "call_x"
    # Turn 2: feed tool result back
    conv = (
        msg("user", "Weather in Paris? Use the tool."),
        Message(role="assistant", content=(ToolUseBlock(id=tool_use_id, name=name, input=args),)),
        Message(role="user", content=(ToolResultBlock(tool_use_id=tool_use_id, content="Sunny, 25C"),)),
    )
    r2 = await collect(
        provider(),
        LLMRequest(messages=conv, system="Answer using the tool result.",
                   tools=(weather,), model=MODEL, max_tokens=400, temperature=0.0),
    )
    final = r2["text"] + r2["thinking"]
    print(f"  final_text={r2['text'][:80]!r}")
    ok = ("25" in final or "sunny" in final.lower()) and not r2["errors"]
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def t_error_classification() -> bool:
    print("\n[T5] error classification: bad model -> structured ERROR (not crash)")
    creds = CredentialResolver()
    bad = ModelSpec(
        model_id="nonexistent/model-xyz-999", label="bad", description="bad",
        capabilities=ProviderCapabilities(streaming=True),
        transports=(TransportSpec(id="bad", wire=WireKind.OPENAI,
                    auth=bearer("OPENROUTER_API_KEY"), endpoint=ENDPOINT, real_streaming=True),),
    )
    plan = FallbackPlan(retry=RetryStrategy(), protocol_switch=ProtocolSwitchStrategy(bad),
                        model_fallback=ModelFallbackStrategy(bad, router=Router(creds)))
    p = WireProvider(bad, OpenAIWireCodec(),
                     [HttpTransport(ts, ts.auth, creds) for ts in bad.transports],
                     fallback=plan, recovery=None)
    r = await collect(p, LLMRequest(messages=(msg("user", "hi"),), system="",
                                    model="nonexistent/model-xyz-999", max_tokens=10))
    print(f"  errors={r['errors']}")
    ok = len(r["errors"]) > 0 and r["errors"][0][0] in (400, 404)
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def t_auth_error() -> bool:
    print("\n[T6] auth error: bad key -> 401, classified")
    os.environ["BAD_KEY_TEST"] = "sk-or-v1-invalidkey000"
    creds = CredentialResolver()
    s = ModelSpec(
        model_id=MODEL, label="x", description="x",
        capabilities=ProviderCapabilities(streaming=True),
        transports=(TransportSpec(id="badauth", wire=WireKind.OPENAI,
                    auth=bearer("BAD_KEY_TEST"), endpoint=ENDPOINT, real_streaming=True),),
    )
    plan = FallbackPlan(retry=RetryStrategy(), protocol_switch=ProtocolSwitchStrategy(s),
                        model_fallback=ModelFallbackStrategy(s, router=Router(creds)))
    p = WireProvider(s, OpenAIWireCodec(),
                     [HttpTransport(ts, ts.auth, creds) for ts in s.transports],
                     fallback=plan, recovery=None)
    r = await collect(p, LLMRequest(messages=(msg("user", "hi"),), system="", model=MODEL, max_tokens=10))
    del os.environ["BAD_KEY_TEST"]
    print(f"  errors={r['errors']}")
    ok = len(r["errors"]) > 0 and r["errors"][0][0] == 401
    print("  ->", "PASS" if ok else "FAIL")
    return ok


async def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY not set")
        return 2
    tests = [t_text, t_thinking, t_tool_once, t_tool_loop, t_error_classification, t_auth_error]
    results = []
    for t in tests:
        try:
            results.append(await t())
        except Exception as e:  # noqa: BLE001
            print(f"  EXCEPTION: {type(e).__name__}: {e} -> FAIL")
            results.append(False)
    print(f"\n=== Phase1 real-API: {sum(results)}/{len(results)} passed ===")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
