"""Unit: shell classification + BashTool check_mode (T7, T8)."""

from __future__ import annotations

import asyncio
import os
import time

from autoverse.core.tools.builtin.shell import (
    BashTool,
    _prepare_isolated_command,
    classify_command,
    is_readonly_command,
    is_trusted_check_command,
)
from autoverse.core.subagent.filter import CheckModeBashTool

from tests.conftest import make_ctx


def test_t7_high_risk_classified() -> None:
    assert classify_command("rm -rf /")[0] == "destructive_delete"
    assert classify_command("git push origin main --force")[0] == "force_push"
    assert classify_command("kill -9 123")[0] == "kill_process"
    assert classify_command("DROP TABLE users")[0] == "drop_table"


def test_t7_readonly_classified() -> None:
    assert classify_command("ls -la")[1] is True
    assert classify_command("cat foo.txt | grep bar")[1] is True
    assert is_readonly_command("git status") is True


def test_t7_write_not_readonly() -> None:
    assert is_readonly_command("echo hi > out.txt") is False
    assert is_readonly_command("git commit -m x") is False


def test_validator_trusted_check_commands() -> None:
    assert is_trusted_check_command("python -m pytest tests -q") is True
    assert is_trusted_check_command("python -m py_compile src/autoverse/__init__.py") is True
    assert is_trusted_check_command("npm run lint") is True
    assert is_trusted_check_command("python -c 'open(\"x\", \"w\").write(\"bad\")'") is False


def test_t7_heredoc_body_stripped() -> None:
    cmd = "cat <<EOF\nrm -rf /\nEOF"
    # The dangerous text is inside a heredoc body, not an executed command.
    assert classify_command(cmd)[0] != "destructive_delete"


async def test_t8_check_mode_blocks_write(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    res = await BashTool().run(
        BashTool.Args(command="rm -rf /tmp/x", check_mode=True), ctx
    )
    assert res.is_error and res.error_kind == "check_mode_blocked"


async def test_t8_check_mode_allows_readonly(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    res = await BashTool().run(
        BashTool.Args(command="ls -la", check_mode=True), ctx
    )
    assert res.succeeded and res.metadata["readonly"] is True


async def test_bash_executes(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    res = await BashTool().run(BashTool.Args(command="echo hello"), ctx)
    assert res.succeeded and "hello" in res.output


async def test_isolated_bash_remaps_source_path(tmp_path) -> None:
    source = tmp_path / "repo"
    iso = tmp_path / "repo" / ".autoverse" / "worktrees" / "box"
    source.mkdir(parents=True)
    iso.mkdir(parents=True)
    (source / "a.txt").write_text("source", encoding="utf-8")
    (iso / "a.txt").write_text("isolated", encoding="utf-8")
    ctx = make_ctx(iso, isolation_root=iso, isolation_source_root=source)

    res = await BashTool().run(BashTool.Args(command=f'cat "{source / "a.txt"}"'), ctx)

    assert res.succeeded
    assert "[worktree isolation] Redirected source-repo path" in res.output
    assert "not the main work-tree" in res.output
    assert res.output.strip().endswith("isolated")
    assert res.metadata["remapped"] is True
    assert "not the main work-tree" in res.metadata["remap_note"]


def test_isolated_bash_does_not_remap_existing_worktree_path(tmp_path) -> None:
    source = tmp_path / "repo"
    iso = source / ".autoverse" / "worktrees" / "box"
    source.mkdir(parents=True)
    iso.mkdir(parents=True)
    ctx = make_ctx(iso, isolation_root=iso, isolation_source_root=source)
    command = f"cd {iso} && pwd"

    remapped, err, note = _prepare_isolated_command(ctx, command)

    assert err is None
    assert note is None
    assert remapped == command


async def test_isolated_bash_blocks_cd_outside_worktree(tmp_path) -> None:
    source = tmp_path / "repo"
    iso = tmp_path / "repo" / ".autoverse" / "worktrees" / "box"
    source.mkdir(parents=True)
    iso.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    ctx = make_ctx(iso, isolation_root=iso, isolation_source_root=source)

    res = await BashTool().run(BashTool.Args(command=f'cd "{outside}" && pwd'), ctx)

    assert res.is_error and res.error_kind == "workspace_escape"


async def test_check_mode_bash_executes_trusted_pytest(tmp_path) -> None:
    test_file = tmp_path / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    res = await CheckModeBashTool().run(
        CheckModeBashTool.Args(command="python -m pytest test_ok.py -q"),
        ctx,
    )

    assert res.succeeded
    assert "passed" in res.output


async def test_check_mode_bash_executes_trusted_py_compile(tmp_path) -> None:
    source = tmp_path / "ok.py"
    source.write_text("def ok() -> str:\n    return 'ok'\n", encoding="utf-8")
    ctx = make_ctx(tmp_path)

    res = await CheckModeBashTool().run(
        CheckModeBashTool.Args(command="python -m py_compile ok.py"),
        ctx,
    )

    assert res.succeeded


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_bash_returns_when_background_child_holds_stdout(tmp_path) -> None:
    """A backgrounded child inherits the stdout pipe; the tool must not wait on it.

    Reproduces the daemon-launch hang: ``nohup sleep 30 & echo ...`` leaves a
    long-lived child holding the write-end of the harness stdout pipe, so the
    pipe never reaches EOF. The tool must return as soon as the foreground bash
    command exits, not block until the daemon dies.
    """
    ctx = make_ctx(tmp_path)
    t0 = time.monotonic()
    res = await BashTool().run(
        BashTool.Args(command='nohup sleep 30 & echo "PID: $!"', timeout=15), ctx
    )
    elapsed = time.monotonic() - t0
    try:
        assert res.succeeded, res.output
        assert "PID:" in res.output
        assert elapsed < 8, f"bash hung for {elapsed:.1f}s on a backgrounded child"
    finally:
        # Reap the orphaned sleep so it does not linger past the test.
        for line in res.output.splitlines():
            if line.startswith("PID:"):
                try:
                    os.kill(int(line.split(":", 1)[1]), 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass


async def test_bash_returns_when_compound_background_holds_stdout(tmp_path) -> None:
    """The ``cd && nohup ... &`` daemon pattern from the bug report.

    Backgrounding a compound list forces bash to keep a subshell alive as the
    job parent; that subshell inherits the stdout pipe even though the daemon
    itself redirects to a file. The tool must still return promptly.
    """
    ctx = make_ctx(tmp_path)
    log = tmp_path / "d.log"
    command = f'cd "{tmp_path}" && nohup sleep 30 > "{log}" 2>&1 & echo "PID: $!"'
    t0 = time.monotonic()
    res = await BashTool().run(BashTool.Args(command=command, timeout=15), ctx)
    elapsed = time.monotonic() - t0
    try:
        assert res.succeeded, res.output
        assert "PID:" in res.output
        assert elapsed < 8, f"bash hung for {elapsed:.1f}s on a compound background job"
    finally:
        for line in res.output.splitlines():
            if line.startswith("PID:"):
                try:
                    os.kill(int(line.split(":", 1)[1]), 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass


async def test_bash_timeout_enforced(tmp_path) -> None:
    """``args.timeout`` must bound a foreground command and kill its group."""
    ctx = make_ctx(tmp_path)
    pid_file = tmp_path / "pid.txt"
    command = f'echo $$ > "{pid_file}"; sleep 30'
    t0 = time.monotonic()
    res = await BashTool().run(BashTool.Args(command=command, timeout=1), ctx)
    elapsed = time.monotonic() - t0

    assert res.is_error and res.error_kind == "timeout", res.output
    assert elapsed < 6, f"timeout not enforced: returned after {elapsed:.1f}s"

    # The process group must have been killed, not leaked.
    if pid_file.exists() and pid_file.read_text().strip():
        child_pid = int(pid_file.read_text().strip())
        for _ in range(200):
            if not _pid_alive(child_pid):
                break
            await asyncio.sleep(0.01)
        assert not _pid_alive(child_pid), "bash child leaked after timeout"


async def test_bash_captures_full_output_of_fast_command(tmp_path) -> None:
    """Regression guard: normal commands still capture all stdout to EOF."""
    ctx = make_ctx(tmp_path)
    res = await BashTool().run(
        BashTool.Args(command="for i in 1 2 3; do echo line$i; done"), ctx
    )
    assert res.succeeded
    assert "line1" in res.output and "line2" in res.output and "line3" in res.output


async def test_bash_cancel_kills_child_process_group(tmp_path) -> None:
    """Cancelling BashTool.run must SIGKILL the whole bash process group (no leak)."""
    ctx = make_ctx(tmp_path)
    pid_file = tmp_path / "pid.txt"
    # Record the child shell's PGID, then sleep long enough to outlive the test.
    command = f'echo $$ > "{pid_file}"; sleep 30'

    task = asyncio.create_task(BashTool().run(BashTool.Args(command=command), ctx))

    # Wait until the child has written its pid (proves the process group exists).
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists() and pid_file.read_text().strip(), "child never started"
    child_pid = int(pid_file.read_text().strip())

    # Trigger cancellation (mirrors race_with_abort cancelling the run() coro).
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The process group started with start_new_session must be gone.
    for _ in range(200):
        if not _pid_alive(child_pid):
            break
        await asyncio.sleep(0.01)
    assert not _pid_alive(child_pid), "bash child process leaked after cancel"
    # The process group itself must be gone too.
    try:
        os.killpg(child_pid, 0)
        pgrp_gone = False
    except ProcessLookupError:
        pgrp_gone = True
    except PermissionError:
        pgrp_gone = False
    assert pgrp_gone, "bash process group leaked after cancel"
