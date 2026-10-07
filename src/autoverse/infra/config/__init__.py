"""Layered config loading (docs/reference/config.md §1).

Merges five sources — built-in defaults < global file < project file <
environment variables < explicit CLI overrides — into one deterministic,
validated, frozen :class:`~autoverse.app.config.Config` tree (F1). All tunable
thresholds live in the ``Config`` sub-models (P1/F2); this loader holds zero
scattered magic constants. Validation failures surface as a rich
:class:`ConfigError` that names the offending field, value and source (F3).

This subsystem lives in ``infra`` and is never imported by ``core`` (P3).
"""

from __future__ import annotations

from autoverse.infra.config.loader import (
    ConfigError,
    LoadedConfig,
    load_config,
)
from autoverse.infra.config.writer import (
    ConfigPersistError,
    merge_into_config_file,
    resolve_global_config_path,
)

__all__ = [
    "ConfigError",
    "ConfigPersistError",
    "LoadedConfig",
    "load_config",
    "merge_into_config_file",
    "resolve_global_config_path",
]
