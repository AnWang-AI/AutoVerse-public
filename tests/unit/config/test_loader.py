"""Unit tests — layered config loader (docs/reference/config.md §1.4/§1.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoverse.infra.config import ConfigError, load_config


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_defaults_only(tmp_path: Path) -> None:
    loaded = load_config(tmp_path, env={}, cli={}, global_dir=tmp_path / "no-global")
    assert loaded.config.max_turns == 100
    assert loaded.config.compaction.compact_threshold == pytest.approx(0.72)


def test_override_precedence_default_lt_global_lt_project_lt_env_lt_cli(
    tmp_path: Path,
) -> None:
    global_dir = tmp_path / "global"
    _write(global_dir / "config.json", {"max_turns": 10})
    project = tmp_path / "proj"
    _write(project / ".autoverse" / "config.json", {"max_turns": 20})

    # env beats project; cli beats env — final value is deterministic.
    loaded = load_config(
        project,
        env={"AUTOVERSE_MAX_TURNS": "30"},
        cli={"max_turns": 40},
        global_dir=global_dir,
    )
    assert loaded.config.max_turns == 40
    assert loaded.sources["max_turns"] == "cli"


def test_global_overridden_by_project(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    _write(global_dir / "config.json", {"max_turns": 10})
    project = tmp_path / "proj"
    _write(project / ".autoverse" / "config.json", {"max_turns": 20})
    loaded = load_config(project, env={}, cli={}, global_dir=global_dir)
    assert loaded.config.max_turns == 20
    assert loaded.sources["max_turns"] == "project"


def test_env_nested_double_underscore(tmp_path: Path) -> None:
    loaded = load_config(
        tmp_path,
        env={"AUTOVERSE_COMPACTION__COMPACT_THRESHOLD": "0.8"},
        cli={},
        global_dir=tmp_path / "none",
    )
    assert loaded.config.compaction.compact_threshold == pytest.approx(0.8)
    assert loaded.sources["compaction.compact_threshold"] == "env"


def test_validation_error_names_field(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    _write(global_dir / "config.json", {"compaction": {"compact_threshold": 1.5}})
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path, env={}, cli={}, global_dir=global_dir)
    text = str(exc.value)
    assert "compaction.compact_threshold" in text
    assert "1.5" in text


def test_unknown_key_rejected_with_hint(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    _write(global_dir / "config.json", {"complaction": {}})
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path, env={}, cli={}, global_dir=global_dir)
    text = str(exc.value)
    assert "complaction" in text
    assert "compaction" in text  # nearest-neighbour hint


def test_corrupt_file_is_not_silently_ignored(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    (global_dir).mkdir(parents=True)
    (global_dir / "config.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path, env={}, cli={}, global_dir=global_dir)
    assert "corrupt" in str(exc.value).lower()


def test_cli_dotted_override(tmp_path: Path) -> None:
    loaded = load_config(
        tmp_path,
        env={},
        cli={"model.model_id": "anthropic/claude-sonnet"},
        global_dir=tmp_path / "none",
    )
    assert loaded.config.model.model_id == "anthropic/claude-sonnet"
    assert loaded.sources["model.model_id"] == "cli"
