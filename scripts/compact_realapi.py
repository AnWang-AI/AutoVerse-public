"""Real-API verification of the compaction fixes against OpenRouter DeepSeek V4 Pro.

Throwaway manual script (not part of the test suite). Drives the *real* LLM
summariser through CompactionPolicy to prove the four fixes work end-to-end:

  1. force_compact  -> a real LLM meta-summary replaces the head (manual /compact).
  2. maybe_compact  -> auto-escalates to the LLM meta-summary in the
                       [compact_threshold, truncate_threshold) band when there is
                       no tool-noise left to micro-fold (the #1 cascade fix).
  3. re-compaction  -> a prior summary is NOT sent to the summariser again but
                       still survives as superseded context (the #3 dedup fix).

Run:  OPENROUTER_API_KEY=... python scripts/compact_realapi.py
"""

from __future__ import annotations

import asyncio
import os

from autoverse.core.context.compaction import CompactionPolicy
from autoverse.core.context.config import CompactionConfig
from autoverse.core.context.split import _SUMMARY_MARKER
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
    EventType,
    ProviderCapabilities,
    WireKind,
)
from autoverse.types.messages import Message, TextBlock

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL_ID = "deepseek/deepseek-v4-pro"


def make_spec() -> ModelSpec:
    return ModelSpec(
        model_id=MODEL_ID,
        label="OpenRouter DeepSeek V4 Pro",
        description="Real-API compaction verification.",
        context_window=128_000,
        capabilities=ProviderCapabilities(
            streaming=True, thinking=False, cache=False, vision=False
        ),
        transports=(
            TransportSpec(
                id="openrouter.dsv4pro.bearer",
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
    spec = make_spec()
    codec = OpenAIWireCodec()
    transports = [HttpTransport(ts, ts.auth, creds) for ts in spec.transports]
    plan = FallbackPlan(
        retry=RetryStrategy(),
        protocol_switch=ProtocolSwitchStrategy(spec),
        model_fallback=ModelFallbackStrategy(spec, router=Router(creds)),
    )
    return WireProvider(spec, codec, transports, fallback=plan, recovery=None)


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, ev: object) -> None:
        self.events.append(ev)

    def kinds(self) -> list[str]:
        return [
            e.payload.kind  # type: ignore[attr-defined]
            for e in self.events
            if e.type is EventType.COMPACTING  # type: ignore[attr-defined]
        ]


def _u(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))


def _a(text: str) -> Message:
    return Message(role="assistant", content=(TextBlock(text=text),))


# A realistic multi-turn coding conversation (no tool-noise messages, so micro
# folding has nothing to fold and the cascade must escalate to a real summary).
def conversation() -> tuple[Message, ...]:
    return (
        _u("I'm building a Python CLI tool called 'ledger' that parses bank CSV "
           "exports into a normalised SQLite database. The CSV columns are date, "
           "description, amount, balance. Start by scaffolding the project."),
        _a("Created src/ledger/__init__.py, src/ledger/cli.py with an argparse "
           "entrypoint, and pyproject.toml declaring the console_scripts entry "
           "point 'ledger = ledger.cli:main'. Python 3.11+."),
        _u("Add a parser module that reads the CSV and yields Transaction "
           "dataclasses. Dates are in DD/MM/YYYY format and amounts use commas as "
           "thousands separators."),
        _a("Added src/ledger/parser.py with a Transaction dataclass (date: "
           "datetime.date, description: str, amount: Decimal, balance: Decimal) "
           "and parse_csv(path) that strips commas, parses DD/MM/YYYY with "
           "datetime.strptime, and converts amounts to Decimal."),
        _u("Now persist transactions into SQLite. Use a table 'txn' keyed by a "
           "content hash so re-imports are idempotent."),
        _a("Added src/ledger/store.py: open_db(path) creates table txn(hash TEXT "
           "PRIMARY KEY, date TEXT, description TEXT, amount TEXT, balance TEXT). "
           "insert_many uses INSERT OR IGNORE; the hash is sha256 of "
           "date|description|amount."),
        _u("There's a bug: importing the same file twice doubles the row count. "
           "Investigate."),
        _a("Root cause found: the hash omitted the balance field, so two "
           "transactions with identical date/description/amount but different "
           "running balances collided. Fixed by including balance in the hash "
           "input in store.py line 41."),
        _u("Good. Add a 'ledger report --month YYYY-MM' subcommand that prints "
           "total inflow, outflow, and net for the month."),
        _a("Added the report subcommand in cli.py calling store.month_summary "
           "which runs SELECT with substr(date,1,7)=? and sums positive vs "
           "negative amounts. Output is formatted with a thousands separator."),
    )


async def test_force_compact(pol_cfg: CompactionConfig) -> bool:
    print("\n=== TEST 1: force_compact -> real LLM meta-summary ===")
    pol = CompactionPolicy(build_provider(), pol_cfg)
    msgs = conversation()
    sink = _Sink()
    before = pol.estimate(msgs)
    out = await pol.force_compact(msgs, sink)
    print(f"  est_before={before}  tokens_before={out.tokens_before} "
          f"tokens_after={out.tokens_after}  kind={out.kind}")
    print(f"  events={sink.kinds()}")
    print(f"  --- summary (first 600 chars) ---\n{out.summary[:600]}")
    # NOTE: token reduction is only meaningful on a LARGE history; on this small
    # 10-message sample a faithful summary can be longer than the raw input, so
    # we assert on summary *correctness* (marker + verbatim facts preserved)
    # rather than on raw shrinkage. Real shrinkage is exercised by the loop on
    # full-window histories; see Test 2 for the in-band cascade behaviour.
    facts = ("ledger", "txn", "DD/MM/YYYY", "sha256", "balance", "month_summary")
    preserved = [f for f in facts if f in out.summary]
    print(f"  preserved facts: {preserved}")
    ok = (
        out.changed
        and out.kind == "overflow"
        and out.summary.startswith(_SUMMARY_MARKER)
        # the summariser must preserve several concrete facts verbatim
        and len(preserved) >= 3
        # and the protected tail must remain verbatim after the summary
        and len(out.messages) >= 2
        and out.messages[0] is not None
    )
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_auto_escalation() -> bool:
    print("\n=== TEST 2: maybe_compact auto-escalates to LLM summary (#1) ===")
    msgs = conversation()
    # Size the window so the conversation sits in [compact, truncate): above the
    # meta threshold but below the hard ceiling, with no foldable tool-noise.
    raw = sum(len(b.text) for m in msgs for b in m.content if isinstance(b, TextBlock))  # type: ignore[union-attr]
    est = raw // 3
    window = int(est / 0.85)  # est ~= 0.85 * window -> between 0.82 and 0.90
    cfg = CompactionConfig(
        context_window=window,
        micro_ratio=0.50,
        compact_ratio=0.82,
        truncate_ratio=0.90,
        protected_tail=2,
        micro_keep_tail=2,
    )
    pol = CompactionPolicy(build_provider(), cfg)
    print(f"  est={pol.estimate(msgs)} window={window} "
          f"micro@{cfg.micro_threshold()} meta@{cfg.compact_threshold()} "
          f"hard@{cfg.truncate_threshold()}")
    sink = _Sink()
    out = await pol.maybe_compact(msgs, sink)
    print(f"  kind={out.kind} events={sink.kinds()} "
          f"before={out.tokens_before} after={out.tokens_after}")
    ok = out.changed and out.kind == "overflow" and "overflow" in sink.kinds()
    print(f"  -> {'PASS' if ok else 'FAIL'} (expected an LLM summary, not truncate)")
    return ok


class _RecordingProvider:
    """Wraps a real provider, recording the messages of each stream() request so
    we can assert what the summariser actually received."""

    def __init__(self, inner: WireProvider) -> None:
        self._inner = inner
        self.sent_messages: list[tuple[Message, ...]] = []

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        return self._inner.capabilities

    async def stream(self, req, signal):  # type: ignore[no-untyped-def]
        self.sent_messages.append(tuple(req.messages))
        async for ev in self._inner.stream(req, signal):
            yield ev

    async def aclose(self) -> None:
        await self._inner.aclose()


async def test_recompaction_dedup(pol_cfg: CompactionConfig) -> bool:
    print("\n=== TEST 3: re-compaction does not re-summarise prior summary (#3) ===")
    # First compaction -> produces a marker-tagged summary message.
    pol = CompactionPolicy(build_provider(), pol_cfg)
    out1 = await pol.force_compact(conversation(), _Sink())
    summary_msg = out1.messages[0]
    prior_text = summary_msg.content[0].text  # type: ignore[union-attr]

    # Second compaction on [prior_summary, ...more turns...] with a recording
    # provider so we can see exactly what was sent to the summariser.
    more = (
        _u("Add CSV export of the monthly report to a file."),
        _a("Added 'ledger export --month YYYY-MM --out FILE' writing the same "
           "month_summary rows as CSV via the csv module."),
        _u("Write unit tests for the parser covering the comma and DD/MM/YYYY "
           "edge cases."),
        _a("Added tests/test_parser.py with cases for '1,234.56' and '01/02/2024' "
           "asserting Decimal('1234.56') and date(2024, 2, 1)."),
    )
    msgs2 = (summary_msg,) + more
    rec = _RecordingProvider(build_provider())
    pol2 = CompactionPolicy(rec, pol_cfg)  # type: ignore[arg-type]
    out2 = await pol2.force_compact(msgs2, _Sink())

    sent_msgs = rec.sent_messages[-1] if rec.sent_messages else ()
    prior_in_sent = any(
        isinstance(b, TextBlock) and _SUMMARY_MARKER in b.text
        for m in sent_msgs for b in m.content
    )
    print(f"  prior summary chars={len(prior_text)}")
    print(f"  2nd-summary kind={out2.kind} "
          f"starts_marker={out2.summary.startswith(_SUMMARY_MARKER)}")
    print(f"  superseded block present: "
          f"{'[SUPERSEDED EARLIER SUMMARY]' in out2.summary}")
    print(f"  prior marker re-sent to summariser: {prior_in_sent} "
          f"(expected False)")
    ok = (
        out2.changed
        and out2.kind == "overflow"
        and out2.summary.startswith(_SUMMARY_MARKER)
        and "[SUPERSEDED EARLIER SUMMARY]" in out2.summary
        and prior_in_sent is False
    )
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


def big_conversation(repeat: int) -> tuple[Message, ...]:
    """A long history: the base conversation repeated with verbose assistant
    turns so the estimate is well into the multi-thousand-token range."""
    base = conversation()
    out: list[Message] = []
    for r in range(repeat):
        for m in base:
            txt = m.content[0].text  # type: ignore[union-attr]
            out.append(
                Message(role=m.role, content=(TextBlock(text=f"[round {r}] {txt} "
                        + "Further detail and discussion. " * 6),))
            )
    return tuple(out)


async def test_large_history_shrinks() -> bool:
    print("\n=== TEST 4: meta-summary actually SHRINKS a large history ===")
    msgs = big_conversation(repeat=6)  # ~60 verbose messages
    cfg = CompactionConfig(
        context_window=200_000, protected_tail=4, summary_budget=900,
    )
    pol = CompactionPolicy(build_provider(), cfg)
    before = pol.estimate(msgs)
    out = await pol.force_compact(msgs, _Sink())
    print(f"  messages {len(msgs)} -> {len(out.messages)}  "
          f"tokens {out.tokens_before} -> {out.tokens_after}")
    reduction = 0.0 if before <= 0 else (out.tokens_before - out.tokens_after) / out.tokens_before
    print(f"  reduction = {reduction:.1%}")
    ok = (
        out.changed
        and out.kind == "overflow"
        and out.tokens_after < out.tokens_before
        and len(out.messages) < len(msgs)
    )
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    print(f"OPENROUTER_API_KEY present: {bool(key)} (len={len(key)})  model={MODEL_ID}")
    if not key:
        print("ERROR: OPENROUTER_API_KEY not set")
        return 2

    # Force a meta-summary regardless of size for tests 1 & 3.
    force_cfg = CompactionConfig(
        context_window=2_000, micro_ratio=0.01, compact_ratio=0.02,
        truncate_ratio=0.99, protected_tail=2, summary_budget=1_500,
    )

    results: list[bool] = []
    results.append(await test_force_compact(force_cfg))
    results.append(await test_auto_escalation())
    results.append(await test_recompaction_dedup(force_cfg))
    results.append(await test_large_history_shrinks())

    print("\n=== SUMMARY ===")
    print(f"  passed {sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
