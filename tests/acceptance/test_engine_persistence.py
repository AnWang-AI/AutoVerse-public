"""gap10 acceptance — the built engine persists, traces, resolves secrets, loads
config, and snapshots.

Proves production wiring (gap10, A4/A5/A6/A8/A10/A12): ``EngineBuilder.build()``
defaults to a durable ``JSONLStore``, folds a ``TraceCollector`` in as a sink
downstream, exposes an env-first secret vault, honours a layered project config,
and (opt-in) captures a git-shadow snapshot for ``/rewind`` — all reachable from
``engine.app`` (the DI container the REPL slash commands consume).
See docs/reference/events-and-storage.md and docs/reference/config.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autoverse.app.builder import EngineBuilder
from autoverse.app.config import Config
from autoverse.app.options import EngineOptions
from autoverse.infra.observ import TraceCollector
from autoverse.infra.persist import JSONLStore
from autoverse.infra.snapshot import GitShadowSnapshot
from autoverse.types import TextDeltaPayload, TurnEndPayload, make_event

from tests._doubles import ScriptedProvider, make_git_repo


def _one_turn() -> list[object]:
    return [
        [
            make_event(TextDeltaPayload(text="ok")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="END_TURN")),
        ]
    ]


async def _drain(engine: object, prompt: str = "hello") -> None:
    async for _ in engine.run(prompt):  # type: ignore[attr-defined]
        pass


async def test_session_persists_to_jsonl_and_resumes(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    cfg = Config(session_dir=str(sessions))
    sid = "sess-persist"

    provider = ScriptedProvider(_one_turn())
    opts = EngineOptions(
        model="scripted", cwd=tmp_path, session_id=sid,
        settings=cfg, data_dir=tmp_path, llm_provider=provider,
    )
    engine = await EngineBuilder(opts).build()
    await _drain(engine, "first")

    # The session landed on disk as JSONL (durable store, not MemStore).
    path = sessions / f"{sid}.jsonl"
    assert path.is_file()
    msgs = await JSONLStore(sessions).load(sid)
    assert any(
        "first" in "".join(getattr(b, "text", "") for b in m.content)
        for m in msgs
    ), "user message should be persisted"
    assert len(msgs) >= 2, "user + assistant turn persisted"

    # A fresh engine over the SAME store/session resumes the history: the next
    # request the provider sees carries the earlier user message.
    provider2 = ScriptedProvider(_one_turn())
    opts2 = EngineOptions(
        model="scripted", cwd=tmp_path, session_id=sid,
        settings=cfg, data_dir=tmp_path, llm_provider=provider2,
    )
    engine2 = await EngineBuilder(opts2).build()
    await _drain(engine2, "second")
    req = provider2.requests[0]
    seen = "\n".join(
        "".join(getattr(b, "text", "") for b in m.content) for m in req.messages
    )
    assert "first" in seen, "resumed conversation should include the prior turn"


async def test_trace_collector_wired_and_persisted(tmp_path: Path) -> None:
    cfg = Config(session_dir=str(tmp_path / "sessions"))
    provider = ScriptedProvider(_one_turn())
    opts = EngineOptions(
        model="scripted", cwd=tmp_path, session_id="sess-trace",
        settings=cfg, data_dir=tmp_path, llm_provider=provider,
    )
    engine = await EngineBuilder(opts).build()
    await _drain(engine)

    trace = engine.app.trace
    assert isinstance(trace, TraceCollector)
    assert trace.model.turns, "the turn should be folded into the trace model"
    # Persisted incrementally under data_dir/traces/<sid>/.
    assert (tmp_path / "traces" / "sess-trace" / "trace.jsonl").is_file()


async def test_resumed_trace_continues_turn_indices(tmp_path: Path) -> None:
    cfg = Config(session_dir=str(tmp_path / "sessions"))
    sid = "sess-trace-resume"
    engine1 = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=tmp_path,
            session_id=sid,
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider(_one_turn()),
        )
    ).build()
    await _drain(engine1, "first")
    await engine1.aclose()

    engine2 = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=tmp_path,
            session_id=sid,
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider(_one_turn()),
        )
    ).build()
    await _drain(engine2, "second")
    assert [turn.turn_index for turn in engine2.app.trace.model.turns] == [0, 1]


async def test_secret_provider_env_first(tmp_path: Path) -> None:
    cfg = Config(session_dir=str(tmp_path / "sessions"))
    opts = EngineOptions(
        model="echo", cwd=tmp_path, session_id="sess-secret",
        settings=cfg, data_dir=tmp_path,
    )
    engine = await EngineBuilder(opts).build()
    secrets = engine.app.secrets
    assert secrets is not None

    os.environ["AUTOVERSE_TEST_TOKEN"] = "from-env"
    try:
        assert secrets.get("AUTOVERSE_TEST_TOKEN") == "from-env"  # env wins (F5)
    finally:
        del os.environ["AUTOVERSE_TEST_TOKEN"]
    assert secrets.get("AUTOVERSE_NEVER_SET") is None


async def test_project_config_append_in_prompt(tmp_path: Path) -> None:
    # A project .autoverse/config.json is discovered by the layered loader and its
    # system_prompt_append reaches the assembled system prompt.
    proj = tmp_path / "proj"
    (proj / ".autoverse").mkdir(parents=True)
    (proj / ".autoverse" / "config.json").write_text(
        json.dumps(
            {"system_prompt_append": "PROJECT_MARKER_XYZ", "session_dir": str(tmp_path / "s")}
        ),
        encoding="utf-8",
    )
    provider = ScriptedProvider(_one_turn())
    opts = EngineOptions(
        model="scripted", cwd=proj, session_id="sess-cfg",
        data_dir=tmp_path, llm_provider=provider,
    )
    engine = await EngineBuilder(opts).build()
    await _drain(engine)
    assert "PROJECT_MARKER_XYZ" in provider.requests[0].system


async def test_snapshot_opt_in_tracks_workspace(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    (repo / "untracked.txt").write_text("new content\n", encoding="utf-8")

    cfg = Config(session_dir=str(tmp_path / "sessions"), snapshot_enabled=True)
    provider = ScriptedProvider(_one_turn())
    opts = EngineOptions(
        model="scripted", cwd=repo, session_id="sess-snap",
        settings=cfg, data_dir=tmp_path, llm_provider=provider,
    )
    engine = await EngineBuilder(opts).build()
    snap = engine.app.snapshot
    assert isinstance(snap, GitShadowSnapshot)

    ref = await snap.track()
    assert ref is not None and ref.hash, "first track captures the work-tree"
    assert "untracked.txt" in ref.files
    # No change → no redundant snapshot.
    assert await snap.track() is None

    # Mutate then restore back to the captured tree.
    (repo / "untracked.txt").write_text("CHANGED\n", encoding="utf-8")
    changed = await snap.restore(ref)
    assert "untracked.txt" in changed
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "new content\n"


async def test_rewind_turn_restores_files_and_replaces_only_selected_turn(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    target = repo / "state.txt"
    target.write_text("before\n", encoding="utf-8")
    sessions = tmp_path / "sessions"
    cfg = Config(session_dir=str(sessions), snapshot_enabled=True)
    provider = ScriptedProvider(_one_turn() + _one_turn())
    engine = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=repo,
            session_id="turn-edit",
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=provider,
        )
    ).build()

    first_hash = await engine.capture_rewind_point()
    await _drain_with_metadata(
        engine, "keep this", {"rewind_id": "keep", "snapshot_hash": first_hash}
    )
    second_hash = await engine.capture_rewind_point()
    assert second_hash == first_hash  # unchanged files may share one immutable tree
    await _drain_with_metadata(
        engine, "wrong instruction", {"rewind_id": "replace", "snapshot_hash": second_hash}
    )
    # Simulate an auto-title produced from the soon-to-be-discarded turn. Rewind
    # must replace it from retained history even when title generation falls back.
    await JSONLStore(sessions).update_meta(
        "turn-edit", title="stale wrong-instruction title"
    )
    target.write_text("changed by wrong turn\n", encoding="utf-8")
    (repo / "created.txt").write_text("new file\n", encoding="utf-8")

    # A new process has an empty in-memory ref map and must resolve the persisted
    # snapshot hash from the shadow repository.
    resumed = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=repo,
            session_id="turn-edit",
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider([]),
        )
    ).build()
    prompt, attachments, changed = await resumed.rewind_turn("replace")

    assert prompt == "wrong instruction"
    assert attachments == ()
    assert {"state.txt", "created.txt"} <= set(changed)
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not (repo / "created.txt").exists()
    messages = await JSONLStore(sessions).load("turn-edit")
    text = "\n".join(
        getattr(block, "text", "") for message in messages for block in message.content
    )
    assert "keep this" in text
    assert "wrong instruction" not in text
    assert (await JSONLStore(sessions).load_meta("turn-edit")).title == "keep this"


async def test_rewind_first_turn_clears_title_then_replacement_retitles(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    sessions = tmp_path / "sessions"
    cfg = Config(session_dir=str(sessions), snapshot_enabled=True)
    engine = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=repo,
            session_id="replace-first-turn",
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider(_one_turn() + _one_turn()),
        )
    ).build()

    wrong_hash = await engine.capture_rewind_point()
    await _drain_with_metadata(
        engine,
        "wrong title source",
        {"rewind_id": "wrong-first", "snapshot_hash": wrong_hash},
    )
    assert (await JSONLStore(sessions).load_meta("replace-first-turn")).title == (
        "wrong title source"
    )

    await engine.rewind_turn("wrong-first")
    assert (await JSONLStore(sessions).load_meta("replace-first-turn")).title == ""

    replacement_hash = await engine.capture_rewind_point()
    await _drain_with_metadata(
        engine,
        "correct replacement title",
        {"rewind_id": "correct-first", "snapshot_hash": replacement_hash},
    )
    assert (await JSONLStore(sessions).load_meta("replace-first-turn")).title == (
        "correct replacement title"
    )


async def test_rewind_preserves_manually_locked_title(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    sessions = tmp_path / "sessions"
    cfg = Config(session_dir=str(sessions), snapshot_enabled=True)
    engine = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=repo,
            session_id="locked-rewind-title",
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider(_one_turn()),
        )
    ).build()
    snapshot_hash = await engine.capture_rewind_point()
    await _drain_with_metadata(
        engine,
        "discarded prompt",
        {"rewind_id": "locked-turn", "snapshot_hash": snapshot_hash},
    )
    await JSONLStore(sessions).update_meta(
        "locked-rewind-title", title="My Manual Title", title_locked=True
    )

    await engine.rewind_turn("locked-turn")

    meta = await JSONLStore(sessions).load_meta("locked-rewind-title")
    assert meta.title == "My Manual Title"
    assert meta.extra.get("title_locked") is True


async def test_rewind_restores_plan_goal_and_activity_metadata(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path)
    sessions = tmp_path / "sessions"
    cfg = Config(session_dir=str(sessions), snapshot_enabled=True)
    engine = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=repo,
            session_id="stateful-rewind",
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider(_one_turn()),
        )
    ).build()
    plan_before = {
        "active": True,
        "pending_approval": False,
        "reason": "planning before turn",
    }
    engine.app.extra["plan_mode"] = dict(plan_before)
    goal = engine.app.goal
    goal.set("stateful-rewind", "keep the original goal")
    await goal.persist("stateful-rewind")
    snapshot_hash = await engine.capture_rewind_point()

    await _drain_with_metadata(
        engine,
        "state-changing turn",
        {
            "rewind_id": "stateful-turn",
            "snapshot_hash": snapshot_hash,
            "plan_mode_snapshot": plan_before,
        },
    )
    prior_meta = await JSONLStore(sessions).load_meta("stateful-rewind")
    prior_activity = float(prior_meta.extra["last_activity_at"])

    engine.app.extra["plan_mode"] = {
        "active": False,
        "pending_approval": True,
        "plan": "discarded plan",
    }
    goal.mark_achieved("stateful-rewind", "discarded outcome")
    await goal.persist("stateful-rewind")

    await engine.rewind_turn("stateful-turn")

    assert engine.app.extra["plan_mode"] == plan_before
    restored_goal = goal.get("stateful-rewind")
    assert restored_goal is not None
    assert restored_goal.condition == "keep the original goal"
    assert goal.last_achieved("stateful-rewind") is None
    meta = await JSONLStore(sessions).load_meta("stateful-rewind")
    assert float(meta.extra["last_activity_at"]) >= prior_activity
    assert meta.extra["last_outcome"] == "ok"


async def test_rewind_turn_keeps_history_when_workspace_restore_fails(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path)
    sessions = tmp_path / "sessions"
    cfg = Config(session_dir=str(sessions), snapshot_enabled=True)
    engine = await EngineBuilder(
        EngineOptions(
            model="scripted",
            cwd=repo,
            session_id="failed-turn-edit",
            settings=cfg,
            data_dir=tmp_path,
            llm_provider=ScriptedProvider(_one_turn()),
        )
    ).build()
    snapshot_hash = await engine.capture_rewind_point()
    await _drain_with_metadata(
        engine,
        "must remain",
        {"rewind_id": "failed", "snapshot_hash": snapshot_hash},
    )

    async def fail_restore(_ref):
        raise RuntimeError("restore failed")

    engine.app.snapshot.restore = fail_restore  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="restore failed"):
        await engine.rewind_turn("failed")

    messages = await JSONLStore(sessions).load("failed-turn-edit")
    text = "\n".join(
        getattr(block, "text", "") for message in messages for block in message.content
    )
    assert "must remain" in text


async def _drain_with_metadata(
    engine: object, prompt: str, metadata: dict[str, object]
) -> None:
    async for _ in engine.run(prompt, message_metadata=metadata):  # type: ignore[attr-defined]
        pass
