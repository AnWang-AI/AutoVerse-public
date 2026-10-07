"""Post-execution audit helpers (docs/reference/subagents.md §7.3).

The audit checks whether an isolated sub-agent (e2e-verifier) modified any
git-tracked source file. Only TRACKED source changes count — untracked
byproducts (logs / caches / ``__pycache__``) are tolerated. When tracked source
is modified the verdict is voided and the summary carries
``[VERIFICATION_TAINTED]`` (§7.3).

These are reusable helpers; the ``Isolator`` adapter (infra) wires
``git_diff_tracked`` into its own ``audit`` method by injecting its serialised,
fail-loud git plumbing via the *run* parameter. The runner always calls the
injected ``Isolator.audit`` — these functions let the policy live in core, and
let test doubles reuse the same semantics.

``core`` purity: imports only stdlib.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

POLICY_GIT_DIFF_EMPTY = "git_diff_must_be_empty"
TAINT_PREFIX = "[VERIFICATION_TAINTED]"

# A git invoker returning the raw stdout of ``git diff --name-only HEAD --``
# for a given cwd. The Isolator injects one backed by its serialised, fail-loud
# git plumbing; when omitted a private unsynchronised subprocess is used.
GitDiffRunner = Callable[[Path], Awaitable[str]]


async def git_diff_tracked(cwd: Path, run: GitDiffRunner | None = None) -> tuple[str, ...]:
    """Return the tracked files changed vs HEAD (``git diff --name-only HEAD --``).

    Untracked files do not appear in this list, so byproducts are tolerated.

    *run*, when provided, is an injected git invoker (e.g. the Isolator's
    serialised + fail-loud plumbing) returning the command's stdout for *cwd*.
    When omitted, a private unsynchronised subprocess is used — the core-pure
    default for tests / standalone use.
    """
    if run is not None:
        text = await run(cwd)
    else:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "HEAD",
            "--",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", errors="replace")
    return tuple(f for f in text.splitlines() if f.strip())


def post_audit(policy: str | None, changed: tuple[str, ...]) -> tuple[bool, str | None]:
    """Apply an audit policy to a set of changed tracked files (§7.3).

    Returns ``(tainted, note)``. For ``git_diff_must_be_empty`` any tracked
    change taints the verdict and the note carries ``[VERIFICATION_TAINTED]``.
    """
    if policy != POLICY_GIT_DIFF_EMPTY:
        return False, None
    if changed:
        return True, f"{TAINT_PREFIX} tracked source modified: {list(changed)}"
    return False, None


__all__ = [
    "POLICY_GIT_DIFF_EMPTY",
    "TAINT_PREFIX",
    "git_diff_tracked",
    "post_audit",
]
