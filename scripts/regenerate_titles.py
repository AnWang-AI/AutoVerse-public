"""Regenerate AI titles for all AutoVerse sessions in ~/.autoverse/sessions/.

Uses the fast-tier model (deepseek/deepseek-v4-flash) to produce semantic
5-8 character Chinese titles from each session's user prompts. Skips sessions
with ``title_locked`` (manually renamed). Best-effort: a single failure never
halts the batch.

Usage:
    cd /Users/alden/CCWorkSpace/AutoVerse
    python scripts/regenerate_titles.py [--dry-run] [--delay 0.5]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Autoverse imports (dev install: pip install -e ".[dev]")
# ---------------------------------------------------------------------------
from autoverse.app.engine import _TITLE_SYSTEM_PROMPT, _clean_title
from autoverse.infra.llm.registry import resolve_alias
from autoverse.infra.llm.router import Router
from autoverse.infra.llm.transport.credential import CredentialResolver
from autoverse.infra.persist.jsonl_store import JSONLStore
from autoverse.infra.secrets import EnvFirstSecretProvider, SecretVault, VaultUnavailable
from autoverse.ports import SessionMeta
from autoverse.types import EventType
from autoverse.types.abort import AbortController
from autoverse.types.llm import LLMRequest
from autoverse.types.messages import Message, TextBlock


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def stream_text(provider, req: LLMRequest) -> str:
    """Drain a provider stream, accumulating only TEXT_DELTA payloads."""
    signal = AbortController().signal
    buf: list[str] = []
    async for ev in provider.stream(req, signal):
        if ev.type == EventType.TEXT_DELTA:
            buf.append(ev.payload.text)
    return "".join(buf)


def collect_user_prompts(messages: list[Message]) -> str:
    """Concatenate the text of every user message (mirrors MetaStamp version)."""
    parts: list[str] = []
    for msg in messages:
        if getattr(msg, "role", "") != "user":
            continue
        for block in getattr(msg, "content", ()):
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(" ".join(text.split()))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def regenerate(store: JSONLStore, provider, *, dry_run: bool = False, delay: float = 0.5) -> dict[str, str]:
    """Walk all sessions, regenerate titles, return {session_id: new_title}."""
    session_dir = Path("~/.autoverse/sessions").expanduser()
    jsonl_files = sorted(session_dir.glob("sess-*.jsonl"))
    total = len(jsonl_files)
    results: dict[str, str] = {}

    print(f"Found {total} sessions in {session_dir}")
    print(f"Dry run: {dry_run}, delay: {delay}s")
    print("-" * 60)

    for i, path in enumerate(jsonl_files, 1):
        sid = path.stem
        ts = time.time()

        try:
            # ── load meta ──
            meta = await store.load_meta(sid)
            extra = getattr(meta, "extra", {}) or {}
            if isinstance(extra, dict) and extra.get("title_locked"):
                print(f"  [{i:3d}/{total}] {sid}  SKIP (title_locked)")
                continue

            # ── collect user prompts ──
            msgs: list[Message] = await store.load(sid, since=0)
            if not msgs:
                print(f"  [{i:3d}/{total}] {sid}  SKIP (no messages)")
                continue

            prompts = collect_user_prompts(msgs)
            if not prompts.strip():
                print(f"  [{i:3d}/{total}] {sid}  SKIP (empty user prompt)")
                continue

            # ── call LLM ──
            instruction = (
                "请为下面的对话起一个简洁、语义化的中文标题，长度约 5-8 个字。"
                "只输出标题本身，不要解释、标点、引号或代码。\n\n"
                "=== 用户请求 ===\n" + prompts
            )
            req = LLMRequest(
                messages=(Message(role="user", content=(TextBlock(text=instruction),)),),
                system=_TITLE_SYSTEM_PROMPT,
                model=FAST_MODEL,
                max_tokens=512,
                temperature=0.3,
            )
            raw = await asyncio.wait_for(stream_text(provider, req), timeout=30.0)
            title = _clean_title(raw)

            if not title:
                print(f"  [{i:3d}/{total}] {sid}  EMPTY (model returned nothing)")
                continue

            elapsed = time.time() - ts

            if dry_run:
                print(f"  [{i:3d}/{total}] {sid}  would set → 「{title}」 ({elapsed:.1f}s)")
                results[sid] = title
            else:
                await store.update_meta(sid, title=title)
                print(f"  [{i:3d}/{total}] {sid}  → 「{title}」 ({elapsed:.1f}s)")
                results[sid] = title

            # Rate-limit: pause between calls
            if delay > 0 and i < total:
                await asyncio.sleep(delay)

        except asyncio.TimeoutError:
            print(f"  [{i:3d}/{total}] {sid}  TIMEOUT")
        except Exception as exc:
            print(f"  [{i:3d}/{total}] {sid}  ERROR: {exc}")

    return results


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

FAST_MODEL = "deepseek/deepseek-v4-flash"

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    delay = 0.5
    for arg in sys.argv:
        if arg.startswith("--delay="):
            delay = float(arg.split("=", 1)[1])

    async def main():
        # Secrets: env-first, then vault
        vault = None
        try:
            vault = SecretVault(Path("~/.autoverse/secrets.enc").expanduser())
        except VaultUnavailable:
            pass
        secrets = EnvFirstSecretProvider(vault)

        # Provider: router for the fast-tier model
        creds = CredentialResolver(secrets)
        router = Router(creds)
        resolved = resolve_alias(FAST_MODEL)
        print(f"Model: {FAST_MODEL} → {resolved}")
        provider = router.build(FAST_MODEL)

        # Store
        store = JSONLStore(Path("~/.autoverse/sessions").expanduser())

        t0 = time.time()
        results = await regenerate(store, provider, dry_run=dry_run, delay=delay)
        total_time = time.time() - t0

        print("-" * 60)
        print(f"Done. {len(results)} titles {'would be' if dry_run else ''} regenerated in {total_time:.1f}s.")

    asyncio.run(main())
