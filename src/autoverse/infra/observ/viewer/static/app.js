// AutoVerse trace viewer SPA — consumes the read-only data API (docs/reference/events-and-storage.md §5.2).
// Pure static asset; no build chain.
"use strict";

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} → ${resp.status}`);
  return resp.json();
}

function renderSessions(sessions) {
  const el = document.getElementById("sessions");
  if (!sessions.length) { el.textContent = "No sessions."; return; }
  const rows = sessions.map((s) =>
    `<tr><td><a href="#${s.session_id}">${s.session_id}</a></td>` +
    `<td>${s.model}</td><td>${s.turns}</td>` +
    `<td>${s.input_tokens}/${s.output_tokens}</td>` +
    `<td>${s.end_reason || ""}</td></tr>`
  ).join("");
  el.innerHTML =
    `<table><thead><tr><th>Session</th><th>Model</th><th>Turns</th>` +
    `<th>In/Out</th><th>End</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDetail(trace) {
  const el = document.getElementById("detail");
  const turns = (trace.turns || []).map((t) => {
    const tools = (t.tools || []).map((tool) => {
      let cls = "";
      if (tool.decision === "deny") cls = "denied";
      else if (tool.is_error) cls = "error";
      return `<span class="${cls}">${tool.name}</span>`;
    }).join(", ");
    const stop = t.llm ? t.llm.stop_cause : "";
    return `<tr><td>${t.turn_index}</td><td>${t.model}</td>` +
      `<td>${stop}</td><td>${tools}</td></tr>`;
  }).join("");
  el.innerHTML =
    `<h2>Session ${trace.session_id}</h2>` +
    `<table><thead><tr><th>Turn</th><th>Model</th><th>Stop</th>` +
    `<th>Tools</th></tr></thead><tbody>${turns}</tbody></table>`;
}

async function loadDetail(sid) {
  try {
    const trace = await getJSON(`/api/session/${sid}`);
    renderDetail(trace);
  } catch (e) {
    document.getElementById("detail").textContent = String(e);
  }
}

async function main() {
  try {
    const sessions = await getJSON("/api/sessions");
    renderSessions(sessions);
  } catch (e) {
    document.getElementById("sessions").textContent = String(e);
  }
  if (location.hash.length > 1) loadDetail(location.hash.slice(1));
  window.addEventListener("hashchange", () => {
    if (location.hash.length > 1) loadDetail(location.hash.slice(1));
  });
}

main();
