"""Bash tool + command classification (docs/reference/tools.md §11, T7/T8).

`classify_command` is a pure heuristic returning (kind, is_readonly). High-risk
patterns (rm -rf, kill, dd, mkfs, DROP TABLE, git push --force) are flagged so
the permission policy can require approval. `BashTool` never calls permissions
itself (P2); it supports a `check_mode` dry-run that rejects write commands and
allows read-only ones (T8).
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import shlex
import tempfile
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

_HIGH_RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b", "destructive_delete"),
    (r"\brm\s+-[a-z]*r\b.*\b-[a-z]*f\b", "destructive_delete"),
    (r"\bkill(all)?\b", "kill_process"),
    (r"\bdd\b", "raw_disk_write"),
    (r"\bmkfs\b", "format_disk"),
    (r"\bdrop\s+table\b", "drop_table"),
    (r"\bdrop\s+database\b", "drop_database"),
    (r"\bgit\s+push\b.*--force", "force_push"),
    (r"\bgit\s+push\s+.*-f\b", "force_push"),
    (r"\bgit\s+reset\s+--hard\b", "hard_reset"),
)

_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "ls", "cat", "grep", "rg", "find", "head", "tail", "wc", "echo",
        "pwd", "stat", "file", "which", "type", "env", "date", "whoami",
        "git", "diff", "sort", "uniq", "awk", "sed", "less", "more", "tree",
    }
)

# A small set of git subcommands that mutate state even though `git` is "read-ish".
_GIT_WRITE_SUBCMDS: frozenset[str] = frozenset(
    {"push", "commit", "reset", "rebase", "merge", "clean", "checkout", "rm", "mv", "add"}
)


def _strip_heredocs(cmd: str) -> str:
    """Remove heredoc bodies so their contents don't trigger false matches."""
    lines = cmd.splitlines()
    out: list[str] = []
    skip_until: str | None = None
    for line in lines:
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        out.append(line)
        if m:
            skip_until = m.group(1)
    return "\n".join(out)


def classify_command(cmd: str) -> tuple[str, bool]:
    """Return (kind, is_readonly). kind is a risk label or 'normal'/'readonly'."""
    body = _strip_heredocs(cmd)
    lowered = body.lower()
    for pattern, kind in _HIGH_RISK_PATTERNS:
        if re.search(pattern, lowered):
            return (kind, False)
    if is_readonly_command(cmd):
        return ("readonly", True)
    return ("normal", False)


def is_readonly_command(cmd: str) -> bool:
    """Best-effort: true if every pipeline segment is a known read-only command."""
    body = _strip_heredocs(cmd).strip()
    if not body:
        return True
    # Split on shell separators; reject if any redirection-to-file is present.
    if re.search(r"(?<![0-9<>])>(?!&)", body) or ">>" in body:
        return False
    segments = re.split(r"\|\||&&|\||;", body)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        tokens = seg.split()
        head = tokens[0]
        if head not in _READONLY_COMMANDS:
            return False
        if head == "git" and len(tokens) > 1 and tokens[1] in _GIT_WRITE_SUBCMDS:
            return False
    return True


_TRUSTED_CHECK_TOOLS: frozenset[str] = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "ruff",
        "mypy",
        "pyright",
        "tsc",
        "vitest",
        "eslint",
        "npm",
        "npx",
        "uv",
        "poetry",
    }
)
_TRUSTED_NPM_SCRIPTS: frozenset[str] = frozenset(
    {"test", "lint", "build", "typecheck", "check"}
)


def is_trusted_check_command(cmd: str) -> bool:
    """True for test/lint/build commands allowed by validator check mode.

    This is intentionally narrower than "non-read-only": no redirection, no high
    risk patterns, and every shell segment must be a directory change or a known
    verification command. The command still runs in the tool cwd/worktree.
    """
    kind, readonly = classify_command(cmd)
    if readonly:
        return True
    if kind != "normal":
        return False
    body = _strip_heredocs(cmd).strip()
    if re.search(r"(?<![0-9<>])>(?!&)", body) or ">>" in body:
        return False
    for raw_segment in re.split(r"\|\||&&|;", body):
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        if not tokens:
            continue
        head = Path(tokens[0]).name
        if head in {"cd", "pushd", "popd"}:
            continue
        if head not in _TRUSTED_CHECK_TOOLS:
            return False
        if head in {"python", "python3"}:
            if not (
                "-m" in tokens
                and any(t in {"pytest", "ruff", "mypy", "py_compile"} for t in tokens)
            ):
                return False
            continue
        if head == "npm":
            if len(tokens) < 3 or tokens[1] != "run" or tokens[2] not in _TRUSTED_NPM_SCRIPTS:
                return False
            continue
        if head == "npx":
            if len(tokens) < 2 or tokens[1] not in {"tsc", "vitest", "eslint", "pyright"}:
                return False
            continue
        if head in {"uv", "poetry"}:
            if len(tokens) < 3 or tokens[1] != "run":
                return False
            tool = Path(tokens[2]).name
            if tool not in {"pytest", "ruff", "mypy", "py_compile", "pyright"}:
                return False
            continue
    return True


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the whole process group of a child started with start_new_session.

    Best-effort: the child may already have exited (ProcessLookupError) or we may
    lack permission to signal it (unlikely for our own child). Falls back to
    killing just the child if the group lookup fails.
    """
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")


def _clean_env(ctx: ToolContext) -> dict[str, str]:
    """Build the child env with credential-looking vars stripped (docs/reference/tools.md §11.2).

    Removes ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` (etc.) so a bash command cannot
    exfiltrate the harness's credentials. Vars a tool legitimately needs are
    re-injected from ``app.secrets`` (the explicit allow-list), never inherited
    silently from the parent process environment (P5 secure-by-default)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.upper().endswith(sfx) for sfx in _SECRET_SUFFIXES)
    }
    secrets = getattr(getattr(ctx, "app", None), "secrets", None)
    allow = getattr(secrets, "exposed_env", None)
    if callable(allow):
        try:
            for name, value in dict(allow()).items():
                env[str(name)] = str(value)
        except Exception:  # noqa: BLE001 — never block bash on secret wiring
            pass
    return env


def _prepare_isolated_command(
    ctx: ToolContext, command: str
) -> tuple[str, str | None, str | None]:
    """Remap obvious source-repo absolute paths and block cd escapes.

    Bash is intentionally powerful and cannot be made path-safe with a full shell
    parser here. This guard closes the common leak from prompts that copy source
    absolute paths into an isolated run: those paths are rewritten to the
    worktree. Explicit directory changes outside the worktree/source mapping are
    denied so commands cannot trivially run in the main tree.
    """
    root = getattr(ctx, "isolation_root", None)
    if root is None:
        return command, None, None
    root_s = str(Path(root).expanduser().resolve(strict=False))
    source = getattr(ctx, "isolation_source_root", None)
    source_s = str(Path(source).expanduser().resolve(strict=False)) if source else ""
    remapped = command
    remap_note: str | None = None
    if source_s and source_s != root_s and source_s in remapped:
        placeholder = "__AUTOVERSE_ISOLATION_ROOT__"
        while placeholder in remapped:
            placeholder = f"_{placeholder}_"
        protected = remapped.replace(root_s, placeholder)
        remapped = protected.replace(source_s, root_s).replace(placeholder, root_s)
        if remapped != command:
            remap_note = (
                f"[worktree isolation] Redirected source-repo path {source_s} "
                f"to isolated worktree {root_s}. The output below is from the "
                "isolated worktree, not the main work-tree."
            )
    for raw_segment in re.split(r"\|\||&&|;|\n", remapped):
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if len(tokens) >= 2 and tokens[0] in {"cd", "pushd"}:
            target = Path(tokens[1])
            if target.is_absolute():
                target_s = str(target.expanduser().resolve(strict=False))
                if not target_s == root_s and not target_s.startswith(root_s + os.sep):
                    return remapped, (
                        f"isolated bash cannot change directory outside the worktree: {tokens[1]}"
                    ), remap_note
    return remapped, None, remap_note


class BashArgs(BaseModel):
    command: str = Field(..., description="The shell command to execute.")
    timeout: float | None = Field(None, description="Timeout in seconds.")
    restart: bool = Field(False, description="Restart the shell session.")
    check_mode: bool = Field(
        False, description="Dry-run: reject write commands, allow read-only ones."
    )


@register_tool
class BashTool(BaseTool):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = "Execute a bash command in a shell session."
    capability: ClassVar[CapabilityTier] = CapabilityTier.PROCESS
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = BashArgs

    async def run(self, args: BashArgs, ctx: ToolContext) -> ToolResult:
        command, isolation_error, remap_note = _prepare_isolated_command(ctx, args.command)
        if isolation_error is not None:
            return ToolResult.error(
                isolation_error,
                error_kind="workspace_escape",
                command=args.command,
                remapped=command != args.command,
                remap_note=remap_note or "",
            )
        if args.check_mode:
            kind, readonly = classify_command(command)
            if readonly or is_trusted_check_command(command):
                return ToolResult.ok(
                    "check_mode: command is allowed and would be executed.",
                    check_mode=True,
                    readonly=readonly,
                    trusted_check=not readonly,
                    kind=kind,
                    command=command,
                    remapped=command != args.command,
                )
            return ToolResult.error(
                f"check_mode: command is not read-only ({kind}); blocked.",
                error_kind="check_mode_blocked",
                readonly=False,
                kind=kind,
                command=command,
            )

        # Capture output to a temp file rather than an OS pipe. asyncio couples a
        # subprocess transport's completion to its stdout PIPE reaching EOF, so a
        # backgrounded daemon that inherits the pipe write-end keeps proc.wait()
        # (and communicate()) blocked for the daemon's entire lifetime — the hang
        # this fixes. A regular file fd has no such coupling: wait() returns as
        # soon as the foreground bash command exits, and we read the file after.
        out_file = tempfile.NamedTemporaryFile(
            prefix="autoverse-bash-", suffix=".out", delete=False
        )
        out_path = out_file.name
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_file.fileno(),
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(ctx.cwd),
                start_new_session=True,
                env=_clean_env(ctx),
            )
            out_file.close()
            try:
                if args.timeout is not None and args.timeout > 0:
                    await asyncio.wait_for(proc.wait(), timeout=args.timeout)
                else:
                    await proc.wait()
            except asyncio.TimeoutError:
                # The tool's own timeout fired: kill the whole group and report it.
                _kill_process_group(proc)
                timed_out = True
            except BaseException:  # noqa: BLE001 — kill child process group on cancellation
                # On cancel the dispatcher cancels this coroutine; the bash child
                # and its whole process group keep running unless we kill them
                # explicitly. start_new_session=True makes the child a group
                # leader, so killing the group (proc.pid == pgid) reaps it.
                _kill_process_group(proc)
                raise
            try:
                out = Path(out_path).read_bytes()
            except OSError:
                out = b""
        finally:
            if not out_file.closed:
                out_file.close()
            try:
                os.unlink(out_path)
            except OSError:
                pass
        if timed_out:
            return ToolResult.error(
                f"command timed out after {args.timeout:g}s and was killed.",
                error_kind="timeout",
                command=command,
                remapped=command != args.command,
                remap_note=remap_note or "",
            )
        text = out.decode("utf-8", errors="replace")
        shown = f"{remap_note}\n{text}" if remap_note else text
        if proc.returncode != 0:
            return ToolResult.error(
                shown or f"command exited with code {proc.returncode}",
                error_kind="exit_nonzero",
                command=command,
                remapped=command != args.command,
                remap_note=remap_note or "",
                exit_code=proc.returncode,
            )
        return ToolResult.ok(
            shown,
            command=command,
            remapped=command != args.command,
            remap_note=remap_note or "",
            exit_code=0,
        )


__all__ = [
    "classify_command",
    "is_readonly_command",
    "is_trusted_check_command",
    "BashArgs",
    "BashTool",
]
