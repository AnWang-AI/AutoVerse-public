"""Prompt-template expansion for slash commands (docs/reference/commands.md §4.1).

A *prompt-template* command carries a markdown ``template`` that is expanded
into a single user prompt and submitted as one turn, instead of a handler that
prints a string. Expansion has three pure-ish stages mirroring MiMo-Code's
``session/prompt.ts`` L3131-3172:

1. :func:`expand_args` — ``$ARGUMENTS`` / ``$1``..``$n`` substitution (the last
   positional placeholder absorbs the remaining args; with no placeholder and
   non-empty args the args are appended).
2. :func:`expand_shell` — ``!`cmd``` injection (runs the shell, substitutes
   stdout). The only stage with IO.
3. :func:`expand_files` — injects the contents of ``@path`` references as fenced
   blocks appended to the prompt (:func:`extract_file_refs` finds the refs).

:func:`derive_hints` derives the argument hints shown in help/completion.

Pure logic lives in ``frontends/repl`` — no ``core`` import (P3).
"""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path

# A positional placeholder like $1, $2 ... ($ARGUMENTS handled separately).
_POSITIONAL_RE = re.compile(r"\$(\d+)")
# Shell injection: !`cmd`  (mirrors MiMo SHELL_REGEX = /!`([^`]+)`/g).
_SHELL_RE = re.compile(r"!`([^`]+)`")
# File reference: @path (word-ish path, no whitespace).
_FILE_REF_RE = re.compile(r"(?<!\S)@([^\s`]+)")


def split_args(raw_args: str) -> list[str]:
    """Quote-aware split of the raw argument string (mirrors MiMo arg parsing)."""
    if not raw_args.strip():
        return []
    try:
        return shlex.split(raw_args)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split so we never crash.
        return raw_args.split()


def expand_args(template: str, raw_args: str) -> str:
    """Substitute ``$ARGUMENTS`` and ``$1``..``$n`` in *template*.

    Rules (identical to MiMo-Code):
    * ``$ARGUMENTS`` → the raw argument string verbatim.
    * ``$1``..``$n`` → positional args; the **highest-numbered** placeholder
      present absorbs all remaining args joined by spaces.
    * positional index out of range → empty string.
    * if the template has neither ``$N`` nor ``$ARGUMENTS`` but args are given,
      the args are appended to the template (separated by a blank line).
    """
    args = split_args(raw_args)

    positions = [int(m.group(1)) for m in _POSITIONAL_RE.finditer(template)]
    last = max(positions) if positions else 0

    def _replace(match: re.Match[str]) -> str:
        position = int(match.group(1))
        arg_index = position - 1
        if arg_index >= len(args):
            return ""
        if position == last:
            return " ".join(args[arg_index:])
        return args[arg_index]

    result = _POSITIONAL_RE.sub(_replace, template)
    uses_arguments = "$ARGUMENTS" in template
    result = result.replace("$ARGUMENTS", raw_args)

    if not positions and not uses_arguments and raw_args.strip():
        result = f"{result}\n\n{raw_args}"
    return result


def derive_hints(template: str) -> tuple[str, ...]:
    """Argument hints for display, e.g. ``('$1', '$2', '$ARGUMENTS')``.

    Mirrors MiMo ``hints()``: distinct sorted ``$N`` placeholders, then
    ``$ARGUMENTS`` if present.
    """
    out: list[str] = []
    numbered = sorted({m.group(0) for m in _POSITIONAL_RE.finditer(template)})
    out.extend(numbered)
    if "$ARGUMENTS" in template:
        out.append("$ARGUMENTS")
    return tuple(out)


def extract_file_refs(template: str) -> list[str]:
    """Return ``@path`` references in *template* (deduplicated, order-preserved)."""
    seen: dict[str, None] = {}
    for match in _FILE_REF_RE.finditer(template):
        seen.setdefault(match.group(1), None)
    return list(seen)


def expand_files(template: str, *, cwd: Path, max_bytes: int = 100_000) -> str:
    """Append the contents of each ``@path`` reference as a fenced block.

    For every distinct ``@path`` in *template* that resolves (relative to *cwd*)
    to a readable regular file, a ``--- @path ---`` block with the file's text is
    appended after the template body — so the model sees both the inline mention
    and the actual content (MiMo ``resolvePromptParts`` parity, adapted to a
    single text prompt). The ``@path`` tokens are left in place.

    Best-effort and non-throwing: missing / unreadable / oversized references are
    skipped (oversized ones are truncated to *max_bytes*), so a stray reference
    never breaks template expansion. References that do not resolve to a file are
    silently ignored (they remain as literal ``@path`` text in the prompt).
    """
    refs = extract_file_refs(template)
    if not refs:
        return template
    blocks: list[str] = []
    for ref in refs:
        path = (cwd / ref).expanduser()
        try:
            if not path.is_file():
                continue
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(data) > max_bytes:
            data = data[:max_bytes] + "\n…[truncated]"
        blocks.append(f"\n\n--- @{ref} ---\n{data}")
    return template + "".join(blocks)


async def expand_shell(template: str, *, cwd: Path, timeout: float = 30.0) -> str:
    """Replace each ``!`cmd``` with the command's stdout (run in *cwd*).

    Commands run sequentially via the user's shell. A failing command's stderr/
    partial stdout is still substituted (best-effort, matching MiMo nothrow).
    """
    matches = list(_SHELL_RE.finditer(template))
    if not matches:
        return template

    outputs: list[str] = []
    for match in matches:
        cmd = match.group(1)
        outputs.append(await _run_shell(cmd, cwd=cwd, timeout=timeout))

    index = 0

    def _replace(_match: re.Match[str]) -> str:
        nonlocal index
        out = outputs[index]
        index += 1
        return out

    return _SHELL_RE.sub(_replace, template)


async def _run_shell(cmd: str, *, cwd: Path, timeout: float) -> str:
    """Run *cmd* in a shell and return stdout (+ stderr on failure), trimmed."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"[command timed out after {timeout:.0f}s: {cmd}]"
        text = raw_out.decode("utf-8", "replace").strip()
        if proc.returncode and not text:
            text = raw_err.decode("utf-8", "replace").strip()
        return text
    except Exception as exc:  # noqa: BLE001 — best-effort injection, never crash
        return f"[command failed: {cmd}: {exc}]"


async def expand_template(template: str, raw_args: str, *, cwd: Path) -> str:
    """Full expansion: args → shell → ``@path`` file injection → trim."""
    expanded = expand_args(template, raw_args)
    expanded = await expand_shell(expanded, cwd=cwd)
    expanded = expand_files(expanded, cwd=cwd)
    return expanded.strip()


__all__ = [
    "split_args",
    "expand_args",
    "derive_hints",
    "extract_file_refs",
    "expand_files",
    "expand_shell",
    "expand_template",
]
