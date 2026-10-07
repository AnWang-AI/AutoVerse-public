"""Unit: CLI entry — one-shot runs green; no-prompt in a non-TTY exits 2.

These exercise the CLI entry which consumes ONLY StreamEvent and the narrow
EngineHandle facade (R8). The interactive path is the Textual TUI only (there
is no plain-text REPL fallback), so it is covered by the TUI app tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoverse.frontends.cli.main import _dispatch, _resolve_model, build_parser


async def test_cli_one_shot_returns_zero(tmp_path: Path) -> None:
    args = build_parser().parse_args(["hello", "--model", "echo", "--cwd", str(tmp_path)])
    rc = await _dispatch(args)
    assert rc == 0


async def test_cli_no_prompt_returns_2(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--cwd", str(tmp_path)])
    rc = await _dispatch(args)
    assert rc == 2


def test_resolve_model_uses_configured_default(tmp_path: Path) -> None:
    # A project-level config pins a default model; a bare launch (no --model)
    # must honour it rather than falling back to echo.
    proj = tmp_path / "proj"
    (proj / ".autoverse").mkdir(parents=True)
    (proj / ".autoverse" / "config.json").write_text(
        json.dumps({"model": {"model_id": "configured-x"}}), encoding="utf-8"
    )
    assert _resolve_model(None, proj) == "configured-x"


def test_resolve_model_explicit_flag_wins(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / ".autoverse").mkdir(parents=True)
    (proj / ".autoverse" / "config.json").write_text(
        json.dumps({"model": {"model_id": "configured-x"}}), encoding="utf-8"
    )
    # Explicit --model (including echo) overrides the configured default.
    assert _resolve_model("echo", proj) == "echo"
    assert _resolve_model("other", proj) == "other"


def test_resolve_model_falls_back_to_echo(tmp_path: Path) -> None:
    # No flag and no configured model → offline echo last resort.
    assert _resolve_model(None, tmp_path / "empty") == "echo"
