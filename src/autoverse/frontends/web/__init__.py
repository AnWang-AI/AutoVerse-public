"""AutoVerse WebUI — a consumer adapter on the shared engine.

The Web frontend is positioned exactly like ``LineRepl``: it feeds
the engine a :class:`~autoverse.frontends.repl.tui_sink.TuiEventSink` over a
duck-typed log surface (:class:`~autoverse.frontends.web.web_log.WebLog`) so the
SAME rendering decisions, derived state (tokens / cost / todos / changed files /
sub-agents) and command/permission machinery are reused unchanged. The only
new code is the surface that turns sink writes into structured block JSON, the
SSE bridge, the session manager, and the FastAPI routes.
"""

from __future__ import annotations

from fastapi import FastAPI

__all__ = ["create_app", "main"]


def create_app(
    *,
    model: str | None = None,
    cwd: str | None = None,
    theme_name: str | None = None,
) -> FastAPI:  # pragma: no cover - thin re-export
    from autoverse.frontends.web.server import create_app as _create_app

    return _create_app(model=model, cwd=cwd, theme_name=theme_name)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin re-export
    from autoverse.frontends.web.server import main as _main

    return _main(argv)
