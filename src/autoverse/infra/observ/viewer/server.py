"""TraceViewer — read-only data API + external static SPA (docs/reference/events-and-storage.md §5).

Two hard rules (O5):

1. The server provides *data only* — it contains no embedded HTML/JS strings.
2. The SPA assets (``index.html`` / ``app.js``) are external files under
   ``static/`` and are read from disk, never embedded as Python literals.

The data API methods (:meth:`sessions`, :meth:`session`, :meth:`children`,
:meth:`cost`) are plain async functions returning JSON-serialisable dicts so
they can be unit-tested without binding a real socket. :meth:`handle` maps a
request path to a ``(status, content_type, body)`` triple, and
:meth:`make_handler` adapts it to ``http.server`` for a real server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from autoverse.infra.observ.meter import Meter
from autoverse.infra.observ.model import TraceModel
from autoverse.infra.observ.store import TraceStore


def static_dir() -> Path:
    """Absolute path to the external SPA asset directory."""
    return Path(__file__).resolve().parent / "static"


class TraceViewer:
    def __init__(self, store: TraceStore, webui_dir: Path | None = None) -> None:
        self._store = store
        self._webui = webui_dir or static_dir()

    # ── data API (testable without a socket) ─────────────────────────────────

    async def sessions(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for sid in await self._store.list_sessions():
            model = await self._store.load(sid)
            if model is None:
                continue
            usage = model.usage
            out.append(
                {
                    "session_id": sid,
                    "model": model.model,
                    "created_at": model.created_at,
                    "turns": len(model.turns),
                    "end_reason": model.end_reason,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "parent_session_id": None
                    if model.parent is None
                    else model.parent.parent_session_id,
                }
            )
        return out

    async def session(self, sid: str) -> dict[str, object] | None:
        model = await self._store.load(sid)
        if model is None:
            return None
        return model.to_dict()

    async def children(self, parent_sid: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for sid in await self._store.list_children(parent_sid):
            model = await self._store.load(sid)
            if model is not None:
                out.append(model.to_dict())
        return out

    async def cost(self, sid: str) -> dict[str, object] | None:
        model = await self._store.load(sid)
        if model is None:
            return None
        meter = _meter_for(model)
        return {
            "total": meter.total_cost().to_dict(),
            "by_model": {m: c.to_dict() for m, c in meter.cost_by_model().items()},
        }

    # ── routing ──────────────────────────────────────────────────────────────

    async def handle(self, path: str) -> tuple[int, str, bytes]:
        """Map *path* → (status, content_type, body). Used by the HTTP handler."""
        clean = path.split("?", 1)[0]
        if clean in ("/", "") or clean.startswith("/session/"):
            return self._static_file("index.html")
        if clean.startswith("/static/"):
            return self._static_file(clean[len("/static/"):])
        if clean == "/api/sessions" or clean == "/sessions":
            return _json(200, await self.sessions())
        if clean.startswith("/api/session/") or clean.startswith("/session/api/"):
            sid = clean.rsplit("/", 1)[-1]
            data = await self.session(sid)
            return _json(404, {"error": "not found"}) if data is None else _json(200, data)
        if clean.startswith("/api/cost/"):
            sid = clean.rsplit("/", 1)[-1]
            data = await self.cost(sid)
            return _json(404, {"error": "not found"}) if data is None else _json(200, data)
        if clean.startswith("/api/children/"):
            sid = clean.rsplit("/", 1)[-1]
            return _json(200, await self.children(sid))
        return _json(404, {"error": "not found"})

    def _static_file(self, rel: str) -> tuple[int, str, bytes]:
        # Read SPA assets from disk — never embedded Python strings (O5).
        target = (self._webui / rel).resolve()
        if not str(target).startswith(str(self._webui.resolve())):
            return 403, "text/plain", b"forbidden"
        if not target.is_file():
            return 404, "text/plain", b"not found"
        return 200, _content_type(target), target.read_bytes()

    def make_handler(self) -> Callable[..., object]:
        """Build a ``BaseHTTPRequestHandler`` subclass bound to this viewer."""
        import asyncio
        from http.server import BaseHTTPRequestHandler

        viewer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — stdlib naming
                status, ctype, body = asyncio.run(viewer.handle(self.path))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return None

        return _Handler


def _json(status: int, data: object) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(data, ensure_ascii=False).encode("utf-8")


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
    }.get(suffix, "application/octet-stream")


def _meter_for(model: TraceModel) -> Meter:
    meter = Meter()
    for turn in model.turns:
        if turn.llm is not None:
            meter.record(turn.llm.model, turn.llm.usage)
    return meter


__all__ = ["TraceViewer", "static_dir"]
