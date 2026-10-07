"""autoverse.app — assembly layer (DI container + facade)."""

from __future__ import annotations

from autoverse.app.config import (
    CompactionConfig,
    Config,
    MemoryConfig,
    ModelConfig,
    ModelTiersConfig,
    RecoveryConfig,
)
from autoverse.app.context import AppContext, SpawnFn
from autoverse.app.options import EngineOptions

__all__ = [
    "AppContext",
    "SpawnFn",
    "Config",
    "CompactionConfig",
    "RecoveryConfig",
    "ModelConfig",
    "ModelTiersConfig",
    "MemoryConfig",
    "EngineOptions",
]
