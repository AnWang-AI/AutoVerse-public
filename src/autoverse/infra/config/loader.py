"""Layered config loader (docs/reference/config.md §1.4).

Override priority, low → high::

    built-in defaults  <  global file  <  project file  <  env vars  <  CLI

Same-key values from higher layers win after a deep merge of the layer dicts;
the merged dict is validated once by Pydantic so the final value is
deterministic (F1). On a validation error every problem is summarised — never
first-error-stop — and each carries field path + value + source layer (F3, P5).

Sources:

- **defaults**  : implicit (``Config()`` field defaults — the truth source, P1)
- **global**    : ``<global_dir>/config.json`` (TOML or JSON; defaults to
  ``~/.autoverse``)
- **project**   : the first ``.autoverse/config.json`` (or ``.toml``) found
  scanning upward from ``cwd``
- **env**       : ``AUTOVERSE_`` prefix, ``__`` = nesting separator
- **cli**       : an explicit overrides mapping (already dotted/typed)
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from autoverse.app.config import Config

_ENV_PREFIX = "AUTOVERSE_"
_NEST_SEP = "__"
#: Layer names in priority order (low → high). Source metadata is derived from
#: this order, so there is no second hand-maintained priority list (P1).
_LAYER_ORDER = ("default", "global", "project", "env", "cli")


class ConfigError(Exception):
    """Rich, human-facing config failure (P5/F3).

    Aggregates *all* validation problems (not first-error-stop). Each line
    names the field path, the offending value, the source layer and the
    expectation, plus a nearest-neighbour hint for unknown keys.
    """

    def __init__(self, message: str, *, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = problems

    @classmethod
    def from_pydantic(
        cls, exc: ValidationError, sources: Mapping[str, str]
    ) -> "ConfigError":
        problems: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            source = sources.get(loc, "default")
            kind = err.get("type", "")
            if kind == "extra_forbidden":
                hint = _nearest_key(loc)
                tail = f"  hint: did you mean '{hint}'?" if hint else ""
                problems.append(f"unknown key '{loc}'  source: {source}{tail}")
            else:
                value = err.get("input")
                problems.append(
                    f"{loc} = {value!r}  source: {source}\n"
                    f"    expected: {err.get('msg', kind)}"
                )
        header = f"config validation failed ({len(problems)} problem(s)):"
        body = "\n".join(f"  - {p}" for p in problems)
        return cls(f"{header}\n{body}", problems=tuple(problems))


@dataclass(frozen=True)
class LoadedConfig:
    """Validated config plus its provenance (derived view, P1).

    ``sources`` maps a dotted key path → the layer that supplied the winning
    value, for ``/config`` "where did this come from" introspection (§1.8).
    """

    config: Config
    sources: dict[str, str]
    global_path: Path | None
    project_path: Path | None


def load_config(
    cwd: Path,
    env: Mapping[str, str],
    cli: Mapping[str, Any],
    *,
    global_dir: Path | None = None,
) -> LoadedConfig:
    """Assemble, deep-merge and validate the layered config (F1/F3)."""
    gdir = global_dir if global_dir is not None else Path.home() / ".autoverse"
    global_path = _find_global(gdir)
    project_path = _find_project(cwd)

    layers: list[tuple[str, dict[str, Any]]] = [
        ("default", {}),
        ("global", _read_file(global_path)),
        ("project", _read_file(project_path)),
        ("env", _parse_env_overrides(env)),
        ("cli", _normalise_cli(cli)),
    ]

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for source_name, data in layers:
        _deep_merge(merged, data, source_name, sources, prefix="")

    try:
        cfg = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError.from_pydantic(exc, sources) from exc

    return LoadedConfig(
        config=cfg,
        sources=sources,
        global_path=global_path,
        project_path=project_path,
    )


# --------------------------------------------------------------------------- #
# file discovery + parsing
# --------------------------------------------------------------------------- #
def _find_global(global_dir: Path) -> Path | None:
    for name in ("config.json", "config.toml"):
        candidate = global_dir / name
        if candidate.is_file():
            return candidate
    return None


def _find_project(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for directory in (current, *current.parents):
        for name in ("config.json", "config.toml"):
            candidate = directory / ".autoverse" / name
            if candidate.is_file():
                return candidate
    return None


def _read_file(path: Path | None) -> dict[str, Any]:
    # Missing file → empty layer → fall back to defaults (not an error, §1.7).
    if path is None or not path.is_file():
        return {}
    raw = path.read_bytes()
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(raw.decode("utf-8"))
        else:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        # Corrupt ≠ missing: do NOT silently ignore (§1.7, P5).
        raise ConfigError(
            f"config file is corrupt and cannot be parsed: {path}\n    {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file root must be a table/object: {path}")
    return data


# --------------------------------------------------------------------------- #
# env + cli normalisation
# --------------------------------------------------------------------------- #
def _parse_env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """``AUTOVERSE_COMPACTION__COMPACT_THRESHOLD=0.8`` → nested dict.

    ``__`` separates nesting levels; the leaf value is coerced from its string
    form (JSON first, then bool/number, then plain string).
    """
    out: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        path = key[len(_ENV_PREFIX) :].lower().split(_NEST_SEP)
        if not path or path == [""]:
            continue
        _assign_nested(out, path, _coerce_scalar(raw))
    return out


def _normalise_cli(cli: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit CLI overrides keyed by dotted path (e.g. ``model.name``).

    Values are taken as-is (already typed by the CLI parser); the leading
    convenience aliases (``--model`` → ``model.model_id``) are the caller's job.
    """
    out: dict[str, Any] = {}
    for dotted, value in cli.items():
        _assign_nested(out, dotted.split("."), value)
    return out


def _coerce_scalar(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _assign_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[path[-1]] = value


# --------------------------------------------------------------------------- #
# deep merge + provenance
# --------------------------------------------------------------------------- #
def _deep_merge(
    base: dict[str, Any],
    overlay: Mapping[str, Any],
    source: str,
    sources: dict[str, str],
    *,
    prefix: str,
) -> None:
    for key, value in overlay.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            existing = base.get(key)
            if not isinstance(existing, dict):
                existing = {}
                base[key] = existing
            _deep_merge(existing, value, source, sources, prefix=f"{dotted}.")
        else:
            base[key] = value
            sources[dotted] = source


def _nearest_key(unknown: str) -> str | None:
    """Best-effort spelling hint among the known top-level + nested field names."""
    import difflib

    leaf = unknown.split(".")[-1]
    candidates = sorted(_known_field_names())
    matches = difflib.get_close_matches(leaf, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _known_field_names() -> set[str]:
    names: set[str] = set(Config.model_fields)
    for field in Config.model_fields.values():
        annotation = field.annotation
        sub_fields = getattr(annotation, "model_fields", None)
        if sub_fields:
            names.update(sub_fields)
    return names


__all__ = ["ConfigError", "LoadedConfig", "load_config"]
