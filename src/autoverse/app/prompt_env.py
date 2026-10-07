"""Environment section builder for the main-agent prompt (docs/architecture/runtime-flow.md §6.3).

Environment probing (git detection, platform, shell) has side effects, so it
lives in ``app/`` rather than ``core/prompt`` (which stays pure). The result is
an already-built string fed into :class:`PromptAssembler.environment` (P2/P3).

``app/`` may import ``infra``/``core`` + stdlib.
"""

from __future__ import annotations

import datetime as _dt
import os
import platform as _platform
import subprocess
from pathlib import Path

# Process-wide cache of git-detection results, keyed by resolved cwd. ``git
# rev-parse`` would otherwise fork on every prompt rebuild (including each
# sub-agent spawn) with no useful change between calls.
_GIT_DETECT_CACHE: dict[str, bool] = {}


def _detect_git(cwd: Path) -> bool:
    """Return True iff *cwd* is inside a git work tree (cached, fail-safe)."""
    key = str(cwd)
    cached = _GIT_DETECT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=key,
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_git = result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:  # noqa: BLE001 — git absent / timeout / not a dir → not a repo
        is_git = False
    _GIT_DETECT_CACHE[key] = is_git
    return is_git


def _shell_name() -> str:
    shell = os.environ.get("SHELL", "unknown")
    if "zsh" in shell:
        return "zsh"
    if "bash" in shell:
        return "bash"
    return shell


def build_environment(cwd: Path) -> str:
    """Build the Environment section (cwd / date / git / platform / shell).

    The date is intentionally NOT cached — each session-creation moment gets the
    real wall-clock time, so the model has a true anchor for "today" (P5).
    """
    lines = ["## 环境\n", f"- 工作目录: {cwd}"]
    now = _dt.datetime.now().astimezone()
    lines.append(f"- 今天的日期: {now.strftime('%Y-%m-%d (%A)')}")
    lines.append(f"- 本地时区: {now.tzname() or 'local'} (UTC{now.strftime('%z')})")
    lines.append(f"- 是否 git 仓库: {'是' if _detect_git(cwd) else '否'}")
    lines.append(f"- 平台: {_platform.system()} {_platform.release()}")
    lines.append(f"- Shell: {_shell_name()}")
    return "\n".join(lines)


__all__ = ["build_environment"]
