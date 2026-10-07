"""EngineBuilder — explicit assembly returning an EngineHandle (docs/architecture/contracts.md §7.2).

Replaces the old 10-element tuple (D1): ``build()`` wires the Store, Conversation,
LLM provider, compaction/recovery policies, stop-gate registry, tool dispatch,
the :class:`AppContext` DI container and the :class:`TurnLoop`, then wraps it all
in an :class:`EngineAdapter` (an :class:`~autoverse.ports.EngineHandle`).

``app/`` may import ``infra``/``core`` (only ``core → infra/app`` is forbidden).
The default provider is the offline :class:`EchoProvider`, so ``build()`` works
without network/credentials for one-shot and test runs.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.app.engine import EngineAdapter, QueueEventSink
from autoverse.app.extensions import wire_extensions
from autoverse.app.options import EngineOptions
from autoverse.app.policy_map import (
    build_core_compaction_config as _build_compaction_config,
    build_core_recovery_config as _build_recovery_config,
)
from autoverse.app.prompt_env import build_environment
from autoverse.app.session_query import sync_session_catalog

# Importing the builtin package triggers @register_tool self-registration so
# ToolRegistry.build() sees the full builtin tool set (docs/reference/tools.md §8).
import autoverse.core.tools.builtin  # noqa: F401,E402  (import for side effects)
from autoverse.core.context.compaction import CompactionPolicy
from autoverse.core.context.recovery import RecoveryPolicy
from autoverse.core.loop.turn_loop import TurnLoop
from autoverse.core.prompt import PromptAssembler, build_tool_list
from autoverse.core.state.conversation import Conversation
from autoverse.core.tools.base import Tool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.dispatcher import ToolDispatcher
from autoverse.core.tools.registry import ToolRegistry
from autoverse.app.subagent import build_subagent_runner
from autoverse.app.workflow import (
    BackgroundGate,
    CompletionGate,
    GoalEvaluator,
    GoalGate,
    GoalStore,
    PhaseGate,
    WorkflowRegistry,
)
from autoverse.app.workflow.judge import make_llm_judge
from autoverse.app.workflow.pipelines import builtin_workflow_definitions
from autoverse.core.workflow.stop_gate import GateRegistry
from autoverse.infra.config.loader import load_config
from autoverse.infra.browser import (
    BrowserRuntimeConfig,
    BrowserSession,
    playwright_available,
)
from autoverse.infra.llm.echo import EchoProvider
from autoverse.infra.permission.fixed import (
    AllowAllPermissionAdapter,
    DenyAllPermissionAdapter,
    NonInteractivePermissionAdapter,
)
from autoverse.infra.imagegen import OpenRouterImageBackend
from autoverse.infra.observ import FanOutSink, TraceCollector, TraceStore
from autoverse.infra.observ.logging import configure_from_config
from autoverse.infra.persist import (
    BusyLock,
    FileCompactionArchive,
    FileTurnIndexStore,
    JSONLStore,
    SQLiteSessionCatalog,
    TaskStore,
)
from autoverse.infra.secrets import (
    DecryptError,
    EnvFirstSecretProvider,
    SecretVault,
    VaultUnavailable,
)
from autoverse.infra.isolation.worktree import (
    GitWorktreeIsolator,
    InMemoryWorktreeRegistry,
)
from autoverse.infra.snapshot import GitShadowSnapshot
from autoverse.infra.tools.agent_tool import AgentTool
from autoverse.infra.tools.job_admin import JobGetTool, JobListTool, JobStopTool
from autoverse.infra.tools.job_output import JobOutputTool
from autoverse.infra.tools.vision_tool import VisionTool
from autoverse.infra.llm.registry import get_context_window
from autoverse.infra.llm.router import Router
from autoverse.infra.llm.transport.credential import CredentialResolver
from autoverse.ports import EventSink, LLMProvider, SecretProvider, SessionMeta, Store
from autoverse.ports.isolation import IsoSpec, WorkspaceMode, WorkspaceSpec
from autoverse.types import StreamEvent
from autoverse.types.tools import ToolCall, ToolResult

if TYPE_CHECKING:
    from autoverse.frontends.gateway import GatewayRunner


def _wrap_dispatch_with_hooks(
    dispatcher: ToolDispatcher, hook_engine: Any
) -> Any:
    """Wrap per-call dispatch with the HookEngine Pre/PostToolUse middleware (§6.4).

    The middleware's ``next`` contract is exactly ``dispatch_one(call, ctx)``: a
    PreToolUse ``deny`` short-circuits before permission/execution (feedback to the
    model, P5); otherwise the call proceeds and PostToolUse observes. Order +
    concurrency match the plain dispatcher (T3)."""
    middleware = hook_engine.middleware()

    async def dispatch(calls: list[ToolCall], ctx: ToolContext) -> list[ToolResult]:
        if not calls:
            return []
        if dispatcher._plan_exit_barrier_active(calls, ctx):  # noqa: SLF001 - shared safety gate
            return await dispatcher.dispatch(calls, ctx)
        tasks = [middleware(c, ctx, dispatcher.dispatch_one) for c in calls]
        return list(await asyncio.gather(*tasks))

    return dispatch


async def _build_workflow_gates(
    *,
    app: AppContext,
    store: Store,
    session_id: str,
    runner: Any,
    permission_engine: Any,
    provider: LLMProvider,
    model: str,
    events: EventSink,
) -> GateRegistry:
    """Wire per-session workflow state and return the stop-gate registry."""
    goal_store = GoalStore(store=store)
    try:
        persisted_meta = await store.load_meta(session_id)
        goal_store.restore(
            session_id,
            getattr(persisted_meta, "extra", {}).get("goal_state"),
        )
    except Exception:  # noqa: BLE001 — optional goal snapshot must not block load
        pass
    app.goal = goal_store

    workflows = WorkflowRegistry()
    for defn in builtin_workflow_definitions():
        workflows.register(defn)
    workflows.register_decorated()
    app.extra["workflows"] = workflows
    app.extra["subagent_runner"] = runner
    app.extra["workflow_permissions"] = permission_engine

    judge = make_llm_judge(provider, model=model)
    gates = GateRegistry()
    gates.register(BackgroundGate(session_id=session_id))
    gates.register(CompletionGate(session_id=session_id))
    gates.register(PhaseGate(registry=workflows, session_id=session_id))
    gates.register(
        GoalGate(
            store=goal_store,
            evaluator=GoalEvaluator(judge),
            session_id=session_id,
            events=events,
        )
    )
    return gates


class _StaticSecret:
    """A one-key :class:`SecretProvider`: maps every lookup to a fixed token.

    Used when an explicit ``api_key`` is supplied via :class:`EngineOptions`,
    so the credential resolver returns it without requiring an env var."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self, key: str) -> str | None:
        return self._value

    def require(self, key: str) -> str:
        return self._value


def _make_provider(opts: EngineOptions) -> LLMProvider:
    """Select the LLM backend from the model id.

    ``echo`` → the offline :class:`EchoProvider` so test/smoke runs need no
    network. An empty model is rejected (ValueError). Any other model id is
    resolved through the production :class:`Router` (registry → codec →
    transport → fallback). An explicit ``opts.api_key`` is injected as a
    :class:`SecretProvider`; otherwise the transport's credential is read from
    the environment (e.g. ``OPENROUTER_API_KEY``).
    """
    if opts.llm_provider is not None:
        return opts.llm_provider
    if opts.model == "echo":
        return EchoProvider(reply="echo")
    if not opts.model:
        raise ValueError("model is required: cannot be empty; use 'echo' for offline smoke tests")
    secrets: SecretProvider | None = None
    if opts.api_key:
        secrets = _StaticSecret(opts.api_key)
    elif opts.secrets is not None:
        secrets = opts.secrets  # gap10: the env-first encrypted vault
    retry_max = (
        max(0, int(opts.settings.recovery.max_retries))
        if opts.settings is not None
        else 3
    )
    return Router(
        CredentialResolver(secrets), max_retries=retry_max
    ).build(opts.model)


def _resolve_config(opts: EngineOptions) -> Config:
    """Layered config (gap10/A8): explicit ``settings`` wins, else load_config.

    With no ``settings`` injected we assemble default<global<project<env<cli
    (CLI overrides empty here — the CLI maps its flags onto EngineOptions, not the
    config tree). A corrupt config file is a real, surfaced error (P5/F3); a
    *missing* file just falls back to defaults.
    """
    if opts.settings is not None:
        return opts.settings
    return load_config(opts.cwd, os.environ, {}).config


def _find_plaintext_secret_source(cwd: Path) -> Path | None:
    """Find a project-local ``.autoverse/secrets`` dotenv-style source file."""
    try:
        current = cwd.expanduser().resolve()
    except OSError:
        current = cwd.expanduser()
    for directory in (current, *current.parents):
        candidate = directory / ".autoverse" / "secrets"
        if candidate.is_file():
            return candidate
    return None


def _parse_plaintext_secret_source(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines without ever logging values."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts or "=" not in parts[0]:
            continue
        key, value = parts[0].split("=", 1)
        key = key.strip()
        if key and key.replace("_", "").isalnum() and not key[0].isdigit():
            values[key] = value
    return values


def _backup_unreadable_vault(path: Path) -> None:
    """Keep a ciphertext backup before regenerating from a plaintext source."""
    if not path.is_file():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-import-{stamp}")
    try:
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
    except OSError:
        pass


def _import_plaintext_secret_source(vault: SecretVault, vault_path: Path, cwd: Path) -> None:
    source = _find_plaintext_secret_source(cwd)
    if source is None:
        return
    values = _parse_plaintext_secret_source(source)
    if not values:
        return
    try:
        vault.update(values)
    except DecryptError:
        _backup_unreadable_vault(vault_path)
        vault.replace_all(values)


def _build_secrets(opts: EngineOptions, data_dir: Path) -> SecretProvider:
    """Env-first secret provider over an AES-256-GCM vault (gap10/A6).

    Env vars always win (an explicit export is operator intent, F5); the vault
    is the durable fallback. When ``cryptography`` is missing the vault is
    unavailable and we degrade to env-only resolution rather than failing the
    build (docs/reference/config.md §2.8).

    A project-local ``.autoverse/secrets`` file is treated as an operator-owned
    plaintext import source: its KEY=VALUE entries are merged into the encrypted
    vault at startup. If an old hostname-derived vault can no longer be
    decrypted, the ciphertext is backed up and regenerated from that source.
    """
    if opts.secrets is not None:
        return opts.secrets
    vault: SecretVault | None = None
    vault_path = data_dir / "secrets.enc"
    try:
        vault = SecretVault(vault_path)
        _import_plaintext_secret_source(vault, vault_path, opts.cwd)
    except VaultUnavailable:
        vault = None  # cryptography not installed → env-only
    return EnvFirstSecretProvider(vault)


async def _build_trace(
    opts: EngineOptions, config: Config, session_id: str, data_dir: Path
) -> TraceCollector | None:
    """Per-session trace collector (gap10/A5), persisted under ``traces/``.

    Returned so the builder can fan it in as a downstream of the queue sink and
    expose it on ``app.trace`` for ``/trace`` + the sidebar."""
    if not config.trace_enabled:
        return None
    store = TraceStore(data_dir / "traces")
    existing = await store.load(session_id)
    return TraceCollector(
        session_id,
        opts.model,
        store=store,
        initial_model=existing,
    )


def _downstream_sink(
    user_sink: EventSink | None, trace: TraceCollector | None
) -> EventSink | None:
    """Combine the caller sink + trace collector into one downstream (O8).

    ``None`` when neither is present; a single sink passes through directly; both
    are fanned out so one bad sink never starves the other."""
    sinks: list[EventSink] = []
    if user_sink is not None:
        sinks.append(user_sink)
    if trace is not None:
        sinks.append(trace)
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    return FanOutSink(sinks)


def _build_image_backend(secrets: SecretProvider) -> OpenRouterImageBackend | None:
    """Wire the OpenRouter text-to-image backend when a key is present (§11.7).

    Inject only when the env-first secret provider resolves
    ``OPENROUTER_API_KEY``; otherwise leave ``app.image_backend`` as ``None`` so
    ``generate_image`` degrades gracefully instead of failing at call time
    (P5)."""
    key: str | None = None
    try:
        key = secrets.get("OPENROUTER_API_KEY")
    except Exception:  # noqa: BLE001 — provider failure → fall back to env probe
        key = None
    if not key and not os.environ.get("OPENROUTER_API_KEY"):
        return None
    return OpenRouterImageBackend(secrets)


def _build_browser_session(config: Config) -> BrowserSession | None:
    """Wire a session-scoped browser backend when the optional driver exists."""
    browser = config.browser
    if browser.backend == "none":
        return None
    if browser.backend in {"auto", "playwright"} and not playwright_available():
        return None
    runtime = BrowserRuntimeConfig(
        backend=browser.backend,
        headless=browser.headless,
        viewport_width=browser.viewport_width,
        viewport_height=browser.viewport_height,
        timeout_ms=browser.timeout_ms,
    )
    return BrowserSession(runtime)


def _make_window_provider(app: AppContext) -> Callable[[], int]:
    """A closure resolving the *active* model's context window (docs/reference/config.md §1).

    Reads the live ``AppContext`` each call so a ``/model`` switch (which updates
    ``app.model``) or a config hot-swap re-targets compaction without rebuilding
    the policy. ``compaction.context_window`` (when >0) is an explicit override;
    otherwise the registry's per-model window is used (unknown → 200k default)."""

    def _resolve() -> int:
        cfg = getattr(getattr(app, "config", None), "compaction", None)
        override = int(getattr(cfg, "context_window", 0) or 0) or None
        return get_context_window(getattr(app, "model", "") or "", override=override)

    return _resolve


def _build_permission(opts: EngineOptions, config: Config) -> Any:
    """Resolve the PermissionPort for production builds.

    Explicit DI wins. Otherwise follow the config default; ``ask`` degrades to a
    non-interactive deny-at-approval adapter unless a frontend supplies its own
    interactive adapter (TUI does this).
    """
    if opts.permission_engine is not None:
        return opts.permission_engine
    if config.default_permission_mode == "allow_all":
        return AllowAllPermissionAdapter()
    if config.default_permission_mode == "deny_all":
        return DenyAllPermissionAdapter()
    return NonInteractivePermissionAdapter()


def _seed_permission_rules(perm_engine: Any, rule_store: Any) -> None:
    """Load persisted "always" rules into a PermissionEngine's matcher at build.

    Best-effort: if the engine is a ``PermissionEngine`` (has a ``matcher``),
    sync its matcher with the rules on disk. Fixed adapters (AllowAll/DenyAll)
    have no matcher and are skipped — they never reach the approval step anyway.
    """
    matcher = getattr(perm_engine, "matcher", None)
    if matcher is None:
        return
    try:
        matcher.sync_rules(rule_store.load())
    except Exception:  # noqa: BLE001 — a bad permissions file must not block build
        pass


class EngineBuilder:
    """Construct an :class:`EngineHandle` from :class:`EngineOptions` (P4, D1)."""

    def __init__(self, options: EngineOptions) -> None:
        self._options = options

    def make_gateway_runner(self) -> "GatewayRunner":
        """Create a :class:`GatewayRunner` wired to this builder's options.

        The runner's ``engine_factory`` builds a fresh :class:`EngineAdapter` per
        session via :meth:`build`, injecting the gateway's per-session event sink
        and permission port. Platforms are registered after construction via
        ``runner.add_platform(name, ...)`` (G7/P8).
        """
        from autoverse.frontends.gateway import GatewayRunner, PlatformRegistry

        base_opts = self._options

        async def engine_factory(
            session_id: str,
            sink: EventSink,
            permission: Any,
        ) -> Any:
            gateway_opts = replace(
                base_opts,
                session_id=session_id,
                event_sink=sink,
                permission_engine=permission,
            )
            return await EngineBuilder(gateway_opts).build()

        return GatewayRunner(
            registry=PlatformRegistry(),
            engine_factory=engine_factory,
        )

    async def build(self) -> EngineAdapter:
        opts = self._options

        # ── Layered config + persistence/secrets roots (gap10) ──────────────
        config: Config = _resolve_config(opts)
        # The config tree's model id is the fallback when the caller (CLI flags →
        # EngineOptions) supplied none, so a configured model is honoured instead
        # of silently defaulting to echo. An explicit opts.model always wins.
        if not opts.model and config.model.model_id:
            opts = replace(opts, model=config.model.model_id)
        session_id = opts.session_id or f"sess-{uuid.uuid4().hex[:12]}"
        session_root = Path(config.session_dir).expanduser()
        data_dir = opts.data_dir or session_root.parent
        log_path = configure_from_config(config, data_dir, frontend="engine")

        main_isolator = GitWorktreeIsolator()
        worktree_registry = InMemoryWorktreeRegistry()
        session_workspace_cm: Any = None
        session_workspace_handle: Any = None
        workspace_spec = opts.workspace
        if isinstance(workspace_spec, WorkspaceSpec) and workspace_spec.mode is WorkspaceMode.SESSION_WORKTREE:
            base_cwd = Path(workspace_spec.repo_root or opts.cwd)
            slug = f"session/{session_id}"
            session_workspace_cm = main_isolator.scope(
                IsoSpec(slug=slug, lifecycle="session", base_ref=workspace_spec.base_ref),
                base_cwd,
            )
            session_workspace_handle = await session_workspace_cm.__aenter__()
            worktree_registry.register_session(session_id, session_workspace_handle)
            opts = replace(opts, cwd=Path(session_workspace_handle.cwd))

        # Env-first encrypted vault (A6); thread it onto opts so _make_provider
        # (and the recursive sub-agent provider) resolve credentials from it.
        secrets: SecretProvider = _build_secrets(opts, data_dir)
        opts = replace(opts, secrets=secrets)

        # Durable JSONL session store by default (A4); MemStore only via DI seam.
        store: Store = opts.store or JSONLStore(session_root)
        session_catalog = opts.session_catalog
        if (
            session_catalog is None
            and isinstance(store, JSONLStore)
            and opts.role is None
        ):
            session_catalog = SQLiteSessionCatalog(
                data_dir / "session-catalog.sqlite3"
            )
        conversation = Conversation(store, session_id)
        # Stamp created_at/updated_at = now so a brand-new session sorts to the
        # top of its rail group immediately (before its first turn stamps
        # last_activity_at, §9.4). create() is idempotent, so this only takes
        # effect for genuinely new sessions — never clobbers existing history.
        _now = time.time()
        await store.create(
            session_id,
            SessionMeta(
                session_id=session_id,
                model=opts.model,
                cwd=str(opts.cwd),
                created_at=_now,
                updated_at=_now,
            ),
        )
        if opts.initial_messages is None:
            await conversation.hydrate()
        else:
            conversation.hydrate_from(opts.initial_messages)

        # Trace collector (A5): fanned in as a downstream of the queue sink so the
        # same StreamEvent feeds the streaming consumer, the caller sink and the
        # trace fold; persisted per-turn under data_dir/traces.
        trace = await _build_trace(opts, config, session_id, data_dir)
        context_archive = FileCompactionArchive(data_dir / "context-archive")
        turn_indices = FileTurnIndexStore(data_dir / "turn-indices")

        # Queue bridge so run() can stream events while the turn runs.
        queue: "asyncio.Queue[StreamEvent | None]" = asyncio.Queue()
        sink: EventSink = QueueEventSink(
            queue, downstream=_downstream_sink(opts.event_sink, trace)
        )

        provider: LLMProvider = _make_provider(replace(opts, settings=config))

        app = AppContext(
            config=config,
            store=store,
            cwd=opts.cwd,
            model=opts.model,
            events=sink,
            secrets=secrets,
            context_archive=context_archive,
            session_catalog=session_catalog,
        )
        app.trace = trace
        app.extra["session_id"] = session_id
        app.extra["session_root"] = str(session_root)
        if session_catalog is not None and (
            opts.initial_messages is None or opts.initial_messages_complete
        ):
            try:
                await sync_session_catalog(
                    session_catalog,
                    store,
                    session_id,
                    conversation.view().messages,
                    source_path=session_root / f"{session_id}.jsonl",
                )
            except Exception:  # noqa: BLE001 - catalog is repairable projection state
                pass
        if log_path is not None:
            app.extra["log_path"] = str(log_path)
        app.extra["model_supports_vision"] = bool(
            getattr(getattr(provider, "capabilities", None), "vision", False)
        )
        # Worktree isolation for the interactive worktree mode (docs/reference/subagents.md §5a):
        # expose an Isolator + a per-session registry so the main engine's
        # Enter/ExitWorktreeMode tools (and the REPL three-state cycle) can build
        # and finalize a manual-lifecycle worktree. The sub-agent runner keeps its
        # own isolator instance (L1, turn lifecycle) — these are L2/5a.
        app.isolator = main_isolator
        app.worktrees = worktree_registry
        if session_workspace_handle is not None:
            app.extra["session_worktree_handle"] = session_workspace_handle
            app.extra["session_worktree_cm"] = session_workspace_cm
            app.extra["session_worktree_on_end"] = getattr(
                workspace_spec, "on_session_end", "keep"
            )
            app.extra["group_root"] = str(
                getattr(session_workspace_handle, "source_root", None) or opts.cwd
            )
            app.extra["isolation_root"] = str(session_workspace_handle.cwd)
            source = getattr(session_workspace_handle, "source_root", None)
            if source is not None:
                app.extra["isolation_source_root"] = str(source)
        if opts.session_ops is not None:
            app.extra["session_ops"] = opts.session_ops
        if opts.manager_tools is not None:
            app.extra["manager_tools"] = opts.manager_tools
        if opts.manager_autonomy is not None:
            app.extra["manager_autonomy"] = opts.manager_autonomy
        # Text-to-image backend for the generate_image tool shell (docs/reference/tools.md §11.7).
        # Injected only when OPENROUTER_API_KEY resolves; otherwise None → the tool
        # degrades with an actionable error rather than crashing.
        app.image_backend = _build_image_backend(secrets)
        # Real browser automation is session-scoped and optional. When the
        # browser extra is not installed this remains None, so browser tools
        # return the same graceful no_backend feedback as other optional tools.
        app.browser = opts.browser if opts.browser is not None else _build_browser_session(config)
        # Session-isolated todo store (A4). app.jobs is installed by the sub-agent
        # wiring below (the in-memory fan-in store the JobOutput tool reads).
        app.tasks = TaskStore(data_dir)
        # Per-turn workspace snapshot for /rewind (A10); OFF by default — the
        # git-shadow capture spawns git over the whole work-tree (perf guard).
        if config.snapshot_enabled:
            app.snapshot = GitShadowSnapshot(
                opts.cwd, data_dir / "snapshots" / f"{session_id}.git"
            )
        # Make the permission port discoverable to the loop's ToolContext.
        permission_engine = _build_permission(opts, config)
        # Durable, cross-process permission rule store so "always allow" rules
        # survive a process restart and are visible to the other frontend.
        from autoverse.infra.permission.rule_store import PermissionRuleStore

        rule_store = PermissionRuleStore(data_dir)
        # Seed the matcher with any persisted rules so they apply from turn 1.
        _seed_permission_rules(permission_engine, rule_store)
        app.extra["permissions"] = permission_engine
        setattr(app, "permissions", permission_engine)

        # ── Tool execution chain (gap7, docs/reference/tools.md §8) ────────────────────────
        # Build the real tool registry from the builtin declarations and the
        # ToolDispatcher (six-stage middleware: route/validate/permission/
        # event/timeout-abort/error-containment). The registry is exposed on the
        # DI container so the prompt assembler derives the tool list (P1) and the
        # TurnLoop derives LLMRequest.tools from the same single source.
        registry = ToolRegistry.build()
        app.tools = registry

        # ── Sub-agent chain (gap8, docs/reference/subagents.md §3) ──────────────────────────
        # Build the production SubAgentRunner (recursive engine_factory + ports),
        # install app.spawn / app.jobs, and register the `agent` + `JobOutput`
        # shells into the SAME registry so the main model can dispatch sub-agents
        # and collect background jobs. Registered before prompt assembly so the
        # Available Tools section (P1) lists them.
        runner = build_subagent_runner(opts, config, app, _make_provider)
        # Concrete tools narrow run()'s arg type (the codebase-wide pattern); the
        # structural Tool protocol uses BaseModel, so cast at this instance-register
        # seam (the decorator path does the same widening for builtins).
        registry.register(
            "subagent",
            cast(
                list[Tool],
                [
                    AgentTool(runner),
                    JobOutputTool(),
                    JobListTool(),
                    JobGetTool(),
                    JobStopTool(),
                ],
            ),
        )

        # ── Vision delegation (multimodal bridge) ───────────────────────────
        # Let a text-only main model "see" by delegating one image to a vision
        # model (default openrouter/minimax-m3). The provider is built lazily via
        # the same Router/credential path as the main provider; building per
        # model id is cheap and reuses the encrypted vault for credentials.
        registry.register(
            "vision",
            cast(
                list[Tool],
                [VisionTool(lambda mid: Router(CredentialResolver(secrets)).build(mid))],
            ),
        )

        # ── Extension sources (docs/reference/extensions.md) ────────────────────────────
        # Build MCP/Skills/Hooks/Plugins from config, run their lifecycle, register
        # contributed tools into the SAME registry, and install them on the DI
        # container so the prompt assembler projects their fragments (P1/P8). The
        # returned HookEngine wraps tool dispatch with the Pre/PostToolUse chain.
        hook_engine = await wire_extensions(opts, config, app)
        app.extra["hook_engine"] = hook_engine

        dispatcher = ToolDispatcher(registry)

        # Assemble the main-agent system prompt (docs/architecture/runtime-flow.md §10) and inject
        # it into the DI container; TurnLoop consumes it via app.prompt.render().
        app.prompt = _build_prompt_assembler(opts, config, app)

        gates = await _build_workflow_gates(
            app=app,
            store=store,
            session_id=session_id,
            runner=runner,
            permission_engine=permission_engine,
            provider=provider,
            model=opts.model,
            events=sink,
        )

        # Map the app Config compaction tree onto the core CompactionConfig the
        # policy actually consumes, so user overrides take effect (not ignored).
        compaction = CompactionPolicy(
            provider,
            _build_compaction_config(config.compaction),
            window_provider=_make_window_provider(app),
            archive=context_archive,
            session_id=session_id,
        )
        recovery = RecoveryPolicy(_build_recovery_config(config.recovery))

        # Hook middleware sits before permission/execution (docs/reference/hooks.md):
        # PreToolUse may deny a call (feedback to the model), PostToolUse observes.
        dispatch: Any = dispatcher.dispatch
        if hook_engine is not None:
            dispatch = _wrap_dispatch_with_hooks(dispatcher, hook_engine)

        loop = TurnLoop(
            llm=provider,
            conversation=conversation,
            events=sink,
            compaction=compaction,
            recovery=recovery,
            gates=gates,
            dispatch_tools=dispatch,
            app=app,
            initial_turn_index=(
                max((turn.turn_index for turn in trace.model.turns), default=-1) + 1
                if trace is not None
                else 0
            ),
            turn_indices=turn_indices,
            session_id=session_id,
        )

        # A provider is bound to one model at build time (WireProvider sends the
        # spec's wire_model and ignores per-request model), so a model switch
        # must rebuild the provider. Hand the adapter a factory that reuses the
        # same secrets/credential path for the newly-selected model.
        def _provider_for(model_id: str) -> LLMProvider:
            return _make_provider(replace(opts, model=model_id, settings=config))

        # Cross-process busy marker so the other frontend's session rail can
        # detect that this session is mid-turn (busy_external status).
        busy = BusyLock(session_root) if isinstance(store, JSONLStore) else None

        return EngineAdapter(
            loop=loop,
            conversation=conversation,
            app=app,
            queue=queue,
            session_id=session_id,
            provider_factory=_provider_for,
            busy=busy,
            rule_store=rule_store,
        )


def _build_prompt_assembler(
    opts: EngineOptions, config: Config, app: AppContext
) -> PromptAssembler:
    """Assemble the main-agent system prompt (docs/architecture/runtime-flow.md §10).

    Each dynamic section comes from its own single source (P1): tool list from
    the ToolRegistry, environment from the app-layer probe, memory from the
    MemoryLoader, append from config + options, extension fragments from each
    registered ExtensionSource. Every input is best-effort — a failure in one
    section degrades that section, never the whole prompt (P5).
    """
    assembler = PromptAssembler(session_mode=opts.session_mode)
    assembler.tool_list = build_tool_list(app.tools)
    try:
        assembler.environment = build_environment(opts.cwd)
    except Exception:  # noqa: BLE001 — env probe must not break engine build
        assembler.environment = ""

    # Project memory (CLAUDE.md / AGENTS.md) from cwd only. role=None → no
    # role trim. Store the AssembledMemory on the app so the web frontend can
    # list which files were loaded; render for the prompt.
    try:
        from autoverse.infra.memory import MemoryLoader

        mem = MemoryLoader(config.memory).assemble(opts.cwd, role=opts.role)
        app.memory = mem
        assembler.memory = mem.render()
    except Exception:  # noqa: BLE001 — memory is a best-effort enrichment
        assembler.memory = ""

    # User-supplied append: config tree + per-call option (both already defined).
    appends = [config.system_prompt_append.strip(), opts.append_system_prompt.strip()]
    assembler.append = "\n\n".join(a for a in appends if a)

    # Extension fragments (docs/reference/extensions.md): MCP tools / Hook constraints /
    # Skills. Any source exposing prompt_fragment() is projected; empty ignored.
    for src in app.extension_sources:
        frag = getattr(src, "prompt_fragment", None)
        name = getattr(src, "name", None)
        if callable(frag) and name:
            try:
                assembler.add_fragment(str(name), frag() or "")
            except Exception:  # noqa: BLE001 — one bad source must not break others
                continue

    return assembler


__all__ = ["EngineBuilder"]
