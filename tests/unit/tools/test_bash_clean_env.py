"""gap6 — bash clean-env strips credential-looking vars (docs/reference/tools.md §11.2, T-20)."""

from __future__ import annotations

import os
from pathlib import Path

from autoverse.core.tools.builtin.shell import BashArgs, BashTool

from tests.conftest import make_ctx


async def test_bash_strips_secret_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "super-secret")
    monkeypatch.setenv("GH_TOKEN", "tok123")
    monkeypatch.setenv("SAFE_VAR", "ok")
    ctx = make_ctx(tmp_path)
    out = await BashTool().run(BashArgs(command="echo K=$MY_API_KEY T=$GH_TOKEN S=$SAFE_VAR"), ctx)
    assert not out.is_error
    # secret-looking vars are absent in the child; the safe var survives.
    assert "super-secret" not in out.output
    assert "tok123" not in out.output
    assert "S=ok" in out.output


async def test_bash_reinjects_allowed_secrets(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)

    class _Secrets:
        def exposed_env(self) -> dict[str, str]:
            return {"OPENROUTER_API_KEY": "injected-value"}

    ctx.app.secrets = _Secrets()  # type: ignore[attr-defined]
    out = await BashTool().run(BashArgs(command="echo V=$OPENROUTER_API_KEY"), ctx)
    assert "V=injected-value" in out.output
