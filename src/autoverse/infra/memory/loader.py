"""Memory loader (docs/reference/config.md).

Discovers the memory files named in ``Config.memory.files`` (default
``CLAUDE.md`` / ``AGENTS.md``) from ``cwd`` only — no upward traversal.
The discovered files are assembled into an :class:`AssembledMemory`. All
budgets (per-file char cap, combined cap) come from ``Config.memory`` — zero
scattered constants (P1/F2).

Per-role omission (``role=`` argument) is kept for compatibility with older
layered-memory designs; production child sub-agents currently use static prompts
and do not inherit the main prompt's memory block.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from autoverse.app.config import MemoryConfig

Scope = Literal["global", "project", "subdir", "session"]

_TRUNCATION_NOTE = "\n\n[... truncated at {n} chars ...]"

#: Per-role scope omission. A role maps to the set of scopes to DROP.
#: ``researcher`` keeps project/subdir conventions but omits the broad global
#: layer (and session preferences) so the survey stays neutral.
_ROLE_OMIT: Mapping[str, frozenset[Scope]] = {
    "researcher": frozenset({"global", "session"}),
    "scout": frozenset({"session"}),
    "builder": frozenset({"global", "project", "subdir", "session"}),
    "evaluator": frozenset({"global", "project", "subdir", "session"}),
}


@dataclass(frozen=True)
class MemoryBlock:
    """One discovered memory source, already truncated and annotated."""

    source: str  # relative path or "session-memory"; the injection label
    content: str
    scope: Scope


@dataclass(frozen=True)
class AssembledMemory:
    """Ordered memory blocks (outer → inner) plus a pure ``render``."""

    blocks: tuple[MemoryBlock, ...]

    def render(self) -> str:
        """Render as an annotated prompt fragment; empty memory → ``''``."""
        return "\n\n".join(
            f"<!-- memory source: {b.source} -->\n{b.content}" for b in self.blocks
        )


class MemoryLoader:
    """Assemble layered memory files into an :class:`AssembledMemory` (F8)."""

    def __init__(self, config: MemoryConfig) -> None:
        self._cfg = config

    def assemble(
        self,
        cwd: Path,
        *,
        home: Path | None = None,
        role: str | None = None,
    ) -> AssembledMemory:
        if not self._cfg.enabled:
            return AssembledMemory(blocks=())

        omit = _ROLE_OMIT.get(role, frozenset()) if role else frozenset()
        cwd = cwd.resolve()
        home_dir = (home or Path.home()).resolve()

        # Only cwd — no upward traversal.
        chain = self._dir_chain(cwd, home_dir)

        blocks: list[MemoryBlock] = []
        for directory, scope in chain:
            if scope in omit:
                continue
            for filename in self._cfg.files:
                block = self._read_block(directory, filename, scope)
                if block is not None:
                    blocks.append(block)

        blocks = self._apply_total_budget(blocks)
        return AssembledMemory(blocks=tuple(blocks))

    # ------------------------------------------------------------------ #
    def _dir_chain(self, cwd: Path, home: Path) -> list[tuple[Path, Scope]]:
        """Only ``cwd`` itself — no upward traversal.

        ``home`` is accepted for API compatibility but ignored.
        """
        return [(cwd, "subdir")]

    def _read_block(
        self, directory: Path, filename: str, scope: Scope
    ) -> MemoryBlock | None:
        path = directory / filename
        try:
            if not path.is_file():
                return None
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable/bad-encoding file is non-fatal: skip and continue.
            return None
        if not text.strip():
            return None
        text = self._truncate(text, self._cfg.max_file_chars)
        return MemoryBlock(source=filename, content=text, scope=scope)

    def _apply_total_budget(self, blocks: list[MemoryBlock]) -> list[MemoryBlock]:
        """Drop outermost blocks first when over the combined cap.

        The most specific (inner, later) blocks are preserved.
        """
        cap = self._cfg.max_total_chars
        if cap <= 0:
            return blocks
        kept: list[MemoryBlock] = []
        running = 0
        # Iterate inner → outer (reverse) so we keep the most specific first.
        for block in reversed(blocks):
            cost = len(block.content)
            if running + cost > cap:
                continue
            running += cost
            kept.append(block)
        kept.reverse()  # restore outer → inner order
        return kept

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if limit <= 0 or len(text) <= limit:
            return text
        note = _TRUNCATION_NOTE.format(n=limit)
        head = text[: max(0, limit - len(note))]
        return head + note


__all__ = ["AssembledMemory", "MemoryBlock", "MemoryLoader"]
