"""Real-API verification: system prompt is now obeyed on OpenAI/OpenRouter wire,
and thinking_budget triggers reasoning deltas."""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "scripts")
from phase1_realapi import collect, msg, provider  # noqa: E402

from autoverse.types import LLMRequest  # noqa: E402

MODEL = "deepseek/deepseek-v4-pro"


async def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("no key")
        return 2

    print("[S1] system prompt obeyed (French)")
    r = await collect(
        provider(),
        LLMRequest(
            messages=(msg("user", "Say hello in one short sentence."),),
            system="You MUST reply ONLY in French. Never use English.",
            model=MODEL,
            max_tokens=200,
            temperature=0.0,
        ),
    )
    txt = (r["text"] + r["thinking"]).lower()
    french = any(w in txt for w in ("bonjour", "salut", "vous", "je "))
    print(f"  reply={r['text'][:80]!r}")
    print("  ->", "PASS (french)" if french else "FAIL (not french)")

    print("\n[S2] thinking_budget triggers reasoning delta")
    r2 = await collect(
        provider(thinking=True),
        LLMRequest(
            messages=(msg("user", "Is 91 prime? Answer yes or no."),),
            system="",
            model=MODEL,
            max_tokens=300,
            temperature=0.0,
            thinking_budget=1024,
        ),
    )
    print(f"  thinking_len={len(r2['thinking'])} text={r2['text'][:50]!r}")
    has_thinking = len(r2["thinking"]) > 0
    print("  ->", "PASS (reasoning captured)" if has_thinking else "FAIL")

    ok = french and has_thinking
    print(f"\n=== system/thinking fix: {'ALL PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
