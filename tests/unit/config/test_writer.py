"""Unit tests — global config writer (docs/reference/config.md §1.4).

``/model`` persists the chosen default model + sub-agent tiers here. The writer
must merge *only* the changed dotted keys into the global ``config.json`` (never
a full dump), so unrelated keys and the layering contract survive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoverse.infra.config import (
    ConfigError,
    ConfigPersistError,
    load_config,
    merge_into_config_file,
    resolve_global_config_path,
)


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_writes_new_file_with_nested_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    merge_into_config_file(
        path,
        {
            "model.model_id": "deep-x",
            "model_tiers.deep": "deep-x",
            "model_tiers.balanced": "bal-y",
            "model_tiers.fast": "fast-z",
        },
    )
    data = _read(path)
    assert data == {
        "model": {"model_id": "deep-x"},
        "model_tiers": {"deep": "deep-x", "balanced": "bal-y", "fast": "fast-z"},
    }


def test_merges_only_changed_keys_preserving_others(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "max_turns": 42,
                "model": {"model_id": "old", "max_tokens": 4096},
            }
        ),
        encoding="utf-8",
    )
    merge_into_config_file(path, {"model.model_id": "new"})
    data = _read(path)
    # Only model.model_id changed; sibling + unrelated keys are untouched.
    assert data["max_turns"] == 42
    assert data["model"] == {"model_id": "new", "max_tokens": 4096}


def test_written_default_is_loadable_and_attributed_to_global(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    path = resolve_global_config_path(global_dir)
    assert path == global_dir / "config.json"  # default target when none exists

    merge_into_config_file(
        path,
        {"model.model_id": "deep-x", "model_tiers.deep": "deep-x"},
    )
    loaded = load_config(tmp_path / "proj", env={}, cli={}, global_dir=global_dir)
    assert loaded.config.model.model_id == "deep-x"
    assert loaded.config.model_tiers.deep == "deep-x"
    assert loaded.sources["model.model_id"] == "global"


def test_higher_layer_shadows_saved_global_default(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    merge_into_config_file(
        resolve_global_config_path(global_dir), {"model.model_id": "global-pick"}
    )
    # An env override sits above the global file → it wins, provenance says so.
    loaded = load_config(
        tmp_path,
        env={"AUTOVERSE_MODEL__MODEL_ID": "env-pick"},
        cli={},
        global_dir=global_dir,
    )
    assert loaded.config.model.model_id == "env-pick"
    assert loaded.sources["model.model_id"] == "env"


def test_resolve_prefers_existing_file(tmp_path: Path) -> None:
    existing = tmp_path / "config.json"
    existing.write_text("{}", encoding="utf-8")
    assert resolve_global_config_path(tmp_path) == existing


def test_toml_target_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("max_turns = 5\n", encoding="utf-8")
    with pytest.raises(ConfigPersistError):
        merge_into_config_file(path, {"model.model_id": "x"})


def test_corrupt_json_target_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigPersistError):
        merge_into_config_file(path, {"model.model_id": "x"})


def test_corrupt_target_does_not_raise_configerror_subclass_confusion() -> None:
    # ConfigPersistError is its own type, distinct from the loader's ConfigError.
    assert not issubclass(ConfigPersistError, ConfigError)
