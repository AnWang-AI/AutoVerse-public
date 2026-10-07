"""autoverse.core.tools.builtin — builtin tools.

Importing this package triggers tool self-registration via the per-module
`@register_tool` decorators.
"""

from __future__ import annotations

from autoverse.core.tools.builtin import (  # noqa: F401
    browser,
    context_archive,
    files,
    imagegen,
    interaction,
    memory,
    notebook,
    plan,
    session,
    shell,
    think,
    utility,
    web,
    worktree,
)

__all__ = [
    "files",
    "shell",
    "web",
    "think",
    "browser",
    "context_archive",
    "notebook",
    "utility",
    "imagegen",
    "interaction",
    "plan",
    "memory",
    "session",
    "worktree",
]
