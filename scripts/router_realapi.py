"""Verify the production Router public API end-to-end against real OpenRouter.

Registers an OpenRouter DeepSeek spec into the live registry, then drives it
through Router.build() (the real routing entry point) — exercising codec +
transport + fallback + my P1 fixes via the public path, not a hand-built
provider.
"""

from __future__ import annotations

import asyncio
import os

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
    WireKind,
)

MODEL = "deepseek/deepseek-v4-pro"


def _register() -> None:
    spec = ModelSpec(
        model_id=MODEL,
        label="DeepSeek V4 Pro",
        description="real-api router test",
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


async def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("no key")
        return 2
    _register()
    router = Router(CredentialResolver())
    provider = router.build(MODEL)  # production routing entry point

    req = LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="Reply in one short French sentence."),)),),
        system="You MUST reply ONLY in French.",
        model=MODEL,
        max_tokens=200,
        temperature=0.0,
    )
    ctrl = AbortController()
    text = ""
    saw_req = False
    async for ev in provider.stream(req, ctrl.signal):
        if ev.type is EventType.REQUEST:
            saw_req = True
        elif ev.type is EventType.TEXT_DELTA:
            text += ev.payload.text
    await provider.aclose()

    print(f"  text={text!r}")
    french = any(w in text.lower() for w in ("bonjour", "salut", "je ", "vous", "voici", "c'est"))
    ok = saw_req and french
    print(f"  REQUEST emitted={saw_req}  french={french}")
    print("=== Router real-API:", "PASS" if ok else "FAIL", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
