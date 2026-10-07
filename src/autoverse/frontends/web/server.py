"""FastAPI server — the WebUI's transport shell (docs/guides/using-webui.md §6).

This is the *only* AutoVerse-specific transport code in the Web tier: it wires a
shared :class:`SessionManager` (engines + rail) and exposes it over REST + SSE.
Every meaningful decision (block shapes, derived state, command dispatch, plan /
approval flow) lives in the reused engine + ``TuiEventSink`` + ``WebLog`` stack,
so the routes here stay thin:

* ``GET  /``                                  → the single-page app shell
* ``GET  /theme.css`` · ``/theme.json``       → live theme (theme.py is source)
* ``GET  /api/sessions``                      → rail listing (StoreSessionQuery)
* ``POST /api/sessions``                      → open / resume a session
* ``DELETE /api/sessions/{sid}``              → delete · ``/rename`` · ``/pin``
* ``GET  /api/sessions/{sid}/stream``         → SSE (snapshot then block deltas)
* ``GET  /api/sessions/{sid}/history``        → bounded older block page
* ``GET  /api/sessions/{sid}/resources/...``  → lazy replay image bytes
* ``WS   /api/browser/host``                  → native visible-browser bridge
* ``POST /api/sessions/{sid}/message``        → run a turn (or a /slash command)
* ``POST .../abort`` · ``/compact`` · ``/clear`` · ``/model``
* ``POST .../approval`` · ``/plan``           → resolve a flyout / plan card
* ``GET  .../agent/{job}``                    → sub-agent drill-down (AgentView)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from autoverse.frontends.repl.theme import Theme
from autoverse.frontends.web.bridge import SessionBridge
from autoverse.frontends.web.files import (
    FileAccessError,
    delete_file,
    list_directory_payload,
    raw_file_info,
    read_file_preview_payload,
    rename_file,
)
from autoverse.frontends.web.session import SessionManager
from autoverse.frontends.web.theme_export import theme_config, theme_css
from autoverse.infra.config.writer import (
    ConfigPersistError,
    merge_into_config_file,
    resolve_global_config_path,
)
from autoverse.infra.observ.logging import configure_from_config, log_context
from autoverse.types import ImageBlock

_STATIC = Path(__file__).parent / "static"
_MAX_IMAGE_ATTACHMENTS = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_LOG = logging.getLogger(__name__)
_SESSION_PATH_RE = re.compile(r"/api/sessions/([^/]+)")


def _asset_token() -> str:
    """A short cache-busting token that changes whenever a bundled static asset
    is edited (derived from file mtimes), so the browser re-fetches app.js /
    theme.css instead of serving a stale heuristic-cached copy."""
    parts: list[str] = []
    for name in ("app.js", "index.html"):
        try:
            parts.append(str(int((_STATIC / name).stat().st_mtime)))
        except OSError:
            parts.append("0")
    return "-".join(parts)


def _resolve_model(model_arg: str | None, cwd: Path) -> str:
    if model_arg is not None:
        return model_arg
    try:
        from autoverse.infra.config import load_config

        configured = load_config(cwd, os.environ, {}).config.model.model_id
    except Exception:  # noqa: BLE001
        configured = ""
    return configured or "echo"


def _context_window(model: str) -> int:
    try:
        from autoverse.infra.llm.registry import get_context_window, resolve_alias

        return get_context_window(resolve_alias(model))
    except Exception:  # pragma: no cover - defensive
        return 0


def _persist_global_config(changes: dict[str, str]) -> None:
    """Merge *changes* (dotted key → value) into the global config file.

    Best-effort: failures (missing home dir, corrupt file, etc.) are silently
    ignored — the in-memory session config is already updated, so the user's
    current session works regardless.
    """
    try:
        merge_into_config_file(resolve_global_config_path(), changes)
    except (ConfigPersistError, OSError, ValueError):
        pass


def _image_attachments(raw: object) -> tuple[ImageBlock, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="attachments must be a list")
    if len(raw) > _MAX_IMAGE_ATTACHMENTS:
        raise HTTPException(
            status_code=413,
            detail=f"at most {_MAX_IMAGE_ATTACHMENTS} images may be attached",
        )
    images: list[ImageBlock] = []
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="attachment must be an object")
        media_type = str(item.get("media_type") or item.get("mime") or "").lower()
        data = str(item.get("data") or "")
        if data.startswith("data:"):
            header, _, payload = data.partition(",")
            if not payload or ";base64" not in header:
                raise HTTPException(status_code=400, detail="image data URL must be base64")
            media_type = header[5:].split(";", 1)[0].lower()
            data = payload
        if media_type not in _ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=400, detail=f"unsupported image type: {media_type}")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="image data must be valid base64") from exc
        size = len(decoded)
        if size > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image attachment is too large")
        total += size
        if total > _MAX_TOTAL_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="image attachments are too large")
        images.append(ImageBlock(media_type=media_type, data=data))
    return tuple(images)


def _validate_cwd(raw: object) -> str | None:
    """Validate an optional new-session working directory from the request body.

    ``None``/empty → use the server default (returns ``None``). A non-empty value
    must resolve to an existing directory, otherwise the request is rejected so
    the user gets immediate feedback instead of a session silently created in the
    wrong place.
    """
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="cwd must be a string")
    try:
        path = Path(raw).expanduser()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid working directory") from exc
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"directory does not exist: {raw}")
    return str(path.resolve())


def _build_manager(*, model: str, cwd: Path, theme: Theme) -> SessionManager:
    from autoverse.app.session_query import StoreSessionQuery
    from autoverse.frontends.repl.builtin_cmds import build_default_registry
    from autoverse.infra.persist import JSONLStore, SQLiteSessionCatalog

    registry = build_default_registry()
    try:
        from autoverse.frontends.repl.commands_loader import register_file_commands

        register_file_commands(registry, cwd)
    except Exception:  # pragma: no cover - a bad command file must not block startup
        pass

    store: Any = None
    session_query: Any = None
    session_catalog: Any = None
    data_dir: Path | None = None
    try:
        from autoverse.infra.config import load_config

        session_dir = load_config(cwd, os.environ, {}).config.session_dir
        session_root = Path(session_dir).expanduser()
        data_dir = session_root.parent
        store = JSONLStore(session_root)
        session_catalog = SQLiteSessionCatalog(
            data_dir / "session-catalog.sqlite3"
        )
        session_query = StoreSessionQuery(
            store,
            base_dir=session_root,
            catalog=session_catalog,
        )
    except Exception:  # pragma: no cover - degrade to in-memory-only sessions
        store = None
        session_query = None

    version = getattr(__import__("autoverse"), "__version__", "0.1.0")
    return SessionManager(
        cwd=cwd,
        model=model,
        theme=theme,
        version=version,
        context_window=_context_window(model),
        registry=registry,
        store=store,
        session_query=session_query,
        session_catalog=session_catalog,
        data_dir=data_dir,
    )


def create_app(
    *,
    model: str | None = None,
    cwd: str | None = None,
    theme_name: str | None = None,
) -> FastAPI:
    work_dir = Path(cwd).expanduser() if cwd else Path.cwd()
    try:
        from autoverse.infra.config import load_config

        config = load_config(work_dir, os.environ, {}).config
    except Exception:  # noqa: BLE001 - logging must still come up with defaults
        from autoverse.app.config import Config

        config = Config()
    data_dir = Path(config.session_dir).expanduser().parent
    log_path = configure_from_config(config, data_dir, frontend="web")
    eff_model = _resolve_model(model, work_dir)
    theme = Theme.load(theme_name)
    manager = _build_manager(model=eff_model, cwd=work_dir, theme=theme)
    if log_path is not None:
        _LOG.info("WebUI logging initialized at %s", log_path)

    app = FastAPI(title="AutoVerse WebUI", version=manager.version)

    @app.middleware("http")
    async def _log_request_errors(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        sid = _session_id_from_path(request.url.path)
        with log_context(session_id=sid, frontend="web"):
            try:
                return await call_next(request)
            except Exception:  # noqa: BLE001 — log then preserve FastAPI error flow
                _LOG.exception("Unhandled WebUI request error: %s %s", request.method, request.url.path)
                raise

    # ── static + theme ────────────────────────────────────────────────────────
    @app.get("/")
    async def index() -> Response:
        return _index_response()

    def _index_response() -> Response:
        idx = _STATIC / "index.html"
        if not idx.exists():
            return PlainTextResponse(
                "AutoVerse WebUI: static assets not built.", status_code=404
            )
        # Stamp the app.js / theme.css URLs with a token derived from the static
        # assets' mtimes and serve the index uncached. The browser revalidates
        # the (tiny) index every load, and whenever a static file changes its
        # token changes so the new asset URL is fetched fresh — without this,
        # StaticFiles sends no Cache-Control and browsers heuristically cache
        # app.js, so a code fix (e.g. plan-card locking) silently never loads.
        try:
            html = idx.read_text(encoding="utf-8")
        except OSError:
            return FileResponse(idx)
        token = _asset_token()
        html = html.replace(
            'src="/static/app.js"', f'src="/static/app.js?v={token}"'
        ).replace('href="/theme.css"', f'href="/theme.css?v={token}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/theme.css")
    async def theme_stylesheet() -> Response:
        return Response(theme_css(theme), media_type="text/css")

    @app.get("/theme.json")
    async def theme_json() -> Response:
        return JSONResponse(theme_config(theme, version=manager.version))

    @app.websocket("/api/browser/host")
    async def native_browser_host(websocket: WebSocket) -> None:
        """Attach the Apple app's visible WKWebView as the browser tool host."""

        await manager.browser_host.serve(
            websocket,
            on_connected=manager.bind_native_browser_to_active_sessions,
        )

    @app.get("/api/browser/status")
    async def native_browser_status() -> Response:
        return JSONResponse({"connected": manager.browser_host.connected})

    # ── command palette + model picker (shared registry is the source) ─────────
    @app.get("/api/commands")
    async def list_commands() -> Response:
        items = manager.registry.completion_items()
        cmds = [{"name": n, "summary": s, "hints": list(h)} for (n, s, h) in items]
        return JSONResponse({"commands": cmds})

    @app.get("/api/models")
    async def list_models() -> Response:
        try:
            from autoverse.infra.llm.registry import MODEL_OPTIONS

            opts = [
                {
                    "model_id": o["model_id"],
                    "label": o.get("label", o["model_id"]),
                    "source": o.get("source", ""),
                    "input_price_per_mtok": o.get("input_price_per_mtok", 0.0),
                    "output_price_per_mtok": o.get("output_price_per_mtok", 0.0),
                    "vision": o.get("vision", False),
                }
                for o in MODEL_OPTIONS
            ]
        except Exception:  # pragma: no cover - defensive
            opts = []
        return JSONResponse({"models": opts, "current": manager.model})

    @app.get("/api/secrets")
    async def list_secrets(session_id: str | None = None) -> Response:
        return JSONResponse(manager.secret_status(session_id))

    # ── extensions: skills/plugins management (shared admin service) ──────────
    @app.get("/api/extensions/skills")
    async def list_extension_skills(session_id: str) -> Response:
        admin = _extension_admin(_existing(manager, session_id))
        return JSONResponse({"skills": [_ext_item(i) for i in admin.list_skills()]})

    @app.get("/api/extensions/skills/{skill_id}")
    async def get_extension_skill(skill_id: str, session_id: str) -> Response:
        admin = _extension_admin(_existing(manager, session_id))
        detail = admin.get_skill(skill_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="skill not found")
        return JSONResponse(_ext_detail(detail))

    @app.post("/api/extensions/skills/{skill_id}/doctor")
    async def doctor_extension_skill(skill_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        return JSONResponse({"text": admin.diagnose_skill(skill_id, body.get("prompt"))})

    @app.post("/api/extensions/skills/{skill_id}/enable")
    async def enable_extension_skill(skill_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        admin.enable_skill(skill_id)
        return JSONResponse({"ok": True})

    @app.post("/api/extensions/skills/{skill_id}/disable")
    async def disable_extension_skill(skill_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        admin.disable_skill(skill_id)
        return JSONResponse({"ok": True})

    @app.post("/api/extensions/skills/{skill_id}/trust")
    async def trust_extension_skill(skill_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        admin.trust_skill(skill_id)
        return JSONResponse({"ok": True})

    @app.get("/api/extensions/plugins")
    async def list_extension_plugins(session_id: str) -> Response:
        admin = _extension_admin(_existing(manager, session_id))
        return JSONResponse({"plugins": [_ext_item(i) for i in admin.list_plugins()]})

    @app.get("/api/extensions/plugins/{plugin_id}")
    async def get_extension_plugin(plugin_id: str, session_id: str) -> Response:
        admin = _extension_admin(_existing(manager, session_id))
        detail = admin.get_plugin(plugin_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="plugin not found")
        return JSONResponse(_ext_detail(detail))

    @app.post("/api/extensions/plugins/{plugin_id}/doctor")
    async def doctor_extension_plugin(plugin_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        return JSONResponse({"text": admin.diagnose_plugin(plugin_id)})

    @app.post("/api/extensions/plugins/{plugin_id}/enable")
    async def enable_extension_plugin(plugin_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        admin.enable_plugin(plugin_id)
        return JSONResponse({"ok": True})

    @app.post("/api/extensions/plugins/{plugin_id}/disable")
    async def disable_extension_plugin(plugin_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        admin.disable_plugin(plugin_id)
        return JSONResponse({"ok": True})

    @app.post("/api/extensions/plugins/{plugin_id}/trust")
    async def trust_extension_plugin(plugin_id: str, request: Request) -> Response:
        body = await _json(request)
        admin = _extension_admin(_existing(manager, str(body.get("session_id", ""))))
        admin.trust_plugin(plugin_id)
        return JSONResponse({"ok": True})

    @app.get("/api/providers/account")
    async def provider_account(key: str, session_id: str | None = None) -> Response:
        if not key:
            raise HTTPException(status_code=400, detail="key is required")
        return JSONResponse(await manager.provider_account_status(key, session_id))

    @app.post("/api/secrets/session")
    async def set_session_secret(request: Request) -> Response:
        body = await _json(request)
        sid = str(body.get("session_id", "") or "")
        key = str(body.get("key", "") or "")
        value = str(body.get("value", "") or "")
        if not sid:
            raise HTTPException(status_code=400, detail="session_id is required")
        if not key:
            raise HTTPException(status_code=400, detail="key is required")
        manager.set_session_secret(sid, key, value)
        return JSONResponse({"ok": True, **manager.secret_status(sid)})

    @app.delete("/api/secrets/session/{key}")
    async def clear_session_secret(key: str, session_id: str | None = None) -> Response:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        ok = manager.clear_session_secret(session_id, key)
        return JSONResponse({"ok": ok, **manager.secret_status(session_id)})

    @app.post("/api/secrets/vault")
    async def set_vault_secret(request: Request) -> Response:
        body = await _json(request)
        key = str(body.get("key", "") or "")
        value = str(body.get("value", "") or "")
        if not key:
            raise HTTPException(status_code=400, detail="key is required")
        if not value:
            raise HTTPException(status_code=400, detail="value is required")
        try:
            manager.set_vault_secret(key, value)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **manager.secret_status(str(body.get("session_id", "") or ""))})

    @app.delete("/api/secrets/vault/{key}")
    async def delete_vault_secret(key: str, session_id: str | None = None) -> Response:
        ok = manager.delete_vault_secret(key)
        return JSONResponse({"ok": ok, **manager.secret_status(session_id)})

    # ── session rail (CRUD) ────────────────────────────────────────────────────
    @app.get("/api/sessions")
    async def list_sessions() -> Response:
        query_started = time.perf_counter()
        sessions = await manager.list_sessions()
        query_ms = (time.perf_counter() - query_started) * 1000.0
        serialization_started = time.perf_counter()
        response = JSONResponse({"sessions": sessions})
        serialization_ms = (time.perf_counter() - serialization_started) * 1000.0
        response.headers["Server-Timing"] = (
            f"catalog-query;dur={query_ms:.2f}, "
            f"serialization;dur={serialization_ms:.2f}"
        )
        return response

    @app.get("/api/workspace")
    async def workspace() -> Response:
        return JSONResponse({"default_cwd": str(manager.cwd)})

    @app.get("/api/sessions/{sid}/files")
    async def list_session_files(
        sid: str, path: str = ".", show_hidden: bool = False
    ) -> Response:
        session = _existing(manager, sid)
        try:
            payload = await asyncio.to_thread(
                list_directory_payload,
                Path(session.cwd),
                path,
                show_hidden=show_hidden,
            )
        except FileAccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return JSONResponse(payload)

    @app.get("/api/sessions/{sid}/file")
    async def read_session_file(sid: str, path: str) -> Response:
        session = _existing(manager, sid)
        try:
            payload = await asyncio.to_thread(
                read_file_preview_payload,
                Path(session.cwd),
                path,
                allowed_external_paths=session.external_file_preview_paths(),
            )
        except FileAccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return JSONResponse(payload)

    @app.get("/api/sessions/{sid}/file/raw")
    async def read_session_file_raw(
        sid: str, path: str, download: bool = False
    ) -> Response:
        session = _existing(manager, sid)
        try:
            file_path, media_type, filename = await asyncio.to_thread(
                raw_file_info,
                Path(session.cwd),
                path,
                allowed_external_paths=session.external_file_preview_paths(),
            )
        except FileAccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        headers = {"X-Content-Type-Options": "nosniff"}
        if media_type == "image/svg+xml":
            headers["Content-Security-Policy"] = "sandbox"
        return FileResponse(
            file_path,
            media_type=media_type,
            filename=filename,
            content_disposition_type="attachment" if download else "inline",
            headers=headers,
        )

    @app.post("/api/sessions/{sid}/file/rename")
    async def rename_session_file(sid: str, request: Request) -> Response:
        session = _existing(manager, sid)
        body = await _json(request)
        new_name = str(body.get("new_name", ""))
        if not new_name:
            raise HTTPException(status_code=400, detail="new_name is required")
        try:
            payload = await asyncio.to_thread(
                rename_file, Path(session.cwd), str(body.get("path", "")), new_name
            )
        except FileAccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return JSONResponse(payload)

    @app.delete("/api/sessions/{sid}/file")
    async def delete_session_file(sid: str, request: Request) -> Response:
        session = _existing(manager, sid)
        path = request.query_params.get("path", "")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        try:
            payload = await asyncio.to_thread(
                delete_file, Path(session.cwd), path
            )
        except FileAccessError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return JSONResponse(payload)

    @app.post("/api/sessions")
    async def open_session(request: Request) -> Response:
        started = time.perf_counter()
        body = await _json(request)
        sid = body.get("session_id") or None
        cwd = _validate_cwd(body.get("cwd")) if not sid else None
        session = await manager.get_or_create_view(sid, cwd=cwd)
        # Switching into an existing session means the user has seen its latest
        # activity → clear the unseen bit so the rail's done/failed badge drops
        # (parity with the TUI's _mark_viewed on switch-in, §5.6.5).
        if sid:
            await session.mark_viewed()
        serialization_started = time.perf_counter()
        response = JSONResponse(
            {"session_id": session.session_id, "meta": session.bridge.meta}
        )
        serialization_ms = (time.perf_counter() - serialization_started) * 1000.0
        response.headers["Server-Timing"] = (
            f"session-open;dur={(time.perf_counter() - started) * 1000.0:.2f}, "
            f"history-load;dur={session._history_load_ms:.2f}, "
            f"serialization;dur={serialization_ms:.2f}"
        )
        return response

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str) -> Response:
        await manager.delete(sid)
        return JSONResponse({"ok": True})

    @app.post("/api/sessions/{sid}/rename")
    async def rename_session(sid: str, request: Request) -> Response:
        body = await _json(request)
        await manager.rename(sid, str(body.get("title", "")))
        return JSONResponse({"ok": True})

    @app.post("/api/sessions/{sid}/pin")
    async def pin_session(sid: str, request: Request) -> Response:
        body = await _json(request)
        await manager.set_pinned(sid, bool(body.get("pinned", True)))
        return JSONResponse({"ok": True})

    @app.get("/api/sessions/{sid}/history")
    async def session_history(sid: str, before: str = "") -> Response:
        session = _existing(manager, sid)
        history_started = time.perf_counter()
        payload = await session.history_page(before)
        history_ms = (time.perf_counter() - history_started) * 1000.0
        serialization_started = time.perf_counter()
        response = JSONResponse(payload)
        serialization_ms = (time.perf_counter() - serialization_started) * 1000.0
        response.headers["Server-Timing"] = (
            f"history-page;dur={history_ms:.2f}, "
            f"serialization;dur={serialization_ms:.2f}"
        )
        return response

    @app.get("/api/sessions/{sid}/resources/{block_id}/{index}")
    async def session_resource(sid: str, block_id: str, index: int) -> Response:
        session = _existing(manager, sid)
        try:
            payload, media_type = session.history_resource(block_id, index)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="session resource not found"
            ) from exc
        return Response(
            payload,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/sessions/{sid}/blocks/{block_id}")
    async def session_block_detail(sid: str, block_id: str) -> Response:
        """Expand one oversized replay block on explicit user request."""
        session = _existing(manager, sid)
        try:
            payload = session.history_block_detail(block_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="session block not found"
            ) from exc
        return JSONResponse(payload)

    @app.get("/api/sessions/{sid}/ranges/{start}/{end}")
    async def session_range_detail(
        sid: str, start: int, end: int, page: str = ""
    ) -> Response:
        """Expand a turn range that was summarized to keep replay bounded."""
        session = _existing(manager, sid)
        return JSONResponse(session.history_range_detail(start, end, page))

    # ── SSE stream (snapshot then deltas) ──────────────────────────────────────
    @app.get("/api/sessions/{sid}/stream")
    async def stream(sid: str, request: Request) -> StreamingResponse:
        session = await manager.get_or_create_view(sid)
        bridge = session.bridge
        return StreamingResponse(
            _sse(bridge, request), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── turn / command ──────────────────────────────────────────────────────────
    @app.post("/api/sessions/{sid}/message")
    async def message(sid: str, request: Request) -> Response:
        body = await _json(request)
        text = str(body.get("text", ""))
        attachments = _image_attachments(body.get("attachments"))
        activation_started = time.perf_counter()
        session = await manager.activate(sid)
        activation_ms = (time.perf_counter() - activation_started) * 1000.0
        if text.lstrip().startswith("/"):
            out = await session.run_command(text.lstrip())
            response = JSONResponse({"ok": True, **out})
            response.headers["Server-Timing"] = f"engine-activation;dur={activation_ms:.2f}"
            return response
        # Start a turn, or queue this as a follow-up if one is already running (R15).
        out = session.submit(text, attachments)
        response = JSONResponse({"ok": True, **out})
        response.headers["Server-Timing"] = f"engine-activation;dur={activation_ms:.2f}"
        return response

    @app.post("/api/sessions/{sid}/queue/cancel")
    async def cancel_queued(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = _existing(manager, sid)
        session.cancel_queued(str(body.get("id", "")))
        return JSONResponse({"ok": True})

    @app.post("/api/sessions/{sid}/command")
    async def command(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = await manager.activate(sid)
        out = await session.run_command(str(body.get("command", "")))
        return JSONResponse({"ok": True, **out})

    @app.post("/api/sessions/{sid}/abort")
    async def abort(sid: str) -> Response:
        session = _existing(manager, sid)
        session.abort()
        return JSONResponse({"ok": True})

    @app.post("/api/sessions/{sid}/turn/rewind")
    async def rewind_turn(sid: str, request: Request) -> Response:
        body = await _json(request)
        rewind_id = str(body.get("rewind_id", ""))
        if not rewind_id:
            return JSONResponse(
                {"ok": False, "detail": "missing rewind_id"}, status_code=400
            )
        session = _existing(manager, sid)
        try:
            result = await session.rewind_turn_for_edit(rewind_id)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/sessions/{sid}/compact")
    async def compact(sid: str) -> Response:
        session = await manager.activate(sid)
        return JSONResponse({"ok": True, "result": await session.compact()})

    @app.post("/api/sessions/{sid}/clear")
    async def clear(sid: str) -> Response:
        session = await manager.activate(sid)
        return JSONResponse({"ok": True, "cleared": await session.clear()})

    @app.post("/api/sessions/{sid}/model")
    async def switch_model(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = await manager.activate(sid)
        model = str(body.get("model", ""))
        session.switch_model(model)
        # Persist as global default so new sessions inherit the preference.
        if model:
            manager.model = model
            if manager.store is not None:
                try:
                    await manager.store.update_meta(sid, model=model)
                except Exception:  # noqa: BLE001 - active switch remains valid
                    pass
            _persist_global_config({"model.model_id": model})
        return JSONResponse({"ok": True, "model": session.model})

    @app.post("/api/sessions/{sid}/mode")
    async def switch_mode(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = await manager.activate(sid)
        target = body.get("target")
        direction = str(body.get("direction") or "forward")
        exit_action = str(body.get("exit_action") or "auto")
        out = await session.switch_mode(
            str(target) if target is not None else None,
            direction=direction,
            exit_action=exit_action,
        )
        return JSONResponse(out)

    @app.get("/api/sessions/{sid}/tiers")
    async def get_tiers(sid: str) -> Response:
        session = await manager.activate(sid)
        return JSONResponse(
            {
                "tiers": session.model_tiers(),
                "main": session.model,
                "available": session.model_availability(),
            }
        )

    @app.post("/api/sessions/{sid}/tiers")
    async def set_tiers(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = await manager.activate(sid)
        picks = {
            "deep": str(body.get("deep", "") or ""),
            "balanced": str(body.get("balanced", "") or ""),
            "fast": str(body.get("fast", "") or ""),
        }
        tiers = session.set_model_tiers(picks)
        # Persist tiers as global defaults so new sessions inherit them.
        tier_changes = {
            f"model_tiers.{k}": v for k, v in picks.items()
        }
        _persist_global_config(tier_changes)
        return JSONResponse(
            {
                "ok": True,
                "tiers": tiers,
                "main": session.model,
                "available": session.model_availability(),
            }
        )

    # ── approval / plan ──────────────────────────────────────────────────────────
    @app.post("/api/sessions/{sid}/approval")
    async def approval(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = _existing(manager, sid)
        ok = session.on_approval_reply(
            str(body.get("call_id", "")),
            str(body.get("action", "deny")),
            str(body.get("scope", "once")),
        )
        return JSONResponse({"ok": ok})

    @app.post("/api/sessions/{sid}/plan")
    async def plan(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = await manager.activate(sid)
        action = str(body.get("action", "cancel"))
        if action == "approve":
            asyncio.create_task(session.resolve_plan("approve"))
        else:
            await session.resolve_plan(action)
        return JSONResponse({"ok": True})

    @app.post("/api/sessions/{sid}/ask_question")
    async def ask_question(sid: str, request: Request) -> Response:
        body = await _json(request)
        session = _existing(manager, sid)
        question_id = str(body.get("question_id", ""))
        selected = body.get("selected") or []
        if not isinstance(selected, list):
            selected = [str(selected)]
        selected = [str(s) for s in selected]
        ok = session.on_ask_reply(question_id, selected)
        return JSONResponse({"ok": ok})

    # ── sub-agent drill-down ──────────────────────────────────────────────────
    @app.get("/api/sessions/{sid}/agent/{job}")
    async def agent_view(sid: str, job: str) -> Response:
        session = _existing(manager, sid)
        view = session.agent_view(job)
        if view is None:
            raise HTTPException(status_code=404, detail="unknown sub-agent job")
        return JSONResponse(view)

    # ── server restart ────────────────────────────────────────────────────────
    @app.post("/api/restart")
    async def restart_server() -> Response:
        """Restart the server **in place** by re-exec'ing the process.

        ``uvicorn.run`` is this process's foreground server — nothing supervises
        it — so a bare ``os._exit`` would kill the server and it would never come
        back (the WebUI then polls ``/theme.json`` until it gives up). Instead we
        ``os.execv`` ourselves: the same process (same PID, host and port) is
        replaced by a fresh Python interpreter that re-runs ``main`` with the
        original arguments, so config/code changes take effect and the WebUI's
        poll-then-reload finds the server back on the same address."""
        def _reexec() -> None:
            time.sleep(0.4)  # let the JSON response flush to the browser first
            try:
                if getattr(sys, "frozen", False):
                    # PyInstaller / desktop sidecar: the bundled exe re-runs itself.
                    argv = [sys.executable, *sys.argv[1:]]
                else:
                    # Reconstruct the launch via ``-m`` so it works whether we were
                    # started by the console-script entry point or ``python -m``.
                    argv = [
                        sys.executable, "-m", "autoverse.frontends.web.server",
                        *sys.argv[1:],
                    ]
                os.execv(argv[0], argv)
            except Exception:  # noqa: BLE001 - never kill the server on a failed re-exec
                pass

        threading.Thread(target=_reexec, name="restart-trigger", daemon=True).start()
        return JSONResponse({"ok": True, "message": "Server restarting — the page will reload …"})

    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    return app


def _session_id_from_path(path: str) -> str:
    match = _SESSION_PATH_RE.search(path)
    return match.group(1) if match else "-"


def _existing(manager: SessionManager, sid: str) -> Any:
    session = manager.sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not open")
    return session


def _extension_admin(session: Any) -> Any:
    extra = getattr(getattr(session, "app_ctx", None), "extra", {})
    admin = extra.get("extension_admin") if isinstance(extra, dict) else None
    if admin is None:
        raise HTTPException(status_code=404, detail="extension admin unavailable")
    return admin


def _ext_item(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "display_name": item.display_name,
        "source": item.source,
        "state": item.state,
        "enabled": item.enabled,
        "trusted": item.trusted,
        "summary": item.summary,
        "diagnostics_count": item.diagnostics_count,
    }


def _ext_detail(detail: Any) -> dict[str, Any]:
    return {
        "item": _ext_item(detail.item),
        "body": detail.body,
        "path": detail.path,
        "diagnostics": list(detail.diagnostics),
        "capabilities": list(detail.capabilities),
    }


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - empty/non-JSON body is fine
        return {}


async def _sse(bridge: SessionBridge, request: Request) -> AsyncIterator[str]:
    """Yield the snapshot, then block/state/status deltas as SSE frames.

    Starlette cancels this generator when the client disconnects, so we don't
    poll ``is_disconnected`` (which can race the receive channel); a 15s ping
    keeps intermediaries from closing an idle stream.
    """
    q = bridge.subscribe()
    try:
        yield _frame(bridge.snapshot())
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # keep-alive comment frame
                continue
            yield _frame(msg)
    finally:
        bridge.unsubscribe(q)


def _frame(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="autoverse-web", description="AutoVerse WebUI server")
    parser.add_argument("--model", default=None, help="model id (default: configured, else echo)")
    parser.add_argument("--cwd", default=None, help="working directory")
    parser.add_argument("--theme", default=None, help="theme name or JSON path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8170)
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="exit when this parent process disappears (desktop sidecar mode)",
    )
    args = parser.parse_args(argv)

    import uvicorn

    if args.parent_pid is not None:
        _start_parent_watchdog(args.parent_pid)

    app = create_app(model=args.model, cwd=args.cwd, theme_name=args.theme)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _start_parent_watchdog(parent_pid: int) -> None:
    """Stop a sidecar server when its owning desktop app disappears.

    The normal CLI/server path does not pass ``--parent-pid`` and is unchanged.
    Checking ``getppid`` also avoids accidentally attaching to an unrelated
    process if the original PID is recycled after the app exits.
    """

    def watch() -> None:
        while os.getppid() == parent_pid:
            time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, name="autoverse-parent-watchdog", daemon=True).start()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["create_app", "main"]
