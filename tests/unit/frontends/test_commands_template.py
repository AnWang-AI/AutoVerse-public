"""Unit: prompt-template expansion + file command loading (docs/reference/commands.md §7).

Covers MiMo-parity argument substitution ($ARGUMENTS / $1..$n / last-absorbs /
append-when-no-placeholder / quote-aware split), hint derivation, @file ref
extraction, !`shell` injection, and the Markdown file command loader +
registration (skip-on-builtin-collision, prompt-template handler wiring).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoverse.frontends.repl.builtin_cmds import build_default_registry
from autoverse.frontends.repl.commands import CommandContext, CommandRegistry
from autoverse.frontends.repl.commands_loader import (
    discover_command_files,
    load_command_file,
    register_file_commands,
)
from autoverse.frontends.repl.commands_template import (
    derive_hints,
    expand_args,
    expand_files,
    expand_shell,
    expand_template,
    extract_file_refs,
    split_args,
)


# ── argument substitution ─────────────────────────────────────────────────────

def test_arguments_placeholder_takes_whole_string() -> None:
    assert expand_args("hello $ARGUMENTS!", "a b c") == "hello a b c!"


def test_positional_last_absorbs_remaining() -> None:
    # $2 is the highest placeholder → it absorbs args[1:] joined.
    assert expand_args("$1 :: $2", "one two three") == "one :: two three"


def test_positional_out_of_range_is_empty() -> None:
    assert expand_args("$1-$2-$3", "only") == "only--"


def test_no_placeholder_appends_args() -> None:
    assert expand_args("do the thing", "now please") == "do the thing\n\nnow please"


def test_no_placeholder_no_args_unchanged() -> None:
    assert expand_args("do the thing", "") == "do the thing"


def test_quote_aware_split() -> None:
    assert split_args('a "b c" d') == ["a", "b c", "d"]


def test_unbalanced_quotes_falls_back_to_whitespace() -> None:
    assert split_args('a "b c') == ["a", '"b', "c"]


# ── hints ──────────────────────────────────────────────────────────────────────

def test_derive_hints_numbered_then_arguments() -> None:
    assert derive_hints("$2 then $1 and $ARGUMENTS") == ("$1", "$2", "$ARGUMENTS")


def test_derive_hints_none() -> None:
    assert derive_hints("no placeholders here") == ()


# ── file refs ──────────────────────────────────────────────────────────────────

def test_extract_file_refs_dedup_order() -> None:
    assert extract_file_refs("see @a.py and @b.py and @a.py") == ["a.py", "b.py"]


# ── @path file injection ──────────────────────────────────────────────────────

def test_expand_files_appends_referenced_file_content(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("print('hi')\n", encoding="utf-8")
    out = expand_files("review @foo.py please", cwd=tmp_path)
    assert "review @foo.py please" in out
    assert "--- @foo.py ---" in out
    assert "print('hi')" in out


def test_expand_files_missing_ref_is_skipped(tmp_path: Path) -> None:
    # A reference that does not resolve to a file is left as-is, no block, no crash.
    out = expand_files("see @nope.py", cwd=tmp_path)
    assert out == "see @nope.py"


def test_expand_files_truncates_oversized(tmp_path: Path) -> None:
    (tmp_path / "big.dat").write_text("x" * 50, encoding="utf-8")
    out = expand_files("@big.dat", cwd=tmp_path, max_bytes=10)
    assert "…[truncated]" in out
    # Exactly 10 bytes of content injected (not the full 50).
    assert "xxxxxxxxxx" in out and "xxxxxxxxxxx" not in out


def test_expand_files_no_refs_unchanged(tmp_path: Path) -> None:
    assert expand_files("no refs here", cwd=tmp_path) == "no refs here"


async def test_expand_template_injects_files(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("HELLO", encoding="utf-8")
    out = await expand_template("summarise @readme.md for $1", "me", cwd=tmp_path)
    assert "summarise @readme.md for me" in out
    assert "--- @readme.md ---\nHELLO" in out


# ── shell injection ──────────────────────────────────────────────────────────────

async def test_expand_shell_substitutes_stdout(tmp_path: Path) -> None:
    out = await expand_shell("before !`echo hi` after", cwd=tmp_path)
    assert out == "before hi after"


async def test_expand_template_full_pipeline(tmp_path: Path) -> None:
    out = await expand_template("fix $1 (!`echo ctx`)", "auth", cwd=tmp_path)
    assert out == "fix auth (ctx)"


# ── file command loader ──────────────────────────────────────────────────────────

def _write_cmd(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_discover_finds_project_and_global(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    _write_cmd(cwd, ".autoverse/command/a.md", "A")
    _write_cmd(cwd, "commands/b.md", "B")
    _write_cmd(home, ".config/autoverse/command/c.md", "C")
    found = {p.stem for p in discover_command_files(cwd, home=home)}
    assert {"a", "b", "c"} <= found


def test_load_command_file_parses_frontmatter(tmp_path: Path) -> None:
    path = _write_cmd(
        tmp_path,
        ".autoverse/command/test.md",
        "---\ndescription: Run tests\nagent: validator\nsubtask: true\n---\n"
        "Run the suite for $ARGUMENTS and report failures.",
    )
    spec = load_command_file(path)
    assert spec.name == "test"
    assert spec.summary == "Run tests"
    assert spec.agent == "validator"
    assert spec.subtask is True
    assert spec.template is not None and spec.template.startswith("Run the suite")
    assert spec.hints == ("$ARGUMENTS",)


async def test_register_file_command_is_prompt_template(tmp_path: Path) -> None:
    _write_cmd(
        tmp_path,
        ".autoverse/command/component.md",
        "---\ndescription: Make a component\n---\nCreate component $ARGUMENTS.",
    )
    reg = build_default_registry()
    names = register_file_commands(reg, tmp_path, home=tmp_path / "nohome")
    assert "component" in names

    spec = reg.get("component")
    assert spec is not None and spec.group == "custom"
    ctx = CommandContext(engine=object(), app=None, registry=reg)
    result = await spec.handler(ctx, "Button")
    # Prompt-template command → returns prompt (not display) for the loop to run.
    assert result.prompt == "Create component Button."
    assert result.display == ""


def test_register_file_command_skips_builtin_collision(tmp_path: Path) -> None:
    # A file named like a built-in (`help`) must not crash startup; it is skipped.
    _write_cmd(tmp_path, ".autoverse/command/help.md", "custom help")
    reg = build_default_registry()
    names = register_file_commands(reg, tmp_path, home=tmp_path / "nohome")
    assert "help" not in names
    # The built-in /help survives unchanged (still the catalogue handler).
    assert reg.get("help") is not None


def test_register_file_command_first_wins_on_file_collision(tmp_path: Path) -> None:
    # Two files with the same stem in different dirs → first-discovered wins,
    # the rest are skipped silently (never raises — startup must not crash).
    _write_cmd(
        tmp_path,
        ".autoverse/command/dup.md",
        "---\ndescription: first\n---\nFIRST $ARGUMENTS",
    )
    _write_cmd(
        tmp_path,
        "commands/dup.md",  # lower precedence dir
        "---\ndescription: second\n---\nSECOND $ARGUMENTS",
    )
    reg = build_default_registry()
    names = register_file_commands(reg, tmp_path, home=tmp_path / "nohome")
    assert names.count("dup") == 1  # registered exactly once, no ValueError
    spec = reg.get("dup")
    assert spec is not None and spec.summary == "first"  # .autoverse/ wins


def test_completion_items_includes_hints(tmp_path: Path) -> None:
    _write_cmd(
        tmp_path,
        ".autoverse/command/greet.md",
        "---\ndescription: Greet\n---\nSay hi to $1.",
    )
    reg = build_default_registry()
    register_file_commands(reg, tmp_path, home=tmp_path / "nohome")
    items = {name: hints for name, _summary, hints in reg.completion_items()}
    assert items["/greet"] == ("$1",)


# ── skill template commands (G6, §4.5) ───────────────────────────────────────

async def test_register_skill_template_commands_submit_prompt() -> None:
    from autoverse.frontends.repl.builtin_cmds import (
        register_skill_template_commands,
    )
    from autoverse.infra.skills.registry import Skill, SkillRegistry

    skills = SkillRegistry(cwd=Path.cwd())
    skills.register(
        Skill(name="brainstorm", description="Brainstorm ideas", body="Brainstorm ideas.")
    )
    skills.register(Skill(name="internal", body="Internal guidance."))
    reg = build_default_registry()
    names = register_skill_template_commands(reg, skills)
    assert "brainstorm" in names
    assert "internal" in names

    spec = reg.get("brainstorm")
    assert spec is not None and spec.group == "extend"
    ctx = CommandContext(engine=object(), app=None, registry=reg)
    res = await spec.handler(ctx, "ML papers")
    assert res.prompt == "Brainstorm ideas.\n\nUser arguments: ML papers"
    assert res.display == ""


def test_register_skill_template_commands_skips_builtin_collision() -> None:
    from autoverse.frontends.repl.builtin_cmds import (
        register_skill_template_commands,
    )
    from autoverse.infra.skills.registry import Skill, SkillRegistry

    skills = SkillRegistry(cwd=Path.cwd())
    skills.register(Skill(name="help", body="custom help skill"))
    reg = build_default_registry()
    names = register_skill_template_commands(reg, skills)
    assert "help" not in names  # built-in /help wins, skill skipped (no crash)
