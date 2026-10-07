"""Unit: the built-in command catalogue (§3.2.1 full set, gap3).

Asserts ``build_default_registry`` registers the full §3.2.1 command set, that
read-only query handlers degrade gracefully against an AppContext with all
optional slots ``None`` (never raising), that ``/exit``/``/quit`` quit, that
``/details`` toggles a render flag, and that ``/secret`` never prints a value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.app.extension_admin import ExtensionListItem
from autoverse.frontends.repl.builtin_cmds import (
    _apply_model_tiers,
    build_default_registry,
    register_skill_commands,
)
from autoverse.frontends.repl.commands import (
    CommandContext,
    CommandResult,
    CommandRegistry,
    dispatch,
)
from autoverse.types import Message


class _DummyStore:
    async def append(self, session_id: str, *msgs: Message) -> None: ...
    async def load(self, session_id: str, *, since: int = 0) -> list[Message]:
        return []
    async def rewrite(self, session_id: str, msgs: list[Message], reason: str) -> None: ...
    async def create(self, session_id: str, meta: object) -> None: ...
    async def load_meta(self, session_id: str) -> Any: ...
    async def update_meta(self, session_id: str, **fields: object) -> None: ...
    async def list_sessions(self) -> list[str]:
        return ["sess-a", "sess-b"]
    async def delete(self, session_id: str) -> None: ...


class _RecordingSink:
    async def emit(self, event: object) -> None: ...


def _minimal_app(**overrides: object) -> AppContext:
    app = AppContext(
        config=Config(),
        store=_DummyStore(),  # type: ignore[arg-type]
        cwd=Path.cwd(),
        model="test-model",
        events=_RecordingSink(),  # type: ignore[arg-type]
    )
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def _ctx(
    registry: CommandRegistry,
    *,
    app: AppContext | None = None,
    engine: object | None = None,
    is_running: bool = False,
) -> CommandContext:
    return CommandContext(
        engine=engine if engine is not None else object(),
        app=app if app is not None else _minimal_app(),
        registry=registry,
        permissions=None,
        is_running=is_running,
        extra={},
    )


# §3.2.1 authoritative command set the catalogue must register.
_EXPECTED = {
    "help", "model", "tiers", "subagent-model", "subagent-models",
    "clear", "compact", "stop", "rewind", "queue", "goal",
    "quit", "exit", "context", "cost", "usage", "diff", "export", "memory",
    "sessions", "session-tree", "details", "doctor", "config", "permissions",
    "secret", "secrets", "trace", "skills", "mcp", "plugin", "expand",
}


def test_registry_registers_full_catalogue() -> None:
    reg = build_default_registry()
    names = {n.lstrip("/") for n in reg.completion_names()}
    missing = _EXPECTED - names
    assert not missing, f"missing commands: {sorted(missing)}"


def _distinct_count(reg: CommandRegistry) -> int:
    distinct = {id(reg.get(n.lstrip("/"))) for n in reg.completion_names()}
    return len(distinct)


def test_registry_has_full_builtin_catalogue() -> None:
    # The built-in set is intentionally allowed to grow; the floor catches
    # accidental removals while keeping additive command work cheap.
    reg = build_default_registry()
    assert _distinct_count(reg) >= 29


async def test_catalogue_reaches_29_with_skill_commands() -> None:
    reg = build_default_registry()

    async def _runner(args: str) -> str:
        return "ran"

    register_skill_commands(reg, {"demo-skill": _runner})
    assert _distinct_count(reg) >= 29


async def test_skills_command_lists_loaded_admin_skills() -> None:
    class _Admin:
        def list_skills(self) -> list[ExtensionListItem]:
            return [
                ExtensionListItem(
                    id="excalidraw-diagram",
                    kind="skill",
                    display_name="excalidraw-diagram",
                    source="user",
                    state="enabled",
                    enabled=True,
                    trusted=False,
                    summary="Draw diagrams",
                )
            ]

    reg = build_default_registry()
    app = _minimal_app()
    app.extra["extension_admin"] = _Admin()
    res = await dispatch("/skills", _ctx(reg, app=app), reg)
    assert res.display == "skills:\n  excalidraw-diagram [user] enabled - Draw diagrams"


async def test_skills_command_fallback_excludes_extension_management_builtins() -> None:
    reg = build_default_registry()

    async def _runner(args: str) -> str:
        return "ran"

    register_skill_commands(reg, {"demo-skill": _runner})
    res = await dispatch("/skills", _ctx(reg), reg)
    assert res.display == "skills:\n  /demo-skill"
    assert "/plugin" not in res.display
    assert "/mcp" not in res.display


async def test_mcp_command_lists_only_mcp_status_and_tools() -> None:
    class _Tool:
        def __init__(self, name: str, description: str) -> None:
            self.name = name
            self.description = description

    class _MCP:
        name = "mcp"
        server_status = {"docs": "connected", "bad": "failed"}

        def contributed_tools(self) -> list[_Tool]:
            return [_Tool("mcp__docs__search", "[docs] Search docs")]

        def resource_tools(self) -> list[_Tool]:
            return [_Tool("list_mcp_resources", "List resources")]

    class _OtherSource:
        name = "skills"

        def contributed_tools(self) -> list[_Tool]:
            return [_Tool("skill_tool", "not MCP")]

    reg = build_default_registry()
    app = _minimal_app()
    app.extension_sources = (_OtherSource(), _MCP())
    res = await dispatch("/mcp", _ctx(reg, app=app), reg)

    assert "docs: connected" in res.display
    assert "bad: failed" in res.display
    assert "mcp__docs__search - [docs] Search docs" in res.display
    assert "list_mcp_resources - List resources" in res.display
    assert "skill_tool" not in res.display
    assert "extension source" not in res.display


def test_apply_model_tiers_does_not_switch_main_model() -> None:
    reg = build_default_registry()
    app = _minimal_app()
    ctx = _ctx(reg, app=app)

    ok = _apply_model_tiers(
        ctx,
        {
            "deep": "deep-model",
            "balanced": "balanced-model",
            "fast": "fast-model",
        },
    )

    assert ok is True
    assert app.model == "test-model"
    assert app.config.model_tiers.deep == "deep-model"
    assert app.config.model_tiers.balanced == "balanced-model"
    assert app.config.model_tiers.fast == "fast-model"


async def test_exit_and_quit_request_quit() -> None:
    reg = build_default_registry()
    ctx = _ctx(reg)
    exit_res = await reg.get("exit").handler(ctx, "")  # type: ignore[union-attr]
    quit_res = await reg.get("quit").handler(ctx, "")  # type: ignore[union-attr]
    assert exit_res.should_quit is True
    assert quit_res.should_quit is True


# Read-only / info handlers that must run without raising against None slots.
_GRACEFUL = [
    "context", "cost", "usage", "sessions", "session-tree", "diff", "export",
    "memory", "goal", "mcp", "plugin", "queue", "permissions", "trace",
    "config", "doctor", "stop", "skills", "tiers",
]


@pytest.mark.parametrize("name", _GRACEFUL)
async def test_handlers_degrade_gracefully(name: str) -> None:
    reg = build_default_registry()
    ctx = _ctx(reg)
    spec = reg.get(name)
    assert spec is not None
    res = await spec.handler(ctx, "")
    assert isinstance(res, CommandResult)
    # Always a real result: either flagged error or an informative display.
    assert res.is_error or res.display


async def test_secret_never_prints_secret_value() -> None:
    secret_value = "sk-super-secret-VALUE-do-not-leak"

    class _FakeSecrets:
        def get(self, key: str) -> str | None:
            return secret_value if key == "OPENAI_API_KEY" else None

        def require(self, key: str) -> str:
            return secret_value

        def list_aliases(self) -> list[str]:
            return ["OPENAI_API_KEY"]

    app = _minimal_app(secrets=_FakeSecrets())
    reg = build_default_registry()
    ctx = _ctx(reg, app=app)
    res = await reg.get("secret").handler(ctx, "")  # type: ignore[union-attr]
    assert secret_value not in res.display
    assert "OPENAI_API_KEY" in res.display  # alias is shown, value is not


async def test_secret_set_uses_session_layer_without_leaking_value() -> None:
    secret_value = "sk-session-secret-do-not-leak"

    class _FakeSecrets:
        def __init__(self) -> None:
            self.session: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.session.get(key)

        def require(self, key: str) -> str:
            return self.session[key]

        def set_session(self, key: str, value: str) -> None:
            self.session[key] = value

        def session_aliases(self) -> list[str]:
            return sorted(self.session)

        def vault_aliases(self) -> list[str]:
            return []

        def list_aliases(self) -> list[str]:
            return sorted(self.session)

    secrets = _FakeSecrets()
    app = _minimal_app(secrets=secrets)
    reg = build_default_registry()
    ctx = _ctx(reg, app=app)
    res = await reg.get("secret").handler(ctx, f"set OPENROUTER_API_KEY {secret_value}")  # type: ignore[union-attr]
    assert not res.is_error
    assert secrets.get("OPENROUTER_API_KEY") == secret_value
    status = await reg.get("secret").handler(ctx, "")  # type: ignore[union-attr]
    assert secret_value not in status.display
    assert "effective=session" in status.display


async def test_secret_save_and_delete_use_vault_layer() -> None:
    class _FakeSecrets:
        def __init__(self) -> None:
            self.vault: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.vault.get(key)

        def require(self, key: str) -> str:
            return self.vault[key]

        def set(self, key: str, value: str) -> None:
            self.vault[key] = value

        def delete(self, key: str) -> bool:
            return self.vault.pop(key, None) is not None

        def session_aliases(self) -> list[str]:
            return []

        def vault_aliases(self) -> list[str]:
            return sorted(self.vault)

        def list_aliases(self) -> list[str]:
            return sorted(self.vault)

    secrets = _FakeSecrets()
    app = _minimal_app(secrets=secrets)
    reg = build_default_registry()
    ctx = _ctx(reg, app=app)
    await reg.get("secret").handler(ctx, "save DEEPSEEK_API_KEY sk-file-secret")  # type: ignore[union-attr]
    assert "DEEPSEEK_API_KEY" in secrets.vault
    status = await reg.get("secret").handler(ctx, "")  # type: ignore[union-attr]
    assert "effective=file" in status.display
    await reg.get("secret").handler(ctx, "delete DEEPSEEK_API_KEY")  # type: ignore[union-attr]
    assert "DEEPSEEK_API_KEY" not in secrets.vault


async def test_details_toggles_flag() -> None:
    reg = build_default_registry()
    ctx = _ctx(reg)
    assert ctx.extra.get("show_details") in (None, False)
    res1 = await reg.get("details").handler(ctx, "")  # type: ignore[union-attr]
    assert ctx.extra.get("show_details") is True
    assert not res1.is_error
    res2 = await reg.get("details").handler(ctx, "")  # type: ignore[union-attr]
    assert ctx.extra.get("show_details") is False
    assert not res2.is_error


async def test_cost_with_meter_reachable_via_trace() -> None:
    """When the app exposes a trace with a public meter, /cost derives from it."""
    from autoverse.infra.observ.meter import Meter
    from autoverse.types.messages import Usage

    meter = Meter()
    meter.record("test-model", Usage(input_tokens=1000, output_tokens=500))

    class _Trace:
        @property
        def meter(self) -> Meter:
            return meter

    app = _minimal_app(trace=_Trace())
    reg = build_default_registry()
    ctx = _ctx(reg, app=app)
    res = await reg.get("cost").handler(ctx, "")  # type: ignore[union-attr]
    assert not res.is_error
    assert "test-model" in res.display
