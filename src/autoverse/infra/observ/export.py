"""Export a TraceModel to a self-contained HTML or Markdown file (O4, §4.6).

Both are pure functions ``TraceModel -> str`` with no IO. The HTML output inlines
all CSS/JS/data so it is a single offline-openable file (no CDN/external assets).
Inlining is intentional here — *export* is single-file by design; the live viewer
(``viewer/server.py``) by contrast serves external static assets.
"""

from __future__ import annotations

import html
import json

from autoverse.infra.observ.meter import Meter
from autoverse.infra.observ.model import (
    CompactionSegment,
    ToolCallRecord,
    ToolDecision,
    TraceModel,
    TurnRecord,
)


def export_markdown(model: TraceModel) -> str:
    meter = _meter_for(model)
    cost = meter.total_cost()
    lines: list[str] = []
    lines.append(f"# Trace — {model.session_id}")
    lines.append("")
    lines.append(f"- Model: `{model.model}`")
    lines.append(f"- Created: {model.created_at}")
    lines.append(f"- End reason: {model.end_reason or 'n/a'}")
    lines.append(f"- Turns: {len(model.turns)}")
    usage = model.usage
    lines.append(
        f"- Tokens: in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_r={usage.cache_read_tokens} cache_w={usage.cache_write_tokens}"
    )
    lines.append(
        f"- Cost (USD): {('%.6f' % cost.total_usd) if cost.known else 'unknown'}"
    )
    if model.collector_errors:
        lines.append(f"- Collector errors: {model.collector_errors}")
        for diag in getattr(model, "collector_diagnostics", ()):
            if not isinstance(diag, dict):
                continue
            event_type = str(diag.get("event_type", "unknown"))
            error_class = str(diag.get("error_class", "unknown"))
            message = str(diag.get("message", ""))
            lines.append(f"  - {event_type}: {error_class}: {message}")
    lines.append("")
    for turn in model.turns:
        lines.extend(_md_turn(turn))
    if model.compaction_segments:
        lines.append("## Compaction / Clear")
        for seg in model.compaction_segments:
            lines.extend(_md_segment(seg))
    if model.session_errors:
        lines.append("## Session errors")
        for err in model.session_errors:
            lines.append(f"- [{err.phase.value or 'n/a'}] {err.message} ({err.error_class})")
    return "\n".join(lines) + "\n"


def _md_turn(turn: TurnRecord) -> list[str]:
    out = [f"## Turn {turn.turn_index} (model `{turn.model}`)"]
    if turn.llm is not None:
        out.append(
            f"- LLM: stop=`{turn.llm.stop_cause}` "
            f"tokens(in={turn.llm.usage.input_tokens}/out={turn.llm.usage.output_tokens})"
        )
        if turn.llm.text_preview:
            out.append(f"  - text: {turn.llm.text_preview[:200]!r}")
    for tool in turn.tools:
        out.append(_md_tool(tool))
    if turn.gate is not None:
        out.append(f"- Gate `{turn.gate.gate_name}`: {turn.gate.decision} — {turn.gate.reason}")
    for retry in turn.retries:
        out.append(
            f"- Retry #{retry.attempt}: {retry.error_class} "
            f"http={retry.http_status} next_transport={retry.next_transport}"
        )
    for err in turn.errors:
        out.append(f"- Error: {err.message} ({err.error_class}, kind={err.error_kind.value})")
    out.append("")
    return out


def _md_tool(tool: ToolCallRecord) -> str:
    status = "ok"
    if tool.decision is ToolDecision.DENY:
        status = f"DENIED ({tool.deny_reason})"
    elif tool.is_error:
        status = f"error/{tool.error_kind.value}: {tool.error_message}"
    return f"- Tool `{tool.name}` [{status}] ({tool.duration_ms:.0f} ms)"


def _md_segment(seg: CompactionSegment) -> list[str]:
    return [
        f"- {seg.kind.value} #{seg.index} (turns {seg.turn_start}–{seg.turn_end}), "
        f"{len(seg.messages)} folded message(s)"
    ]


def export_html(model: TraceModel) -> str:
    meter = _meter_for(model)
    payload = {
        "trace": model.to_dict(),
        "cost": meter.total_cost().to_dict(),
        "cost_by_model": {m: c.to_dict() for m, c in meter.cost_by_model().items()},
    }
    data_json = _embed_json(json.dumps(payload, ensure_ascii=False))
    title = html.escape(f"Trace {model.session_id}")
    rows = "".join(_html_turn_row(t) for t in model.turns)
    return _HTML_TEMPLATE.format(
        title=title,
        session=html.escape(model.session_id),
        model=html.escape(model.model),
        created=html.escape(model.created_at),
        end_reason=html.escape(model.end_reason or "n/a"),
        turns=len(model.turns),
        rows=rows,
        data_json=data_json,
    )


def _embed_json(data_json: str) -> str:
    """Make a JSON string safe to inline inside ``<script type=application/json>``.

    ``html.escape`` would corrupt the payload: a ``<script>`` element's
    ``textContent`` performs no HTML-entity decoding, so an escaped ``&quot;``
    reaches ``JSON.parse`` verbatim and throws. The only sequence that can break
    out of the data island is a literal ``</script>`` (and ``<!--``); neutralise
    those by escaping the ``<`` so the parser still sees identical JSON.
    """
    return data_json.replace("<", "\\u003c")


def _html_turn_row(turn: TurnRecord) -> str:
    tools = ", ".join(
        f"{html.escape(t.name)}"
        + (" (DENIED)" if t.decision is ToolDecision.DENY else "")
        + (" (error)" if t.is_error else "")
        for t in turn.tools
    )
    stop = html.escape(turn.llm.stop_cause if turn.llm else "")
    in_tok = turn.llm.usage.input_tokens if turn.llm else 0
    out_tok = turn.llm.usage.output_tokens if turn.llm else 0
    return (
        f"<tr><td>{turn.turn_index}</td><td>{html.escape(turn.model)}</td>"
        f"<td>{stop}</td><td>{in_tok}</td><td>{out_tok}</td>"
        f"<td>{tools}</td></tr>"
    )


def _meter_for(model: TraceModel) -> Meter:
    meter = Meter()
    for turn in model.turns:
        if turn.llm is not None:
            meter.record(turn.llm.model, turn.llm.usage)
    return meter


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:1.5rem;background:#fafafa;color:#222}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left;font-size:14px}}
th{{background:#eee}}
.summary{{margin-bottom:1rem}}
</style></head>
<body>
<h1>{title}</h1>
<div class="summary">
<p>Session: <code>{session}</code> · Model: <code>{model}</code></p>
<p>Created: {created} · End reason: {end_reason} · Turns: {turns}</p>
</div>
<table>
<thead><tr><th>Turn</th><th>Model</th><th>Stop</th><th>In</th><th>Out</th><th>Tools</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<script id="trace-data" type="application/json">{data_json}</script>
<script>
window.__TRACE__ = JSON.parse(document.getElementById('trace-data').textContent);
</script>
</body></html>
"""


__all__ = ["export_html", "export_markdown"]
