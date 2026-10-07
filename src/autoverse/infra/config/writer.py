"""Persist user config changes back to the global config file (docs/reference/config.md §1.4).

``/model`` writes the chosen default model + sub-agent tiers here so they survive
restarts. We only merge the *changed* dotted keys into the existing global file
(never a full effective-config dump): the file is purely the ``global`` layer,
so the layering contract is preserved — project/env/cli still override it.

Only JSON is written. The stdlib has no TOML writer, so a ``.toml`` global file
is rejected with :class:`ConfigPersistError` rather than silently creating a
shadowing ``config.json`` (the loader probes ``config.json`` before
``config.toml``, so a silent switch would change which file wins).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ConfigPersistError(Exception):
    """A config write could not be completed (corrupt target / unsupported format)."""


def resolve_global_config_path(global_dir: Path | None = None) -> Path:
    """The global config file to write to.

    Mirrors the loader's discovery: prefer an existing ``config.json`` then
    ``config.toml`` under ``global_dir`` (defaults to ``~/.autoverse``); when no
    file exists yet, target a new ``config.json``.
    """
    gdir = global_dir if global_dir is not None else Path.home() / ".autoverse"
    for name in ("config.json", "config.toml"):
        candidate = gdir / name
        if candidate.is_file():
            return candidate
    return gdir / "config.json"


def merge_into_config_file(path: Path, changes: Mapping[str, Any]) -> None:
    """Deep-merge ``changes`` (dotted key → value) into the JSON file at ``path``.

    The existing file is read and only the named keys are overwritten; every
    other key is preserved verbatim. Parent directories are created as needed.
    """
    if path.suffix == ".toml":
        raise ConfigPersistError(
            f"cannot persist to a TOML config ({path}); "
            "no TOML writer is available — convert it to config.json to save defaults"
        )

    existing: dict[str, Any] = {}
    if path.is_file():
        raw = path.read_bytes()
        if raw.strip():
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ConfigPersistError(
                    f"global config is corrupt and cannot be updated: {path}\n    {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ConfigPersistError(
                    f"global config root must be a JSON object: {path}"
                )
            existing = loaded

    for dotted, value in changes.items():
        _assign_nested(existing, dotted.split("."), value)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _assign_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    """Set ``value`` at the nested ``path``, creating intermediate tables.

    A non-table value blocking an intermediate path is replaced (the higher-level
    write wins), mirroring the loader's deep-merge nesting behaviour.
    """
    cursor = target
    for part in path[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[path[-1]] = value


__all__ = ["ConfigPersistError", "merge_into_config_file", "resolve_global_config_path"]
