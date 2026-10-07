"""TUI Markdown widgets with deterministic fence highlighting.

Textual guesses a lexer when a fenced code block has no language tag. That is
helpful for source code, but prose diagrams (paths, arrows, CJK labels, box
drawing glyphs) can be misclassified and painted as syntax errors. AutoVerse
therefore treats unlabelled fences and indented code blocks as plain text while
preserving syntax highlighting for explicitly labelled fences.
"""

from __future__ import annotations

from textual.content import Content
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownFence


class AutoVerseMarkdownFence(MarkdownFence):
    """A Markdown fence that never guesses a lexer for unlabelled content."""

    @classmethod
    def highlight(
        cls, code: str, language: str, ansi: bool = False, dark: bool = False
    ) -> Content:
        return super().highlight(
            code,
            language or "text",
            ansi=ansi,
            dark=dark,
        )


class AutoVerseMarkdown(Markdown):
    """Native Textual Markdown with plain-text fallback for bare code fences."""

    BLOCKS = {
        **Markdown.BLOCKS,
        "fence": AutoVerseMarkdownFence,
        "code_block": AutoVerseMarkdownFence,
    }


__all__ = ["AutoVerseMarkdown", "AutoVerseMarkdownFence"]
