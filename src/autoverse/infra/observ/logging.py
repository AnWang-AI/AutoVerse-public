"""Application logging setup (docs/reference/events-and-storage.md §3).

This module configures process-level diagnostics that complement the structured
``StreamEvent`` trace. Trace is the user/workflow audit log; Python logging is
the operator/debug log for frontend, server, and observer failures.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "autoverse_session_id", default="-"
)
_FRONTEND: contextvars.ContextVar[str] = contextvars.ContextVar(
    "autoverse_frontend", default="-"
)
_HANDLER_MARK = "_autoverse_owned"


class ContextFilter(logging.Filter):
    """Attach AutoVerse contextvars to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = getattr(record, "session_id", _SESSION_ID.get())
        record.frontend = getattr(record, "frontend", _FRONTEND.get())
        return True


class JsonFormatter(logging.Formatter):
    """Small JSON-lines formatter for durable local diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "session_id": getattr(record, "session_id", "-"),
            "frontend": getattr(record, "frontend", "-"),
        }
        if record.exc_info:
            data["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(data, ensure_ascii=False)


def configure_logging(
    *,
    log_dir: Path | str,
    level: str = "INFO",
    json_lines: bool = True,
    frontend: str = "-",
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> Path:
    """Configure the process root logger with one rotating AutoVerse file.

    Calling this repeatedly is safe: previous AutoVerse-owned handlers are
    removed and replaced, while handlers installed by embedding applications are
    left alone.
    """
    path = Path(log_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    log_path = path / "autoverse.log"

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    setattr(handler, _HANDLER_MARK, True)
    handler.addFilter(ContextFilter())
    if json_lines:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(frontend)s %(session_id)s] "
                "%(name)s: %(message)s"
            )
        )
    root.addHandler(handler)
    root.setLevel(_parse_level(level))
    logging.captureWarnings(True)
    _FRONTEND.set(frontend or "-")
    return log_path


def configure_from_config(config: object, data_dir: Path | str, *, frontend: str) -> Path | None:
    """Configure logging from the public Config tree."""
    if not bool(getattr(config, "log_enabled", True)):
        return None
    raw_log_dir = str(getattr(config, "log_dir", "") or "")
    log_dir = Path(raw_log_dir).expanduser() if raw_log_dir else Path(data_dir) / "logs"
    return configure_logging(
        log_dir=log_dir,
        level=str(getattr(config, "log_level", "INFO") or "INFO"),
        json_lines=bool(getattr(config, "log_json", True)),
        frontend=frontend,
        max_bytes=int(getattr(config, "log_max_bytes", 5_000_000) or 5_000_000),
        backup_count=int(getattr(config, "log_backup_count", 3) or 3),
    )


@contextmanager
def log_context(*, session_id: str | None = None, frontend: str | None = None) -> Iterator[None]:
    """Temporarily bind log context for the current task/thread."""
    session_token = (
        _SESSION_ID.set(session_id) if session_id is not None else None
    )
    frontend_token = _FRONTEND.set(frontend) if frontend is not None else None
    try:
        yield
    finally:
        if frontend_token is not None:
            _FRONTEND.reset(frontend_token)
        if session_token is not None:
            _SESSION_ID.reset(session_token)


def _parse_level(level: str) -> int:
    parsed = logging.getLevelName(str(level).upper())
    return parsed if isinstance(parsed, int) else logging.INFO


__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "configure_from_config",
    "configure_logging",
    "log_context",
]
