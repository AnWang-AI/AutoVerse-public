"""PromptAssembler — main-agent system prompt assembly (docs/architecture/runtime-flow.md §4).

A pure assembler: it holds the static sections and accepts a handful of dynamic
inputs (tool list, session mode, environment, memory, extension fragments, user
append), then renders them into a single system string in a fixed section order
(§4.3 — the section order is the single authority, P1).

It does NOT probe the environment, read files, or call an LLM — every dynamic
input is fed in as an already-built string by the caller (P2/P3). One instance
per session (P4).

``core`` purity: imports only sibling ``core.prompt`` modules + stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from autoverse.core.prompt.sections import (
    PROMPT_SECTIONS,
    SESSION_GUIDANCE_INTERACTIVE,
    SESSION_GUIDANCE_ONE_SHOT,
)

# Offline fallback used only when no ToolRegistry is available (tests / one-shot).
_DEFAULT_TOOL_LIST = """\
## 可用工具

- read_file: 读取文件内容（可选行范围）
- write_file: 写入文件（自动建父目录）
- edit_file: 精确文本替换
- delete_file: 删除文件或目录
- glob: 按 glob 模式查找文件
- grep: 按正则搜索文件内容
- bash: 在持久会话中执行 shell 命令
- web_fetch: 抓取 URL 文本内容
- web_search: 联网搜索
- todo_write: 管理结构化待办列表
- notebook_edit: 编辑 Jupyter notebook 单元
- agent: 派发子代理处理自包含子任务"""


def build_tool_list(tools: Any | None) -> str:
    """Render the Available Tools section from a ToolRegistry (P1 derived view).

    ``tools`` is duck-typed: any object exposing ``schemas() -> list[dict]`` works.
    Falls back to the static default when no registry / no schemas are present.
    """
    if tools is None:
        return _DEFAULT_TOOL_LIST
    try:
        schemas = tools.schemas()
    except Exception:  # noqa: BLE001 — a broken registry must not break prompt build
        return _DEFAULT_TOOL_LIST
    if not schemas:
        return _DEFAULT_TOOL_LIST
    lines = ["## 可用工具\n"]
    for schema in schemas:
        name = str(schema.get("name", "")).strip()
        desc = str(schema.get("description", "") or "")
        # Keep only the first sentence to stay scannable (split on CJK/Latin period).
        first = desc.replace("。", ".").split(".")[0].strip()
        if name and first:
            lines.append(f"- {name}: {first}")
        elif name:
            lines.append(f"- {name}")
    return "\n".join(lines)


@dataclass
class PromptAssembler:
    """Assembles the main-agent system prompt (one per session, P4)."""

    tool_list: str = ""
    session_mode: str = "interactive"  # interactive | one_shot
    environment: str = ""
    memory: str = ""
    append: str = ""
    _fragments: dict[str, str] = field(default_factory=dict)

    # ── extension fragment intake (docs/reference/extensions.md projection sink) ───────────
    def add_fragment(self, name: str, fragment: str) -> None:
        """Register an extension source's prompt fragment (MCP tools / Hook
        constraints / Skills). Empty fragments are ignored (P8 boundary: purely
        passive mechanisms do not enter the prompt). Same-name overwrites
        (re-projection on reconnect)."""
        if fragment and fragment.strip():
            self._fragments[name] = fragment.strip()

    # ── session guidance variant (§6.2) ───────────────────────────────────────
    def _session_guidance(self) -> str:
        if self.session_mode == "one_shot":
            return SESSION_GUIDANCE_ONE_SHOT
        return SESSION_GUIDANCE_INTERACTIVE

    # ── render (single exit; section order declared here, P1) ──────────────────
    def render(self) -> str:
        parts: list[str] = [
            PROMPT_SECTIONS.intro,
            PROMPT_SECTIONS.doing_tasks,
            PROMPT_SECTIONS.methodology,
            PROMPT_SECTIONS.using_tools,
            PROMPT_SECTIONS.actions,
            PROMPT_SECTIONS.tone_style,
        ]
        if self.tool_list:
            parts.append(self.tool_list)
        parts.append(PROMPT_SECTIONS.subagent_workflow)
        # Extension fragments — stable name order so the prompt is deterministic.
        for name in sorted(self._fragments):
            parts.append(self._fragments[name])
        parts.append(self._session_guidance())
        parts.append(PROMPT_SECTIONS.compaction_awareness)
        parts.append(PROMPT_SECTIONS.system_behavior)
        if self.environment:
            parts.append(self.environment)
        if self.memory:
            parts.append("## 项目记忆\n\n" + self.memory)
        if self.append:
            parts.append("## 附加指令\n\n" + self.append)
        return "\n\n".join(p for p in parts if p)


__all__ = ["PromptAssembler", "build_tool_list"]
