"""LineRepl — 行式(line-based)交互 REPL 兜底.

全屏 Textual TUI 不可用时(未装 ``textual``、``TERM=dumb``、弱 TTY、用户
``--no-tui``)的降级路径。**最大化复用** TUI 的事件渲染管线:同一套
``render_event`` / ``TOOL_RENDERERS`` 渲染器、``TuiEventSink``、``@command``
注册表、``Theme`` 色阶、``PermissionPort``。

关键复用洞察(docs/guides/using-cli-and-tui.md §3):``TuiEventSink`` 运行时不依赖 ``textual``
(仅 ``rich``),且对日志表面的高级能力(``begin_stream`` / ``write_collapsible``
/ ``write_markdown`` / ``write_or_update_agent``)全是 ``callable(getattr(...))``
探测、缺失即优雅降级。故只需喂一个极简的 :class:`LineLog`(console 表面),即可
得到与 TUI **逐字一致的内容与配色**,仅在 alternate buffer 专属能力(原地流式
重绘、可点折叠、常驻侧栏、子代理钻取)上诚实降级。

本模块只新增三件东西:输出表面 :class:`LineLog`、行式审批
:class:`LinePermissionAdapter`、REPL 循环 :class:`LineRepl`。其余(渲染/命令/
中断/主题/权限引擎基类)全部复用,**框架层一字不改**。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Awaitable, Callable, cast

from autoverse.frontends.repl.commands import CommandContext, dispatch
from autoverse.frontends.repl.interrupt import InterruptHandler
from autoverse.frontends.repl.plan_approval import approved_plan_prompt
from autoverse.frontends.repl.renderer import (
    RenderContext,
    _kfmt,
    format_duration,
)
from autoverse.frontends.repl.theme import DEFAULT_THEME, Theme
from autoverse.frontends.repl.tui_sink import TuiEventSink
from autoverse.infra.permission.engine import GlobalMode, PermissionEngine
from autoverse.infra.permission.matcher import RuleMatcher, make_scoped_rule
from autoverse.types import EventType
from autoverse.types.events import TurnEndPayload
from autoverse.types.tools import Decision, ToolCall

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console, RenderableType

    from autoverse.app.context import AppContext
    from autoverse.frontends.repl.commands import CommandRegistry
    from autoverse.ports import EngineHandle, EventSink, PermissionPort


# An async callable that builds the engine given the sink + permission adapter.
# The CLI supplies this so the engine is wired with the live LineLog sink and the
# row-based approval adapter created inside :meth:`LineRepl.run`.
EngineFactory = Callable[["EventSink", "PermissionPort"], Awaitable["EngineHandle"]]

# An async line reader: ``(prompt) -> str``. Injectable so the loop is unit
# testable (tests feed a scripted reader instead of real ``input()``).
ReadLine = Callable[[str], Awaitable[str]]


class LineLog:
    """``TuiEventSink`` 的鸭子类型日志表面:把 rich renderable 打到 stdout。

    只实现 Sink 真正需要的两个方法(``write`` + ``write_or_update_agent``),其余
    高级能力一律缺席 → 交给 Sink 的内建回退(docs/guides/using-cli-and-tui.md §3.3)。这正是"复用"
    的精髓:降级行为是 Sink 自带的,不是这里新写的。
    """

    def __init__(self, console: "Console") -> None:
        self._console = console
        # 已出现过的子代理 job —— append-only 语义下,首次(dispatch)与终态各打
        # 一行,中间心跳跳过避免刷屏(docs/guides/using-cli-and-tui.md §5.3)。
        self._seen_agents: set[str] = set()

    def write(self, renderable: "RenderableType", *, classes: str = "") -> None:
        # ``classes``(user-echo / tool-block 等)是全屏 CSS 概念(左竖线背景块),
        # 行式忽略 —— 工具块的 ``┃`` / 缩进等视觉前缀本就由 renderer 烧进字符串,
        # 照样可见,故内容观感不丢。分组靠空行节奏。
        self._console.print(renderable)

    def write_or_update_agent(
        self, job: str, text: "RenderableType", *, done: bool = False
    ) -> None:
        if done or job not in self._seen_agents:
            self._seen_agents.add(job)
            self._console.print(text)


class LinePermissionAdapter(PermissionEngine):
    """行式审批 —— 镜像 ``TuiPermissionAdapter``,把 modal Screen 换成行内问询。

    复用 :class:`PermissionEngine` 基类(``check`` / ``check_sync`` / 规则匹配 /
    ``cancel_all`` 全继承),只重写 :meth:`request_approval`。文案/选项与
    ``PermissionScreen`` 一致(``y=本次 / a=始终 / N=拒绝``),``a`` 同样写入
    一条 "always" 规则(``make_scoped_rule``)。
    """

    def __init__(
        self,
        read_line: ReadLine,
        console: "Console",
        matcher: RuleMatcher | None = None,
        *,
        global_mode: GlobalMode = "ask",
        on_wait_start: Callable[[], None] | None = None,
        on_wait_end: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(matcher or RuleMatcher(), global_mode=global_mode)
        self._read_line = read_line
        self._console = console
        self._cancelled = False
        self._on_wait_start = on_wait_start
        self._on_wait_end = on_wait_end

    async def check(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="approval cancelled")
        return await super().check(call)

    async def request_approval(self, call: ToolCall) -> Decision:
        if self._cancelled:
            return Decision.deny(reason="approval cancelled")
        target = self._summarize(call)
        self._console.print(
            f"[bold yellow]权限请求[/bold yellow] — [bold]{call.name}[/bold]"
            + (f"  [dim]{target}[/dim]" if target else "")
        )
        if self._on_wait_start is not None:
            self._on_wait_start()
        try:
            ans = (await self._read_line("  允许? [y=本次 / a=始终 / N=拒绝] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):  # pragma: no cover - interrupt → deny
            return Decision.deny(reason="denied by user")
        finally:
            if self._on_wait_end is not None:
                self._on_wait_end()
        if ans == "a":
            self._matcher.add_rule(make_scoped_rule(call, "allow"), prepend=True)
            return Decision.allow(rule="user-always")
        if ans == "y":
            return Decision.allow(rule="user-approved")
        return Decision.deny(reason="denied by user")

    def cancel_all(self) -> None:
        self._cancelled = True
        super().cancel_all()

    @staticmethod
    def _summarize(call: ToolCall) -> str:
        if "file_path" in call.input:
            return str(call.input["file_path"])
        if "command" in call.input:
            return str(call.input["command"])
        return ""


class LineDialogHost:
    """行式 ``DialogHost`` —— 把全屏 overlay 降级为行内问询 (docs/guides/using-cli-and-tui.md §4.5).

    实现 :class:`~autoverse.frontends.repl.dialogs.host.DialogHost` 协议(``select``
    / ``confirm`` / ``prompt`` / ``toast`` / ``show_text``),让 ``builtin_cmds`` 的
    命令在行式下也能:① 对破坏性操作(``/clear`` / ``/compact``)行内确认,而不是
    无声执行;② 长报告(``/help`` / ``/config``)经分隔线包裹打印,而不是裸灌滚动区;
    ③ ``/model`` / ``/sessions`` / ``/skills`` / ``/workflows`` 用编号列表选择,而不是
    回退到"带参才生效"的纯文本。append-only 语义下无原地重绘:``toast`` 退化为一行
    带色提示,``show_text`` 直接整段打印(用上下分隔线圈出,可滚动复制)。
    """

    def __init__(self, read_line: ReadLine, console: "Console", theme: Theme) -> None:
        self._read_line = read_line
        self._console = console
        self._theme = theme

    async def select(
        self,
        options: object,
        *,
        title: str = "Select",
        placeholder: str = "type to filter…",  # noqa: ARG002 - filter is N/A in line mode
        multi: bool = False,
    ) -> object:
        """编号列表选择。单选读一个序号;``multi=True`` 读逗号分隔的多个序号。

        ``options`` 是 ``SelectOption`` 序列(``.label`` / ``.value``,可选 ``.group``)。
        空输入 / 非法序号 / Esc(EOF)→ 取消(单选返回 None,多选返回 [])。
        """
        opts = list(cast(Iterable[object], options))
        if not opts:
            return [] if multi else None
        t = self._theme
        accent, muted, dim = t.role("accent"), t.role("text_muted"), t.role("text_dim")
        self._console.print(f"[bold {accent}]{title}[/]")
        last_group = object()
        for i, opt in enumerate(opts, start=1):
            # SelectOption fields: ``title`` / ``value`` / ``category`` (the group
            # header) / ``description``. getattr keeps this duck-typed for any
            # stand-in with the same shape.
            group = getattr(opt, "category", None)
            if group and group != last_group:
                self._console.print(f"  [{dim}]{group}[/]")
                last_group = group
            label = getattr(opt, "title", None) or str(opt)
            desc = getattr(opt, "description", "")
            tail = f"  [{dim}]{desc}[/]" if desc else ""
            self._console.print(f"  [{accent}]{i:>2}[/] [{t.role('text')}]{label}[/]{tail}")
        hint = (
            "  选择序号(逗号分隔多选,留空取消): "
            if multi
            else "  选择序号(留空取消): "
        )
        try:
            raw = (await self._read_line(f"[{muted}]{hint}[/]")).strip()
        except (EOFError, KeyboardInterrupt):  # pragma: no cover - interrupt → cancel
            return [] if multi else None
        if not raw:
            return [] if multi else None

        def _pick(tok: str) -> object | None:
            if tok.isdigit() and 1 <= int(tok) <= len(opts):
                return getattr(opts[int(tok) - 1], "value", opts[int(tok) - 1])
            return None

        if multi:
            chosen = [v for v in (_pick(tok.strip()) for tok in raw.split(",")) if v is not None]
            return chosen
        return _pick(raw)

    async def confirm(
        self,
        message: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Confirm",  # noqa: ARG002 - line mode uses y/N, not labels
        cancel_label: str = "Cancel",  # noqa: ARG002
    ) -> bool:
        """行内 ``[y/N]`` 确认。非 ``y`` / EOF → False(安全默认:不执行破坏性操作)。"""
        t = self._theme
        self._console.print(
            f"[bold {t.role('warning')}]{title}[/] [{t.role('text')}]{message}[/]"
        )
        try:
            ans = (await self._read_line(f"[{t.role('text_muted')}]  确认? [y/N] [/]")).strip().lower()
        except (EOFError, KeyboardInterrupt):  # pragma: no cover - interrupt → cancel
            return False
        return ans in ("y", "yes")

    async def prompt(
        self,
        title: str,
        *,
        placeholder: str = "",
        initial: str = "",  # noqa: ARG002 - line input can't pre-fill an editable buffer
        password: bool = False,  # noqa: ARG002 - line mode echoes; no masked TTY input here
    ) -> str | None:
        """行内单行输入。空输入 / EOF → None(取消)。"""
        t = self._theme
        suffix = f" [{t.role('text_dim')}]({placeholder})[/]" if placeholder else ""
        self._console.print(f"[bold {t.role('accent')}]{title}[/]{suffix}")
        try:
            val = (await self._read_line(f"[{t.role('text_muted')}]  › [/]")).strip()
        except (EOFError, KeyboardInterrupt):  # pragma: no cover - interrupt → cancel
            return None
        return val or None

    def toast(self, message: str, *, variant: str = "info") -> None:
        """瞬态角标在行式无处安放 → 退化为一行带色提示。"""
        role = {
            "success": "success",
            "warning": "warning",
            "error": "error",
        }.get(variant, "text_muted")
        colour = self._theme.role(role) or self._theme.role("text_muted")
        self._console.print(f"[{colour}]{message}[/]")

    async def show_text(self, body: str, *, title: str = "") -> None:
        """长报告:上下分隔线圈出整段打印(append-only,无分页器,可滚动复制)。"""
        t = self._theme
        dim = t.role("border_subtle")
        if title:
            self._console.print(f"[bold {t.role('accent')}]{title}[/]")
        self._console.print(f"[{dim}]{'─' * min(48, max(20, self._console.width - 6))}[/]")
        self._console.print(body)
        self._console.print(f"[{dim}]{'─' * min(48, max(20, self._console.width - 6))}[/]")


class LineRepl:
    """纯 append-only、turn-by-turn 的行式交互 REPL。"""

    def __init__(
        self,
        engine_factory: EngineFactory,
        registry: "CommandRegistry",
        *,
        console: "Console | None" = None,
        theme: Theme | None = None,
        cwd: str = "",
        model: str = "",
        version: str = "0.1.0",
        read_line: ReadLine | None = None,
    ) -> None:
        from rich.console import Console

        self._engine_factory = engine_factory
        self._registry = registry
        self._theme = theme or DEFAULT_THEME
        self._console = console or Console()
        self._cwd = cwd
        self._model = model
        self._version = version
        self._read_line: ReadLine = read_line or self._default_read_line
        self._engine: "EngineHandle | None" = None
        self._app_ctx: object = None
        self._sink: TuiEventSink | None = None
        self._interrupt: InterruptHandler | None = None
        self._running = False
        self._turn_paused_elapsed = 0.0
        self._turn_pause_started_at = 0.0
        # Line-mode overlay facade: gives slash commands the same confirm /
        # select / report routing the TUI has, instead of degrading to a raw
        # scroll-region dump with no confirmation on destructive ops (§4.5).
        self._dialog_host = LineDialogHost(self._read_line, self._console, self._theme)

    def _require_app_ctx(self) -> "AppContext":
        return cast("AppContext", self._app_ctx)

    # ── line input ────────────────────────────────────────────────────────────
    async def _default_read_line(self, prompt: str) -> str:
        """Read one line without pulling in a heavyweight prompt library.

        The styled prompt is printed via ``Console`` (so it is themed); the raw
        line is read with stdlib ``input`` on the default executor so the event
        loop is not blocked (engine workers / approval can still resolve).
        """
        loop = asyncio.get_running_loop()
        self._console.print(prompt, end="")
        return await loop.run_in_executor(None, input)

    # ── lifecycle ───────────────────────────────────────────────────────────────
    async def run(self) -> int:
        ctx = RenderContext(
            theme=self._theme,
            ansi=self._console.is_terminal,
            width=max(40, self._console.width),
        )
        self._sink = TuiEventSink(LineLog(self._console), ctx)
        permissions = LinePermissionAdapter(
            self._read_line,
            self._console,
            on_wait_start=self._pause_turn_timer,
            on_wait_end=self._resume_turn_timer,
        )
        self._engine = await self._engine_factory(self._sink, permissions)
        self._interrupt = InterruptHandler(self._engine)
        # Adopt the engine's DI container so slash commands reach live subsystems
        # (mirrors AutoVerseApp._build_engine). Register user-invocable skills as
        # prompt-template commands when the engine exposes them.
        self._app_ctx = getattr(self._engine, "app", None)
        self._register_skill_commands()
        self._write_welcome()
        return await self._loop()

    def _register_skill_commands(self) -> None:
        sources = getattr(self._app_ctx, "extension_sources", ())
        skills = next((s for s in sources if getattr(s, "name", None) == "skills"), None)
        if skills is None:
            return
        try:
            from autoverse.frontends.repl.builtin_cmds import (
                register_skill_template_commands,
            )

            register_skill_template_commands(self._registry, skills)
        except Exception:  # pragma: no cover - a bad skill must not block startup
            pass

    async def _loop(self) -> int:
        prompt = f"[bold {self._theme.role('accent')}]›[/] "
        while True:
            try:
                raw = (await self._read_line(prompt)).strip()
            except EOFError:  # Ctrl+D — clean exit
                self._console.print("")
                return 0
            except KeyboardInterrupt:  # idle Ctrl+C — exit (line-mode convention)
                self._console.print("")
                return 0
            if not raw:
                continue
            if raw.startswith("/"):
                if await self._run_command(raw):
                    return 0  # command requested quit
                continue
            await self._run_turn(raw)

    # ── commands ──────────────────────────────────────────────────────────────
    def _command_ctx(self) -> CommandContext:
        # "ui" is the line-mode DialogHost (LineDialogHost, §4.5): destructive
        # commands confirm inline, long reports are framed instead of flooding,
        # and pickers use a numbered list. "sink" lets /details toggle fold
        # verbosity. (builtin_cmds still degrade to plain text if "ui" is absent.)
        return CommandContext(
            engine=self._engine,
            app=self._app_ctx,
            registry=self._registry,
            permissions=getattr(self._app_ctx, "permissions", None),
            is_running=self._running,
            extra={"sink": self._sink, "ui": self._dialog_host},
        )

    async def _run_command(self, raw: str) -> bool:
        """Dispatch a slash command. Return True if it requested quit."""
        result = await dispatch(raw, self._command_ctx(), self._registry)
        if result.should_clear:
            # Append-only can't erase scrolled-back history; mark the boundary
            # honestly instead of faking a clear (docs/guides/using-cli-and-tui.md §5.4).
            self._rule("上下文已清除 · 下方为新会话")
        if result.display:
            self._console.print(result.display)
        if result.prompt is not None:
            await self._run_turn(result.prompt)
        return result.should_quit

    # ── turns ───────────────────────────────────────────────────────────────────
    async def _run_turn(self, prompt: str) -> None:
        assert self._engine is not None and self._sink is not None
        assert self._interrupt is not None
        self._echo_user(prompt)
        self._sink.mark_user_turn()
        self._interrupt.reset()
        self._running = True
        started = time.monotonic()
        self._turn_paused_elapsed = 0.0
        self._turn_pause_started_at = 0.0
        snap = self._footer_snapshot()
        agen = self._engine.run(prompt)
        stop_cause = ""
        try:
            while True:
                try:
                    event = await agen.__anext__()
                    if event.type is EventType.TURN_END and isinstance(event.payload, TurnEndPayload):
                        stop_cause = event.payload.stop_cause
                except StopAsyncIteration:
                    break
                except KeyboardInterrupt:
                    # Two-stage Ctrl+C (interrupt → cancel): signal the engine and
                    # keep consuming so it can wind down (and a 2nd press escalates).
                    reason = self._interrupt.on_interrupt()
                    self._console.print(
                        f"[bold {self._theme.role('accent')}]⚡ {reason} requested[/]"
                    )
                    continue
        finally:
            self._sink.finalize_turn()
            self._running = False
            self._write_turn_footer(
                self._turn_elapsed_ms(started),
                interrupted=self._interrupt.count > 0,
                snap=snap,
            )
            if stop_cause == "PLAN_PROPOSED":
                await self._handle_plan_proposed()

    async def _handle_plan_proposed(self) -> None:
        extra = getattr(self._app_ctx, "extra", None)
        plan_mode = extra.get("plan_mode") if isinstance(extra, dict) else None
        if not (isinstance(plan_mode, dict) and plan_mode.get("pending_approval")):
            return
        plan = str(plan_mode.get("plan") or "").strip()
        self._console.print("[bold green]Plan proposed[/bold green]")
        ans = (await self._read_line("Approve plan? [y]es / [r]evise / [n]o: ")).strip().lower()
        if ans == "y":
            cast(dict[str, object], extra)["plan_mode"] = {
                "active": False,
                "pending_approval": False,
                "plan": plan,
                "approved": True,
            }
            self._console.print("[green]Plan approved. Executing now.[/green]")
            await self._run_turn(approved_plan_prompt(plan))
        elif ans == "r":
            cast(dict[str, object], extra)["plan_mode"] = {
                "active": True,
                "pending_approval": False,
                "reason": "user requested plan revision",
                "plan": plan,
            }
            self._console.print("[yellow]Plan returned for revision. Plan mode restored.[/yellow]")
        else:
            cast(dict[str, object], extra)["plan_mode"] = {
                "active": False,
                "pending_approval": False,
                "plan": plan,
                "cancelled": True,
            }
            self._console.print("[dim]Plan cancelled; no changes will be made.[/dim]")

    # ── presentation (mirrors AutoVerseApp, console-rendered) ────────────────────
    def _turn_elapsed_ms(self, started: float) -> float:
        if not started:
            return 0.0
        now = time.monotonic()
        elapsed = now - started - self._turn_paused_elapsed
        if self._turn_pause_started_at:
            elapsed -= now - self._turn_pause_started_at
        return max(elapsed * 1000.0, 0.0)

    def _pause_turn_timer(self) -> None:
        if not self._running or self._turn_pause_started_at:
            return
        self._turn_pause_started_at = time.monotonic()

    def _resume_turn_timer(self) -> None:
        if not self._turn_pause_started_at:
            return
        now = time.monotonic()
        self._turn_paused_elapsed += max(now - self._turn_pause_started_at, 0.0)
        self._turn_pause_started_at = 0.0

    def _echo_user(self, prompt: str) -> None:
        """Echo the user's message (MiMo UserMessage parity, console variant)."""
        from rich.text import Text

        accent = self._theme.role("accent")
        text = self._theme.role("text")
        self._console.print("")
        for ln in prompt.splitlines() or [""]:
            row = Text()
            row.append("▌ ", style=accent)
            row.append(ln, style=text)
            self._console.print(row)
        self._console.print("")

    def _write_welcome(self) -> None:
        """A composed welcome state (mirrors AutoVerseApp._write_welcome)."""
        from rich.text import Text

        t = self._theme
        accent = t.role("accent")
        muted = t.role("text_muted")
        title = Text()
        title.append("◆ ", style=accent)
        title.append("AutoVerse", style=f"bold {t.role('text')}")
        title.append("  ", style="")
        title.append("the evolvable agent harness", style=muted)
        self._console.print(title)
        meta = Text()
        meta.append("line mode", style=muted)
        if self._model:
            meta.append(" · ", style=t.role("text_dim"))
            meta.append(self._model, style=t.role("text"))
        meta.append(" · ", style=t.role("text_dim"))
        meta.append("/help 查看命令 · Ctrl+D 退出", style=muted)
        self._console.print(meta)
        rule_w = min(48, max(20, self._console.width - 6))
        self._console.print(Text("─" * rule_w, style=t.role("border_subtle")))

    def _rule(self, label: str) -> None:
        """A calm centred boundary line (mirrors the compaction divider)."""
        try:
            from rich.rule import Rule

            colour = self._theme.role("text_muted")
            self._console.print(
                Rule(title=f"[{colour}]{label}[/{colour}]", characters="─", style=colour)
            )
        except Exception:  # pragma: no cover - defensive
            self._console.print(f"── {label} ──")

    def _footer_snapshot(self) -> tuple[int, float, int, int, int]:
        """Snapshot the sink's cumulative usage so the footer renders a strictly
        per-turn view (mirrors AutoVerseApp._begin_turn_footer)."""
        s = self._sink
        if s is None:
            return (0, 0.0, 0, 0, 0)
        return (s.usage_seq, s.cost_usd, s.cum_cache_read, s.cum_cache_write, s.cum_prompt)

    def _write_turn_footer(
        self,
        elapsed_ms: float,
        *,
        interrupted: bool,
        snap: tuple[int, float, int, int, int],
    ) -> None:
        """The per-turn metadata line: ``▪ model · 12.3s · ↑1.2k ↓340 · cache 40% ·
        $0.0042`` (mirrors AutoVerseApp._compose_turn_footer, console-rendered)."""
        from rich.text import Text

        t = self._theme
        accent = t.role("accent")
        muted = t.role("text_muted")
        dim = t.role("text_dim")
        s = self._sink
        usage_seq0, cost0, cache_read0, cache_write0, prompt0 = snap

        row = Text()
        row.append("▪ ", style=accent)
        parts: list[tuple[str, str]] = [(self._model or "model", muted)]
        if elapsed_ms >= 1:
            parts.append((format_duration(elapsed_ms), muted))
        if s is not None and s.usage_seq > usage_seq0:
            if s.last_input or s.last_output:
                parts.append((f"↑{_kfmt(s.last_input)} ↓{_kfmt(s.last_output)}", muted))
            cache_read = s.cum_cache_read - cache_read0
            cache_write = s.cum_cache_write - cache_write0
            prompt = s.cum_prompt - prompt0
            if cache_read or cache_write:
                rate = (cache_read / prompt) if prompt > 0 else 0.0
                parts.append((f"cache {round(rate * 100)}%", muted))
            cost = s.cost_usd - cost0
            if cost > 0:
                parts.append((f"${cost:.4f}", accent))
        if interrupted:
            parts.append(("interrupted", t.role("warning")))
        for i, (txt, style) in enumerate(parts):
            if i:
                row.append(" · ", style=dim)
            row.append(txt, style=style)
        self._console.print(row)


__all__ = ["LineRepl", "LineLog", "LinePermissionAdapter", "LineDialogHost"]
