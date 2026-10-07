"""EngineOptions / EngineBuilder facade (docs/architecture/contracts.md §7.2).

EngineOptions field set is the cc-webui compatibility baseline; new fields must
have defaults (backward compatible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from autoverse.app.config import Config
from autoverse.ports import (
    BrowserPort,
    EventSink,
    LLMProvider,
    PermissionPort,
    SecretProvider,
    SessionCatalogPort,
    Store,
)
from autoverse.types import Message


@dataclass
class EngineOptions:
    model: str
    cwd: Path
    api_key: str | None = None
    base_url: str | None = None
    settings: Config | None = None
    session_id: str | None = None
    thinking_budget: int = 0
    session_mode: Literal["interactive", "one_shot"] = "interactive"
    permission_engine: PermissionPort | None = None
    # Optional explicit LLM backend. When set, it overrides model-id-based
    # selection in EngineBuilder (DI seam for embedding/tests; defaults preserve
    # the model-id router path). docs/architecture/contracts.md §7.2: new fields are defaulted.
    llm_provider: LLMProvider | None = None
    # Optional explicit extension sources (MCP/Skills/Hooks/Plugins). When set,
    # EngineBuilder uses these verbatim instead of deriving them from config
    # (DI seam for tests/embedding; default None → config-derived). gap9.
    extension_sources: tuple[object, ...] | None = None
    # Persistence / secrets DI seams (gap10). When unset, EngineBuilder derives
    # the defaults from config (JSONLStore under session_dir, an env-first vault).
    # Tests/embedding inject these to stay hermetic or in-memory.
    store: Store | None = None
    session_catalog: SessionCatalogPort | None = None
    # Store-derived snapshot already loaded by a read-only frontend. This avoids
    # a duplicate load during lazy engine activation; turn start still syncs.
    initial_messages: tuple[Message, ...] | None = None
    # False when ``initial_messages`` is only a bounded tail window. In that
    # case the seed may hydrate Conversation for activation, but must never be
    # used to overwrite full-session catalog projections such as prompt count.
    initial_messages_complete: bool = True
    secrets: SecretProvider | None = None
    data_dir: Path | None = None
    event_sink: EventSink | None = None
    role: str | None = None
    append_system_prompt: str = ""
    manager_tools: object | None = None
    session_ops: object | None = None
    manager_autonomy: object | None = None
    # A frontend-owned visible browser can replace the default headless
    # Playwright session while retaining the same core tool contract.
    browser: BrowserPort | None = None
    workspace: object | None = None  # WorkspaceSpec | None (docs/reference/subagents.md §6)
    extra: dict[str, object] = field(default_factory=dict)


__all__ = ["EngineOptions"]
