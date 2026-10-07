"""Unit: /model persists defaults to global config; /config surfaces tiers.

Covers the write-through + provenance behaviour added so the model picked via
``/model`` survives restarts (docs/reference/config.md §1.4), and that ``/config``
renders the main model + sub-agent tier mapping clearly (unset → main model).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoverse.app.config import Config, ModelTiersConfig
from autoverse.app.context import AppContext
from autoverse.frontends.repl.builtin_cmds import (
    _cmd_config,
    _do_switch_model,
    _persist_model_defaults,
)
from autoverse.frontends.repl.commands import CommandContext, CommandRegistry


class _FakeEngine:
    def __init__(self, app: AppContext) -> None:
        self._app = app
        self.switched: list[str] = []

    def switch_model(self, model: str) -> None:
        self.switched.append(model)
        self._app.model = model


class _RecordingStore:
    async def append(self, *a: object, **k: object) -> None: ...
    async def load(self, *a: object, **k: object) -> list[object]:
        return []


def _make_ctx(tmp_path: Path, *, config: Config | None = None) -> tuple[CommandContext, _FakeEngine]:
    app = AppContext(
        config=config or Config(),
        store=_RecordingStore(),
        cwd=tmp_path / "proj",
        model="echo",
        events=object(),  # type: ignore[arg-type]
    )
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)
    engine = _FakeEngine(app)
    ctx = CommandContext(engine=engine, app=app, registry=CommandRegistry())
    return ctx, engine


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at a fresh tmp dir and drop AUTOVERSE_* so global writes/reads
    are deterministic and never touch a real config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for key in [k for k in __import__("os").environ if k.startswith("AUTOVERSE_")]:
        monkeypatch.delenv(key, raising=False)


async def test_switch_model_persists_as_global_default(tmp_path: Path) -> None:
    ctx, engine = _make_ctx(tmp_path)
    result = await _do_switch_model(engine, "claude-x", ctx)

    assert engine.switched == ["claude-x"]
    assert "saved as default" in result.display
    saved = json.loads((tmp_path / "home" / ".autoverse" / "config.json").read_text())
    assert saved["model"]["model_id"] == "claude-x"


async def test_persist_tiers_writes_all_three(tmp_path: Path) -> None:
    ctx, _ = _make_ctx(tmp_path)
    saved, warning = _persist_model_defaults(
        ctx,
        model_id="deep-x",
        tiers={"deep": "deep-x", "balanced": "bal-y", "fast": "fast-z"},
    )
    assert saved is True
    assert warning is None
    data = json.loads((tmp_path / "home" / ".autoverse" / "config.json").read_text())
    assert data["model"]["model_id"] == "deep-x"
    assert data["model_tiers"] == {"deep": "deep-x", "balanced": "bal-y", "fast": "fast-z"}


async def test_persist_warns_when_env_shadows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOVERSE_MODEL__MODEL_ID", "env-pick")
    ctx, _ = _make_ctx(tmp_path)
    saved, warning = _persist_model_defaults(ctx, model_id="global-pick")
    assert saved is True
    assert warning is not None and "env" in warning


async def test_config_renders_tiers_and_unset_fallback(tmp_path: Path) -> None:
    config = Config(
        model_tiers=ModelTiersConfig(deep="deep-x", balanced="", fast="")
    )
    ctx, _ = _make_ctx(tmp_path, config=config)
    result = await _cmd_config(ctx, "")

    body = result.display
    assert "sub-agent tiers:" in body
    assert "deep" in body and "deep-x" in body
    # Unset tiers are explained, not shown as a bare empty string.
    assert "unset → main model" in body
    # Full effective config still included below the focused section.
    assert "effective config:" in body
