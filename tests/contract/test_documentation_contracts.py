"""Documentation contracts for current-reference hygiene.

Docs should contain current, useful project knowledge. Historical implementation
plans, stale checklists, copied history, and old top-level specs are intentionally
absent; historical retrieval belongs to git or external issue systems.
"""

from __future__ import annotations

import ast
from dataclasses import fields
import re
import runpy
import shlex
import tomllib
from pathlib import Path
from typing import Iterable, get_args

from autoverse.app.config import (
    BrowserConfig,
    CompactionConfig,
    Config,
    MCPServerConfig as AppMCPServerConfig,
    MemoryConfig,
    ModelConfig,
    ModelTiersConfig,
    PluginsConfig,
    RecoveryConfig,
)
from autoverse.app.workflow.pipelines import builtin_workflow_definitions
from autoverse.core.subagent.definition import AGENT_DEFINITIONS
from autoverse.frontends.cli.main import build_parser
from autoverse.frontends.repl.builtin_cmds import build_default_registry
from autoverse.frontends.repl.commands_loader import _candidate_dirs
from autoverse.infra.config.loader import _LAYER_ORDER
from autoverse.infra.hooks.engine import HookDef, HookEvent, HookInput, HookOutcome
from autoverse.infra.llm.registry import (
    MODEL_ALIASES,
    MODEL_REGISTRY,
    _passthrough_spec,
    resolve_alias,
)
from autoverse.infra.llm.transport.credential import AuthContext, CredentialResolver
from autoverse.infra.mcp.manager import (
    MCP_DEFAULT_TIER,
    ListMcpResourcesTool,
    MCPServerConfig as RuntimeMCPServerConfig,
    ReadMcpResourceTool,
    ServerStatus,
)
from autoverse.infra.skills.spec import SKILL_NAME_RE, SkillDiagnostic
from autoverse.infra.skills.tool import SkillArgs
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import PAYLOAD_FOR
from autoverse.types.llm import ProviderCapabilities

ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

CURRENT_TEXT_GLOBS = (
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "docs/**/*.md",
    "docs/**/*.yml",
    "scripts/**/*.py",
    "src/**/*.py",
    "src/**/*.js",
    "tests/**/*.py",
)

REMOVED_DOC_REFERENCES = (
    "docs/DOCS_PARITY.md",
    "docs/archive/",
    "docs/archive/legacy-design/",
    "archive/legacy-design/",
    "docs/audits/",
    "audits/",
    "docs/roadmap/",
    "roadmap/",
    "docs/plan/",
    "docs/check/",
    "docs/templates/compat-index.md",
    "docs/templates/historical-plan.md",
    "docs/templates/historical-audit.md",
    "docs/templates/archived-design.md",
    "docs/templates/roadmap.md",
    "docs/architecture/overview.md",
    "docs/design/extensions-and-gateway.md",
    "design/extensions-and-gateway.md",
    "docs/adr/0014-snapshot-port.md",
    "docs/acceptance.md",
    "docs/apple-app.md",
    "docs/config-and-secrets.md",
    "docs/contracts.md",
    "docs/core-loop.md",
    "docs/extensibility.md",
    "docs/gateway.md",
    "docs/isolation.md",
    "docs/line-repl.md",
    "docs/llm-and-protocol.md",
    "docs/methodology.md",
    "docs/mobile-ui.md",
    "docs/new-harness-design.md",
    "docs/observability.md",
    "docs/permissions.md",
    "docs/persistence.md",
    "docs/repl-and-cli.md",
    "docs/session-sidebar.md",
    "docs/slash-commands.md",
    "docs/subagent.md",
    "docs/system-prompt.md",
    "docs/tools.md",
    "docs/tui-compat.md",
    "docs/webui-file-preview.md",
    "docs/webui-spec.md",
    "docs/webui-ui.md",
    "docs/webui-vs-tui.md",
    "docs/webui.md",
    "docs/workflow.md",
)

OLD_TOPIC_SECTION_RE = re.compile(
    r"\b(?:acceptance|apple-app|config-and-secrets|core-loop|loop|state|"
    r"extensibility|gateway|isolation|line-repl|llm-and-protocol|methodology|"
    r"mobile-ui|new-harness-design|observability|permissions|persistence|"
    r"repl-and-cli|session-sidebar|slash-commands|subagent|system-prompt|"
    r"tui-compat|webui-file-preview|webui-spec|webui-ui|webui-vs-tui|"
    r"workflow)\.md\s*§"
)

OLD_BARE_SECTION_RE = re.compile(
    r"\b(?:acceptance|apple-app|config-and-secrets|core-loop|loop|state|"
    r"llm-and-protocol|slash-commands|repl-and-cli|line-repl|tui-compat|"
    r"webui-file-preview|webui-spec|webui-ui|webui-vs-tui)\s*§"
)

LEGACY_DOC_FILENAMES_RE = re.compile(
    r"(?<![\w-])(?:acceptance|apple-app|config-and-secrets|core-loop|loop|state|"
    r"extensibility|gateway|isolation|line-repl|llm-and-protocol|methodology|"
    r"mobile-ui|new-harness-design|observability|permissions|persistence|"
    r"repl-and-cli|session-sidebar|slash-commands|subagent|system-prompt|"
    r"tools|tui-compat|webui-file-preview|webui-spec|webui-ui|webui-vs-tui|"
    r"webui|workflow)\.md\b"
)

CURRENT_DOC_PREFIXES = (
    "docs/architecture/",
    "docs/reference/",
    "docs/guides/",
    "docs/design/",
    "docs/adr/",
    "architecture/",
    "reference/",
    "guides/",
    "design/",
    "adr/",
)

DOC_IMPACT_PATH = ROOT / "docs" / "doc-impact.yml"

REQUIRED_TEMPLATE_FILES = {
    "docs/templates/README.md",
    "docs/templates/current-index.md",
    "docs/templates/guide.md",
    "docs/templates/reference.md",
    "docs/templates/architecture.md",
    "docs/templates/design.md",
    "docs/templates/adr.md",
}

REQUIRED_DOC_HEADER_PREFIXES = (
    "> 当前事实来源：",
    "> 维护规则：",
)

GUIDE_REQUIRED_SECTIONS = (
    "## 前置条件",
    "## 相关文档",
)

BANNED_UNVERIFIED_PHRASES = (
    "拆分前全文档案",
    "全文档案见",
    "仍需以当前代码为准核对",
    "需要以当前代码为准核对",
    "逐项核对后再标为",
    "清理旧 check 文档",
    "loader missing / SkillTool missing",
    "SQLite store",
    "具体 SQLite、JSONL",
    "persistence 与 memory 都用 SQLite",
    "## 实施路线",
)


def _iter_current_text_files() -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in CURRENT_TEXT_GLOBS:
        for path in ROOT.glob(pattern):
            if not path.is_file() or path == THIS_FILE:
                continue
            rel = path.relative_to(ROOT)
            if rel in seen:
                continue
            seen.add(rel)
            yield path


def _iter_documentation_markdown_files() -> Iterable[Path]:
    yield ROOT / "README.md"
    yield ROOT / "AGENTS.md"
    yield from sorted((ROOT / "docs").rglob("*.md"))


def _markdown_table_values(path: str, column: str) -> set[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    lines = text.splitlines()
    values: set[str] = set()
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        headers = [cell.strip(" `") for cell in _split_markdown_table_cells(line)]
        if column not in headers:
            continue
        index = headers.index(column)
        for row in lines[i + 2 :]:
            if not row.startswith("|"):
                break
            cells = [cell.strip() for cell in _split_markdown_table_cells(row)]
            if len(cells) <= index:
                continue
            value = cells[index].strip(" `")
            if value:
                values.add(value)
        return values
    raise AssertionError(f"{path} must contain a markdown table with column {column!r}")


def _markdown_table_rows_after_heading(path: str, heading: str) -> list[dict[str, str]]:
    text = (ROOT / path).read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"{path} must contain heading {heading!r}") from exc

    table_start = None
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") and lines[i] != heading:
            break
        if lines[i].startswith("|"):
            table_start = i
            break
    if table_start is None:
        raise AssertionError(f"{path} {heading!r} must contain a markdown table")

    headers = [
        cell.strip(" `") for cell in _split_markdown_table_cells(lines[table_start])
    ]
    rows: list[dict[str, str]] = []
    for row in lines[table_start + 2 :]:
        if not row.startswith("|"):
            break
        cells = [cell.strip() for cell in _split_markdown_table_cells(row)]
        rows.append(dict(zip(headers, cells, strict=False)))
    return rows


def _markdown_table_rows_with_columns(path: str, columns: set[str]) -> list[dict[str, str]]:
    text = (ROOT / path).read_text(encoding="utf-8")
    lines = text.splitlines()
    rows: list[dict[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("|"):
            i += 1
            continue
        headers = [cell.strip(" `") for cell in _split_markdown_table_cells(line)]
        if columns <= set(headers):
            for row in lines[i + 2 :]:
                if not row.startswith("|"):
                    break
                cells = [cell.strip() for cell in _split_markdown_table_cells(row)]
                rows.append(dict(zip(headers, cells, strict=False)))
        i += 1
    return rows


def _split_markdown_table_cells(line: str) -> list[str]:
    """Split a Markdown table row while preserving escaped pipes inside cells."""
    body = line.strip().strip("|")
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if char == "|":
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        i += 1
    cells.append("".join(buf))
    return cells


def _parse_doc_default(value: str) -> object:
    raw = _strip_code(value)
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    return ast.literal_eval(raw)


def _config_doc_default_value(model_cls: type[object], field_name: str) -> object:
    field = model_cls.model_fields[field_name]  # type: ignore[attr-defined]
    if field.default_factory is not None:
        value = field.default_factory()
    else:
        value = field.default
    if hasattr(value, "model_dump"):
        return {}
    return value


def _strip_code(value: str) -> str:
    return value.strip().strip("`")


def _markdown_anchor(heading: str) -> str:
    slug = heading.strip().lower()
    slug = re.sub(r"^#+\s*", "", slug)
    slug = re.sub(r"[`*_~]", "", slug)
    slug = re.sub(r"[^\w\u3400-\u9fff -]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug.strip("-")


def _split_doc_list(value: str) -> list[str]:
    return [
        cell.strip().strip("`")
        for cell in value.split(",")
        if cell.strip()
    ]


def test_removed_documentation_structures_do_not_exist() -> None:
    removed_paths = [
        "docs/archive",
        "docs/archives",
        "docs/audits",
        "docs/audit",
        "docs/roadmap",
        "docs/plan",
        "docs/plans",
        "docs/check",
        "docs/checks",
        "docs/done",
        "docs/todo",
        "docs/todos",
        "docs/templates/compat-index.md",
        "docs/templates/historical-plan.md",
        "docs/templates/historical-audit.md",
        "docs/templates/archived-design.md",
        "docs/templates/roadmap.md",
        "docs/architecture/overview.md",
        "docs/design/extensions-and-gateway.md",
        "docs/adr/0014-snapshot-port.md",
    ]
    offenders = [path for path in removed_paths if (ROOT / path).exists()]
    assert not offenders, "\n".join(offenders)


def test_docs_do_not_recreate_done_todo_archive_taxonomy() -> None:
    banned_components = {
        "archive",
        "archives",
        "audit",
        "audits",
        "check",
        "checks",
        "done",
        "plan",
        "plans",
        "roadmap",
        "todo",
        "todos",
    }
    offenders: list[str] = []
    for path in (ROOT / "docs").rglob("*"):
        rel = path.relative_to(ROOT)
        components = {part.lower() for part in rel.parts}
        if components & banned_components:
            offenders.append(str(rel))
    assert not offenders, "\n".join(sorted(offenders))


def test_current_sources_do_not_reference_removed_docs() -> None:
    offenders: list[str] = []
    for path in _iter_current_text_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(ref in line for ref in REMOVED_DOC_REFERENCES):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
                continue
            if OLD_TOPIC_SECTION_RE.search(line) or OLD_BARE_SECTION_RE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_docs_root_contains_only_current_entry_points() -> None:
    allowed = {"README.md", "DOCUMENTATION.md"}
    offenders = [
        path.name
        for path in (ROOT / "docs").glob("*.md")
        if path.name not in allowed
    ]
    assert not offenders, "\n".join(sorted(offenders))


def test_markdown_links_point_to_existing_docs() -> None:
    offenders: list[str] = []
    link_re = re.compile(r"\[[^\]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)")
    for path in _iter_documentation_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in link_re.finditer(line):
                target = match.group(1)
                if "://" in target:
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: missing link target {target}"
                )
    assert not offenders, "\n".join(offenders)


def test_markdown_anchor_links_point_to_existing_headings() -> None:
    offenders: list[str] = []
    link_re = re.compile(
        r"\[[^\]]+\]\((?:(?P<target>[^)#]+\.md))?#(?P<anchor>[^)]+)\)"
    )
    for path in _iter_documentation_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in link_re.finditer(line):
                target = match.group("target")
                raw_anchor = match.group("anchor")
                if target is not None and "://" in target:
                    continue
                resolved = (
                    path.resolve()
                    if target is None
                    else (path.parent / target).resolve()
                )
                if not resolved.exists():
                    continue
                headings = {
                    _markdown_anchor(heading)
                    for heading in resolved.read_text(encoding="utf-8").splitlines()
                    if heading.startswith("#")
                }
                if raw_anchor not in headings:
                    display_target = target or path.name
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: missing anchor "
                        f"{display_target}#{raw_anchor}"
                    )
    assert not offenders, "\n".join(offenders)


def test_documented_repo_paths_exist() -> None:
    offenders: list[str] = []
    path_re = re.compile(
        r"`((?:AGENTS\.md|README\.md|pyproject\.toml|docs/|src/|tests/|scripts/)"
        r"[^`\\s)]*)`"
    )
    for path in _iter_documentation_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in path_re.finditer(line):
                raw = match.group(1).rstrip(".,:;")
                raw = raw.split("#", 1)[0]
                if "<" in raw or ">" in raw:
                    continue
                if "*" in raw:
                    if not list(ROOT.glob(raw)):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{lineno}: no match for {raw}"
                        )
                    continue
                if not (ROOT / raw).exists():
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: missing path {raw}"
                    )
    assert not offenders, "\n".join(offenders)


def test_testing_reference_lists_current_validation_commands() -> None:
    text = (ROOT / "docs" / "reference" / "testing.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "guides" / "getting-started.md").read_text(
        encoding="utf-8"
    )

    for document in (text, readme, quickstart):
        assert "pytest" in document
        assert "mypy src/autoverse" in document
        assert "import-linter lint" in document
        assert not re.search(r"^import-linter$", document, flags=re.MULTILINE)
    assert "git diff --check" in text
    assert "../DOCUMENTATION.md#文档影响治理协议" in text
    assert "python scripts/check_doc_impact.py --strict" not in text
    assert "python scripts/check_doc_impact.py --strict --staged" not in text


def test_install_examples_use_defined_optional_extras() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    defined_extras = set(pyproject["project"].get("optional-dependencies", ()))
    install_re = re.compile(r'pip install -e "\.\[([^\]]+)\]"')
    offenders: list[str] = []

    for path in (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in install_re.finditer(line):
                extras = {extra.strip() for extra in match.group(1).split(",")}
                missing = extras - defined_extras
                if missing:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: undefined extras "
                        f"{', '.join(sorted(missing))}"
                    )
    assert not offenders, "\n".join(offenders)


def test_pyproject_comments_do_not_reference_stale_doc_sections() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "§" not in text
    assert "docs/guides/using-cli-and-tui.md §" not in text


def test_agents_does_not_duplicate_dependency_version_lists() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "pyproject.toml" in agents
    assert "**Runtime:**" not in agents
    assert "**Dev:**" not in agents
    assert "**Optional:**" not in agents


def test_source_modules_match_agents_module_conventions() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "from __future__ import annotations" in agents
    assert "每个公开模块都必须有 `__all__`" in agents
    assert "架构敏感模块" in agents
    assert "小型包标记模块" in agents
    assert "Module docstrings are triple-quoted, reference design docs by name" not in agents
    assert "Pydantic config models (user-facing subset)" not in agents
    assert "配置文件/环境变量加载和写入" in agents

    offenders: list[str] = []
    for path in sorted((ROOT / "src" / "autoverse").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if ast.get_docstring(tree) is None:
            offenders.append(f"{path.relative_to(ROOT)}: missing module docstring")
        if not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        ):
            offenders.append(f"{path.relative_to(ROOT)}: missing future annotations")
        if not any(
            (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "__all__"
                    for target in node.targets
                )
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "__all__"
            )
            for node in tree.body
        ):
            offenders.append(f"{path.relative_to(ROOT)}: missing __all__")

    assert not offenders, "\n".join(offenders)


def test_layers_reference_matches_import_linter_contracts() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = {
        contract["name"]: contract
        for contract in pyproject["tool"]["importlinter"]["contracts"]
    }
    rows = _markdown_table_rows_after_heading(
        "docs/architecture/layers.md",
        "## 当前 import-linter 契约",
    )
    documented = {_strip_code(row["契约"]): row for row in rows}

    assert set(documented) == set(contracts)
    for name, contract in contracts.items():
        row = documented[name]
        assert _strip_code(row["type"]) == contract["type"]
        assert _split_doc_list(row["source_modules"]) == contract["source_modules"]
        assert _split_doc_list(row["forbidden_modules"]) == contract["forbidden_modules"]


def test_runtime_flow_documents_tool_boundary_without_misplacing_file_guards() -> None:
    runtime = (ROOT / "docs" / "architecture" / "runtime-flow.md").read_text(
        encoding="utf-8"
    )
    tools_design = (
        ROOT / "docs" / "design" / "tools-and-permissions.md"
    ).read_text(encoding="utf-8")
    dispatcher = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "dispatcher.py"
    ).read_text(encoding="utf-8")
    tool_context = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "context.py"
    ).read_text(encoding="utf-8")
    file_tools = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "builtin" / "files.py"
    ).read_text(encoding="utf-8")
    worktree_mode = (
        ROOT / "src" / "autoverse" / "core" / "loop" / "worktree_mode.py"
    ).read_text(encoding="utf-8")
    pathresolve = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "pathresolve.py"
    ).read_text(encoding="utf-8")

    assert "app 装配的 hook middleware 包住 `ToolDispatcher` 的单调用执行链" in runtime
    assert "读后写、stale-read 和\n相对路径逃逸检查由文件类工具执行" in runtime
    assert "dispatcher 不直接实现 worktree 规则" in runtime
    assert "读后写、路径解析和隔离约束" not in runtime
    assert "`ToolDispatcher` 是统一外层中间件链" in tools_design
    assert "路径解析和隔离映射由 `ToolContext` 提供" in tools_design
    assert "read-before-write 和 stale-read 由文件类工具" in tools_design
    assert "集中处理：\n\n- schema 校验。\n- 能力和权限。\n- 路径解析。" not in tools_design
    assert "was_read_before_write" not in dispatcher
    assert "resolve_path(" not in dispatcher
    assert "resolve_path_in_workspace" in tool_context
    assert "ctx.tracker.was_read_before_write" in file_tools
    assert "app.cwd = worktree_cwd" in worktree_mode
    assert "dispatcher contract" not in pathresolve


def test_entry_docs_list_current_top_level_packages() -> None:
    package_dirs = {
        path.name
        for path in (ROOT / "src" / "autoverse").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    }
    documents = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
    ]

    for path in documents:
        text = path.read_text(encoding="utf-8")
        missing = {name for name in package_dirs if f"{name}/" not in text}
        assert not missing, f"{path.relative_to(ROOT)} missing {sorted(missing)}"


def test_docs_do_not_use_unverified_or_archive_residue_phrases() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for phrase in BANNED_UNVERIFIED_PHRASES:
                if phrase in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {phrase}")
    assert not offenders, "\n".join(offenders)


def test_guides_do_not_copy_generic_prerequisite_or_code_inventories() -> None:
    offenders: list[str] = []
    generic_prerequisites = (
        "已阅读或可回到 [快速开始]",
        "已阅读快速开始",
    )
    banned_headings = {
        "## 相关代码",
        "## 开发入口",
    }

    for path in sorted((ROOT / "docs" / "guides").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(phrase in line for phrase in generic_prerequisites):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
            if line.strip() in banned_headings:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, "\n".join(offenders)


def test_non_doc_files_do_not_reference_legacy_doc_filenames() -> None:
    offenders: list[str] = []
    for path in _iter_current_text_files():
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] == "docs":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in LEGACY_DOC_FILENAMES_RE.finditer(line):
                prefix = line[: match.start()]
                if any(prefix.endswith(current) for current in CURRENT_DOC_PREFIXES):
                    continue
                offenders.append(f"{rel}:{lineno}: {match.group(0)}")
    assert not offenders, "\n".join(offenders)


def test_config_reference_matches_config_models() -> None:
    sections = {
        "### 顶层字段": Config,
        "### `model`": ModelConfig,
        "### `model_tiers`": ModelTiersConfig,
        "### `compaction`": CompactionConfig,
        "### `recovery`": RecoveryConfig,
        "### `memory`": MemoryConfig,
        "### `browser`": BrowserConfig,
        "### `mcp_servers.<name>`": AppMCPServerConfig,
        "### `plugins`": PluginsConfig,
    }

    for heading, model in sections.items():
        rows = _markdown_table_rows_after_heading("docs/reference/config.md", heading)
        documented = {_strip_code(row["字段"]) for row in rows}
        assert documented == set(model.model_fields), heading


def test_config_reference_defaults_match_config_models() -> None:
    sections = {
        "### 顶层字段": Config,
        "### `model`": ModelConfig,
        "### `model_tiers`": ModelTiersConfig,
        "### `compaction`": CompactionConfig,
        "### `recovery`": RecoveryConfig,
        "### `memory`": MemoryConfig,
        "### `browser`": BrowserConfig,
        "### `mcp_servers.<name>`": AppMCPServerConfig,
        "### `plugins`": PluginsConfig,
    }

    for heading, model in sections.items():
        rows = _markdown_table_rows_after_heading("docs/reference/config.md", heading)
        documented = {
            _strip_code(row["字段"]): _parse_doc_default(row["默认值"])
            for row in rows
        }
        actual = {
            field_name: _config_doc_default_value(model, field_name)
            for field_name in model.model_fields
        }

        assert documented == actual, heading


def test_config_docs_do_not_advertise_unowned_frontend_config() -> None:
    config_ref = (ROOT / "docs" / "reference" / "config.md").read_text(encoding="utf-8")
    config_guide = (ROOT / "docs" / "guides" / "configuration.md").read_text(
        encoding="utf-8"
    )

    assert "固定结构内层容器不可就地修改" in config_ref
    assert "frontends | TUI/WebUI 默认行为" not in config_ref
    assert "前端默认行为" not in config_guide
    assert "frontends" not in Config.model_fields


def test_config_reference_documents_loader_layer_order() -> None:
    rows = _markdown_table_rows_after_heading(
        "docs/reference/config.md",
        "## 配置来源和优先级",
    )
    documented_layers = [_strip_code(row["layer"]) for row in rows]
    guide = (ROOT / "docs" / "guides" / "configuration.md").read_text(
        encoding="utf-8"
    )

    assert documented_layers == list(_LAYER_ORDER)
    assert "AUTOVERSE_" in rows[3]["来源"]
    assert "向上找到" in rows[2]["来源"]
    assert "当前加载优先级从低到高" in guide


def test_memory_documentation_matches_current_loader_boundary() -> None:
    cfg = MemoryConfig()
    config_ref = (ROOT / "docs" / "reference" / "config.md").read_text(encoding="utf-8")
    memory_guide = (ROOT / "docs" / "guides" / "project-memory.md").read_text(
        encoding="utf-8"
    )
    builder = (ROOT / "src" / "autoverse" / "app" / "builder.py").read_text(
        encoding="utf-8"
    )

    for filename in cfg.files:
        assert f"`{filename}`" in config_ref
        assert f"`{filename}`" in memory_guide
    assert "当前 `cwd` 下 `memory.files`" in config_ref
    assert "不向上遍历" in config_ref
    assert "不向上遍历父目录" in memory_guide
    assert "scan_max_depth" in memory_guide
    assert "默认 `EngineBuilder` 仍使用 cwd-only full memory 注入" in memory_guide
    assert "retrieval search" in memory_guide
    assert "Fts5RetrievalMemory" not in builder
    assert "should_use_retrieval" not in builder

    rows = _markdown_table_rows_after_heading("docs/reference/config.md", "### `memory`")
    documented = {_strip_code(row["字段"]): row for row in rows}
    assert "默认 builder 仍走 cwd-only full memory 注入" in documented[
        "retrieval_enabled"
    ]["说明"]
    assert "默认 prompt 注入路径尚未消费" in documented["reconcile_on_search"]["说明"]
    assert "强制使用 FTS5 retrieval" not in config_ref


def test_subagent_reference_lists_all_builtin_roles() -> None:
    documented = _markdown_table_values("docs/reference/subagents.md", "角色")
    assert set(AGENT_DEFINITIONS) <= documented


def test_subagent_reference_documents_memory_non_inheritance() -> None:
    text = (ROOT / "docs" / "reference" / "subagents.md").read_text(encoding="utf-8")

    assert "不继承主 agent 的 `PromptAssembler.memory`" in text
    assert "派发 context" in text


def test_subagent_reference_documents_current_drilldown_boundary() -> None:
    text = (ROOT / "docs" / "reference" / "subagents.md").read_text(encoding="utf-8")
    app_subagent = (ROOT / "src" / "autoverse" / "app" / "subagent.py").read_text(
        encoding="utf-8"
    )
    tui_app = (ROOT / "src" / "autoverse" / "frontends" / "repl" / "app.py").read_text(
        encoding="utf-8"
    )
    builtin_cmds = (
        ROOT / "src" / "autoverse" / "frontends" / "repl" / "builtin_cmds.py"
    ).read_text(encoding="utf-8")
    web_server = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "server.py"
    ).read_text(encoding="utf-8")

    assert "UI 深度钻取和长期" not in text
    assert "UI drill-down" in text
    assert "`subagent_child`" in text
    assert "Line/headless" in text
    assert "不提供 in-place `AgentView`" in text
    assert "`/subagents`" in text
    assert "`ctrl+r`" in text
    assert "`GET /api/sessions/{sid}/agent/{job}`" in text
    assert "长期 job 持久化仍不属于默认能力" in text
    assert "SubagentChildPayload" in app_subagent
    assert "action_view_subagents" in tui_app
    assert 'CommandSpec(name="subagents"' in builtin_cmds
    assert '@app.get("/api/sessions/{sid}/agent/{job}")' in web_server


def test_subagent_reference_matches_builtin_role_definitions() -> None:
    rows = _markdown_table_rows_after_heading("docs/reference/subagents.md", "## 角色定义")
    documented = {_strip_code(row["角色"]): row for row in rows}

    assert set(documented) == set(AGENT_DEFINITIONS)
    for name, definition in AGENT_DEFINITIONS.items():
        row = documented[name]
        assert _strip_code(row["能力上限"]) == definition.max_tier.name
        assert row["网络"] == ("是" if definition.can_network else "否")
        assert _strip_code(row["模型 tier"]) == definition.tier.name
        assert row["最大 turn"] == str(definition.max_turns)
        assert row["工具策略"] == _documented_subagent_tools(definition)
        assert row["隔离/审计"] == _documented_subagent_isolation(definition)


def _documented_subagent_tools(definition: object) -> str:
    tools = getattr(definition, "tools")
    if tools is not None:
        return ", ".join(f"`{tool}`" for tool in tools)
    if getattr(definition, "bash_check_mode"):
        return "当前等级下非禁用工具，bash check mode"
    return "当前等级下非禁用工具"


def _documented_subagent_isolation(definition: object) -> str:
    isolation = getattr(definition, "isolation")
    if isolation is not None:
        return "强制 worktree，`git_diff_must_be_empty`，bound process"
    if getattr(definition, "omit_memory"):
        return "省略项目记忆"
    if getattr(definition, "name") == "checkpoint-writer":
        return "系统派发角色"
    return "无"


def test_model_reference_lists_all_registry_models() -> None:
    rows = _markdown_table_rows_after_heading("docs/reference/models.md", "## Registry 模型")
    documented = {_strip_code(row["模型 ID"]): row for row in rows}

    assert set(documented) == set(MODEL_REGISTRY)
    for model_id, spec in MODEL_REGISTRY.items():
        row = documented[model_id]
        assert row["label"] == spec.label
        assert row["context window"] == str(spec.context_window)
        assert row["picker"] == ("yes" if spec.show_in_picker else "no")
        assert row["capabilities"] == _documented_model_capabilities(spec)


def test_model_reference_documents_current_alias_boundary() -> None:
    text = (ROOT / "docs" / "reference" / "models.md").read_text(encoding="utf-8")
    passthrough = _passthrough_spec("unknown/model")

    assert MODEL_ALIASES == {}
    assert resolve_alias("deepseek/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
    assert resolve_alias("unknown/model") == "unknown/model"
    assert passthrough.model_id == "unknown/model"
    assert passthrough.transports[0].endpoint.endswith("/chat/completions")
    assert "当前 registry 使用规范 ID；别名表为空" in text
    assert "未知模型名不会变成别名" in text
    assert "OpenRouter passthrough 规格" in text
    assert "标明状态" not in text


def test_model_reference_capabilities_match_provider_capabilities() -> None:
    rows = _markdown_table_rows_after_heading("docs/reference/models.md", "## 能力位")
    documented = {_strip_code(row["能力"]).replace("-", "_") for row in rows}
    actual = {field.name for field in fields(ProviderCapabilities)}
    reference = (ROOT / "docs" / "reference" / "models.md").read_text(
        encoding="utf-8"
    )
    design = (ROOT / "docs" / "design" / "llm-protocol.md").read_text(
        encoding="utf-8"
    )

    assert documented == actual
    assert "工具 schema 注入当前不是 `ProviderCapabilities` 字段" in reference
    assert "工具 schema 是否出现当前由 ToolRegistry/TurnLoop 决定" in design
    assert "新增 registry 能力位" in design


def test_model_reference_keeps_retry_out_of_transport_boundary() -> None:
    reference = (ROOT / "docs" / "reference" / "models.md").read_text(
        encoding="utf-8"
    )
    transport_base = (
        ROOT / "src" / "autoverse" / "infra" / "llm" / "transport" / "base.py"
    ).read_text(encoding="utf-8")
    fallback = (
        ROOT / "src" / "autoverse" / "infra" / "llm" / "fallback.py"
    ).read_text(encoding="utf-8")

    assert "FallbackPlan" in reference
    assert "不属于 Transport 职责" in reference
    assert "| Transport | HTTP 端点、鉴权、重试" not in reference
    assert "never message\nencoding or retry policy" in transport_base
    assert "RetryStrategy (same-transport exponential backoff)" in fallback


def test_model_reference_matches_current_fallback_registry_boundary() -> None:
    reference = (ROOT / "docs" / "reference" / "models.md").read_text(
        encoding="utf-8"
    )
    design = (ROOT / "docs" / "design" / "llm-protocol.md").read_text(
        encoding="utf-8"
    )

    assert all(len(spec.transports) == 1 for spec in MODEL_REGISTRY.values())
    assert all(spec.fallback_model_id is None for spec in MODEL_REGISTRY.values())
    assert all(
        not spec.capabilities.degradation_prone for spec in MODEL_REGISTRY.values()
    )
    assert "当前 registry 的规范模型均只有一个 transport" in reference
    assert "`fallback_model_id` 都是\n`None`" in reference
    assert "没有声明多 transport 或\n`fallback_model_id`" in design


def _documented_model_capabilities(spec: object) -> str:
    capabilities = getattr(spec, "capabilities")
    names = [
        name
        for name in ("streaming", "thinking", "cache", "vision", "degradation_prone")
        if getattr(capabilities, name)
    ]
    return ", ".join(names) if names else "none"


def test_model_reference_documents_credential_resolution_order(monkeypatch) -> None:
    class _DocSecrets:
        def get(self, key: str) -> str | None:
            return "from_secret" if key == "AUTOVERSE_DOC_SECRET" else None

        def require(self, key: str) -> str:
            value = self.get(key)
            if value is None:
                raise KeyError(key)
            return value

    class _DocSecretsForArk:
        def get(self, key: str) -> str | None:
            return "ark-secret" if key == "ARK_API_KEY" else None

        def require(self, key: str) -> str:
            value = self.get(key)
            if value is None:
                raise KeyError(key)
            return value

    text = (ROOT / "docs" / "reference" / "models.md").read_text(encoding="utf-8")

    assert "`CredentialResolver`" in text
    assert "先读同名环境变量，再读注入的" in text
    assert "两者都没有值时不写对应鉴权 header" in text

    monkeypatch.setenv("AUTOVERSE_DOC_SECRET", "from_env")
    resolver = CredentialResolver(_DocSecrets())
    assert resolver.get("AUTOVERSE_DOC_SECRET") == "from_env"
    monkeypatch.delenv("AUTOVERSE_DOC_SECRET")
    assert resolver.get("AUTOVERSE_DOC_SECRET") == "from_secret"
    assert resolver.get("AUTOVERSE_DOC_MISSING") == ""

    ark = MODEL_REGISTRY["volcengine-plan/glm-5.2"].transports[0]
    headers = ark.auth.apply({}, AuthContext(CredentialResolver(_DocSecretsForArk())))
    assert headers["x-api-key"] == "ark-secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "`x-api-key: <ARK_API_KEY>`" in text
    assert "`anthropic-version: 2023-06-01`" in text


def test_command_reference_matches_builtin_registry() -> None:
    rows = _markdown_table_rows_after_heading("docs/reference/commands.md", "## 内建命令")
    documented = {_strip_code(row["命令"]).lstrip("/"): row for row in rows}
    specs = {spec.name: spec for spec in build_default_registry()._unique_specs()}

    assert set(documented) == set(specs)
    for name, spec in specs.items():
        row = documented[name]
        assert row["分组"] == spec.group
        assert row["摘要"] == spec.summary
        alias_cell = row["快捷键/别名"]
        if spec.keybind is None:
            assert "[" not in alias_cell
        else:
            assert spec.keybind in alias_cell
        for alias in spec.aliases:
            assert f"/{alias}" in alias_cell
        busy_cell = row["busy"]
        if spec.run_while_busy:
            assert busy_cell.startswith("yes")
        else:
            assert busy_cell == ""


def test_command_reference_documents_workflows_picker_boundary() -> None:
    text = (ROOT / "docs" / "reference" / "commands.md").read_text(encoding="utf-8")
    command_specs = {spec.name for spec in build_default_registry()._unique_specs()}

    assert "## Workflows" in text
    assert "`/workflows` 列出 `WorkflowRegistry`" in text
    assert "通过 `engine.run_workflow()`" in text
    assert "不会显示\n成不存在的 `/dream`" in text
    assert {"workflows", "commit", "review", "pr", "init", "coordinate"} <= command_specs
    for name in ("fix", "dream", "distill"):
        assert name not in command_specs


def test_command_reference_matches_template_routing_boundary() -> None:
    reference = (ROOT / "docs" / "reference" / "commands.md").read_text(
        encoding="utf-8"
    )
    guide = (ROOT / "docs" / "guides" / "common-workflows.md").read_text(
        encoding="utf-8"
    )
    app = (ROOT / "src" / "autoverse" / "frontends" / "repl" / "app.py").read_text(
        encoding="utf-8"
    )
    line_repl = (
        ROOT / "src" / "autoverse" / "frontends" / "repl" / "line_repl.py"
    ).read_text(encoding="utf-8")
    web_session = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "session.py"
    ).read_text(encoding="utf-8")

    assert "TUI prompt-template turn 会通过 `switch_model` 临时生效" in reference
    assert "Line REPL/WebUI 当前不应用该字段" in reference
    assert "当前 inline 命令路径不实际路由" in reference
    assert "当前 inline 命令路径不创建隔离子任务" in reference
    assert "Line REPL/WebUI 当前不额外提示" in reference
    assert "agent: reviewer" not in guide
    assert "subtask: true" not in guide
    assert "`agent` / `subtask` frontmatter 会被解析" in guide
    assert "agent/subtask routing isn't honoured for inline commands yet" in app
    assert "result.model" not in line_repl
    assert "result.model" not in web_session


def test_command_reference_matches_at_path_text_injection_boundary() -> None:
    reference = (ROOT / "docs" / "reference" / "commands.md").read_text(
        encoding="utf-8"
    )
    template = (
        ROOT / "src" / "autoverse" / "frontends" / "repl" / "commands_template.py"
    ).read_text(encoding="utf-8")

    assert "作为 `--- @path ---` 文本块追加到同一个 prompt" in reference
    assert "prompt part" not in reference.lower()
    assert 'blocks.append(f"\\n\\n--- @{ref} ---\\n{data}")' in template
    assert "return template + \"\".join(blocks)" in template


def test_command_reference_matches_file_command_discovery_order() -> None:
    rows = _markdown_table_rows_after_heading(
        "docs/reference/commands.md",
        "### 加载路径和优先级",
    )
    documented = [_strip_code(row["目录"]) for row in rows]
    expected = [str(path) for path in _candidate_dirs(Path("<cwd>"), Path("<home>"))]
    reference = (ROOT / "docs" / "reference" / "commands.md").read_text(
        encoding="utf-8"
    )

    assert documented == expected
    assert "不向上查找 git root" in reference
    assert "first-wins" in reference
    assert "静默跳过" in reference


def test_adr_command_examples_use_current_registered_commands() -> None:
    secrets_adr = (
        ROOT / "docs" / "adr" / "0006-secrets-machine-bound-vault.md"
    ).read_text(encoding="utf-8")
    workflow_adr = (
        ROOT / "docs" / "adr" / "0010-workflow-four-dimensions.md"
    ).read_text(encoding="utf-8")
    specs = {spec.name: spec for spec in build_default_registry()._unique_specs()}

    assert "secret" in specs
    assert "secrets" in specs["secret"].aliases
    assert "/secret save <KEY> <VALUE>" in secrets_adr
    assert "/secrets add" not in secrets_adr

    assert {"commit", "coordinate"} <= set(specs)
    assert "`/commit`" in workflow_adr
    assert "`/coordinate`" in workflow_adr
    assert "`/execute`" not in workflow_adr


def test_tool_reference_matches_builtin_registry() -> None:
    from autoverse.core.tools import builtin as _builtin_tools  # noqa: F401
    from autoverse.core.tools.registry import ToolRegistry

    rows = _markdown_table_rows_after_heading(
        "docs/reference/tools.md",
        "## ToolRegistry.build 内置工具",
    )
    documented = {_strip_code(row["工具"]): row for row in rows}
    registry = ToolRegistry.build()

    assert set(documented) == set(registry.names())
    for name in registry.names():
        tool = registry.get(name)
        assert tool is not None
        row = documented[name]
        assert _strip_code(row["capability"]) == CapabilityTier(int(tool.capability)).name
        assert row["network"] == ("yes" if tool.needs_network else "no")


def test_tool_reference_lists_runtime_registered_tools_separately() -> None:
    from autoverse.core.tools import builtin as _builtin_tools  # noqa: F401
    from autoverse.core.tools.registry import ToolRegistry
    from autoverse.infra.tools.agent_tool import AgentTool
    from autoverse.infra.tools.job_admin import JobGetTool, JobListTool, JobStopTool
    from autoverse.infra.tools.job_output import JobOutputTool
    from autoverse.infra.tools.vision_tool import VisionTool

    text = (ROOT / "docs" / "reference" / "tools.md").read_text(encoding="utf-8")
    builtin_names = set(ToolRegistry.build().names())
    runtime_names = {
        AgentTool.name,
        JobOutputTool.name,
        JobListTool.name,
        JobGetTool.name,
        JobStopTool.name,
        VisionTool.name,
    }

    assert runtime_names.isdisjoint(builtin_names)
    assert "生产 `EngineBuilder`" in text
    for name in runtime_names:
        assert f"`{name}`" in text


def test_tool_reference_matches_dispatcher_and_file_tool_responsibilities() -> None:
    text = (ROOT / "docs" / "reference" / "tools.md").read_text(encoding="utf-8")
    dispatcher = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "dispatcher.py"
    ).read_text(encoding="utf-8")
    file_tools = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "builtin" / "files.py"
    ).read_text(encoding="utf-8")

    assert "ToolStartPayload" in dispatcher
    assert "ToolEndPayload" in dispatcher
    assert "ErrorPayload" not in dispatcher
    assert "resolve_path" in file_tools
    assert "ctx.tracker.was_read_before_write" in file_tools
    assert "发出 TOOL_START / TOOL_END 事件" in text
    assert "路径边界和 read-before-write / stale-read 约束由文件类工具" in text
    assert "发出 TOOL_END / ERROR" not in text
    assert "检查 plan mode、权限、能力和路径边界" not in text


def test_common_workflows_defers_tool_capability_inventory_to_reference() -> None:
    guide = (ROOT / "docs" / "guides" / "common-workflows.md").read_text(
        encoding="utf-8"
    )
    reference = (ROOT / "docs" / "reference" / "tools.md").read_text(
        encoding="utf-8"
    )

    for tier in CapabilityTier:
        assert f"`{tier.name}`" in reference
        assert f"| `{tier.name}` |" not in guide
    for config_value in ("read_only", "controlled_write", "arbitrary_write", "process"):
        assert f"| `{config_value}` |" not in guide
    assert "../reference/tools.md" in guide


def test_tool_reference_plan_mode_browser_block_list_matches_dispatcher() -> None:
    from autoverse.core.tools.dispatcher import _PLAN_UNSAFE_BROWSER_TOOLS

    text = (ROOT / "docs" / "reference" / "tools.md").read_text(encoding="utf-8")
    match = re.search(r"plan 模式会拦截 ([^\n]+)。", text)
    assert match is not None
    documented = set(re.findall(r"`(browser_[^`]+)`", match.group(1)))

    assert documented == set(_PLAN_UNSAFE_BROWSER_TOOLS)


def test_browser_tool_reference_matches_builtin_registry_subset() -> None:
    from autoverse.core.tools import builtin as _builtin_tools  # noqa: F401
    from autoverse.core.tools.registry import ToolRegistry

    rows = _markdown_table_rows_after_heading(
        "docs/reference/tools.md",
        "## 浏览器工具",
    )
    documented = {_strip_code(row["工具"]) for row in rows}
    registry = ToolRegistry.build()
    actual = {name for name in registry.names() if name.startswith("browser_")}

    assert documented == actual


def test_browser_tool_reference_documents_actual_schema_fields() -> None:
    from autoverse.core.tools import builtin as _builtin_tools  # noqa: F401
    from autoverse.core.tools.registry import ToolRegistry

    rows = _markdown_table_rows_after_heading(
        "docs/reference/tools.md",
        "## 浏览器工具",
    )
    documented = {_strip_code(row["工具"]): row for row in rows}
    registry = ToolRegistry.build()

    for name, row in documented.items():
        tool = registry.get(name)
        assert tool is not None
        schema = tool.to_api_schema()["input_schema"]  # type: ignore[index]
        properties = set(dict(schema.get("properties", {})))
        captures = set(re.findall(r"`([^`]+)`", row["关键参数"]))
        documented_fields = captures & properties
        assert documented_fields == properties, name


def test_browser_evaluate_contract_is_documented() -> None:
    from autoverse.core.tools import builtin as _builtin_tools  # noqa: F401
    from autoverse.core.tools.registry import ToolRegistry

    tool = ToolRegistry.build().get("browser_evaluate")
    assert tool is not None
    schema = tool.to_api_schema()["input_schema"]  # type: ignore[index]
    description = schema["properties"]["script"]["description"]
    tools_reference = (ROOT / "docs/reference/tools.md").read_text(encoding="utf-8")
    web_reference = (ROOT / "docs/reference/webui-api.md").read_text(
        encoding="utf-8"
    )
    events_reference = (ROOT / "docs/reference/events-and-storage.md").read_text(
        encoding="utf-8"
    )
    apple_source = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )
    apple_readme = (ROOT / "apple/README.md").read_text(encoding="utf-8")

    for token in ("return await", "JSON-serializable", "Promise"):
        assert token in description
    for token in ("unsupported_result", "browser_timeout", "严格 JSON"):
        assert token in tools_reference
    for token in ("unsupported_result", "30 秒", "JSON 合法性检查"):
        assert token in web_reference
    for token in ("unsupported_result", "不会在 trace 中退化为 `unknown`"):
        assert token in events_reference
    for token in (
        "evaluationTimeoutMilliseconds: Double = 30_000",
        "javaScriptResultTypeIsUnsupported",
        'kind: "unsupported_result"',
    ):
        assert token in apple_source
    for token in ("return await", "unsupported_result", "browser_timeout"):
        assert token in apple_readme


def test_webui_api_reference_matches_fastapi_routes() -> None:
    server = (ROOT / "src" / "autoverse" / "frontends" / "web" / "server.py").read_text(
        encoding="utf-8"
    )
    actual = {
        (method.upper(), path)
        for method, path in re.findall(
            r"@app\.(get|post|delete|put|websocket)\(\"([^\"]+)\"", server
        )
    }
    rows = _markdown_table_rows_with_columns("docs/reference/webui-api.md", {"方法", "路径"})
    documented = {
        (_strip_code(row["方法"]), _strip_code(row["路径"]).split("?", 1)[0])
        for row in rows
    }

    assert actual <= documented
    assert documented <= actual


def test_webui_api_reference_is_single_truth_source() -> None:
    reference = (ROOT / "docs" / "reference" / "webui-api.md").read_text(
        encoding="utf-8"
    )
    impact_map = DOC_IMPACT_PATH.read_text(encoding="utf-8")

    assert not (ROOT / "docs" / "guides" / "webui-api.md").exists()
    assert "## 快速调试流程" in reference
    assert "curl -N http://127.0.0.1:8170/api/sessions/<sid>/stream" in reference
    assert "docs/reference/webui-api.md" in impact_map
    assert "docs/guides/webui-api.md" not in impact_map


def test_web_extra_includes_native_browser_websocket_transport() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"uvicorn[standard]>=0.27"' in pyproject


def test_native_browser_handshake_contract_is_documented() -> None:
    broker = (ROOT / "src/autoverse/frontends/web/browser_host.py").read_text(
        encoding="utf-8"
    )
    apple = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )
    reference = (ROOT / "docs/reference/webui-api.md").read_text(encoding="utf-8")

    for token in ("protocol_version = 1", '"type": "host_replaced"'):
        assert token in broker
    for token in ('"protocol": 1', '== "host_replaced"'):
        assert token in apple
    for token in ('"protocol":1', "`host_replaced`", "5 秒"):
        assert token in reference


def test_native_browser_safari_compatible_identity_is_documented() -> None:
    apple = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "apple/README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/guides/using-webui.md").read_text(encoding="utf-8")

    for token in (
        "configuration.applicationNameForUserAgent",
        'return "Version/\\(safari) Safari/\\(webKit) AutoVerse/\\(app)"',
    ):
        assert token in apple
    assert "version-matched Safari product tokens" in readme
    assert "不伪装为 Chrome" in guide


def test_native_browser_workspace_layout_contract_is_documented() -> None:
    content = (ROOT / "apple/Sources/Shared/ContentView.swift").read_text(
        encoding="utf-8"
    )
    webview = (ROOT / "apple/Sources/Shared/WebView.swift").read_text(
        encoding="utf-8"
    )
    app_js = (ROOT / "src/autoverse/frontends/web/static/app.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "src/autoverse/frontends/web/static/index.html").read_text(
        encoding="utf-8"
    )
    guide = (ROOT / "docs/guides/using-webui.md").read_text(encoding="utf-8")

    assert "model.nativeChrome.rightWidth" in content
    assert "BrowserWorkspaceLayout.defaultBrowserWidth" in content
    assert "BrowserWorkspaceLayout.maximumBrowserWidth" in content
    assert "browserWorkspaceRestoreWidth" in content
    assert ".offset(" in content
    assert "browserWorkspaceVisible: model.browserHost.isVisible" in content
    assert "autoverseSetNativeBrowserWorkspace" in webview
    assert "comfortableConversationWidth: CGFloat = 520" in content
    assert "minimumConversationWidth: CGFloat = 520" in content
    assert "minimumBrowserWidth: CGFloat = 320" in content
    assert "const RAIL_COMFORTABLE_CENTER_WIDTH = 520;" in app_js
    assert "const RAIL_MINIMUM_CENTER_WIDTH = 520;" in app_js
    assert "native-macos.native-browser-workspace .mobile-bar" not in css
    assert ".native-macos.native-browser-workspace .conversation-overlay" not in css
    assert "autoverseConversationOverlay" in webview
    assert "native-conversation-overlay-active" in webview
    assert "underPageBackgroundColor = .clear" in webview
    assert 'setValue(false, forKey: "drawsBackground")' in webview
    assert ".zIndex(conversationOverlayVisible ? 2 : 0)" in content
    assert "原位占用 WebUI 右侧栏位置" in guide
    assert "设置、新建会话、图片预览、退出 worktree" in guide
    assert "覆盖 Browser" in guide
    assert "浏览器画面仍在半透明遮罩下可见" in guide
    assert "520px" in guide
    assert "600px" in guide
    assert "900px" in guide


def test_native_browser_address_field_proportions_are_documented() -> None:
    browser_host = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "apple/README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/guides/using-webui.md").read_text(encoding="utf-8")

    for token in (
        "toolbarHeight: CGFloat = 46",
        "addressFieldHeight: CGFloat = 34",
        "addressFieldMinimumWidth: CGFloat = 96",
        ".layoutPriority(1)",
    ):
        assert token in browser_host
    assert "desktop-browser proportions" in readme
    assert "接近桌面 Chrome 的较高圆角比例" in guide


def test_native_browser_gapless_tab_strip_is_documented() -> None:
    browser_host = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "apple/README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/guides/using-webui.md").read_text(encoding="utf-8")
    design = (
        ROOT / "docs/design/frontend-interaction-responsiveness.md"
    ).read_text(encoding="utf-8")

    for token in (
        "tabStripHeight: CGFloat = 40",
        "tabHeight: CGFloat = 40",
        "tabSpacing: CGFloat = 0",
        "tabSeparatorWidth: CGFloat = 1",
        "tabSelectionIndicatorHeight: CGFloat = 2",
        "newPageButtonWidth: CGFloat = 40",
    ):
        assert token in browser_host
    assert "gapless segments" in readme
    assert "彼此零间距的连续分段样式" in guide
    assert "满高、零间距的连续分段" in design


def test_native_browser_agent_activity_is_bounded_and_documented() -> None:
    browser_host = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )
    guide = (ROOT / "docs/guides/using-webui.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/architecture/frontend-adapters.md").read_text(
        encoding="utf-8"
    )
    design = (
        ROOT / "docs/design/frontend-interaction-responsiveness.md"
    ).read_text(encoding="utf-8")

    for token in (
        "@Published private(set) var agentActivity",
        'action != "list_pages"',
        "defer { endAgentActivity(activityToken) }",
        "Text(controlStatusLabel)",
        "InteractionMotion.activity",
        "guard agentIsActing, !reduceMotion else { return }",
        ".contentTransition(.opacity)",
        ".scaleEffect(0.94 + 0.06 * agentPulseStrength)",
        "agentActivityMinimumDisplayMilliseconds: Double = 800",
        "BrowserChromeMetrics.activityButtonWidth",
        "agentActivityPulse = agentIsActing",
        '.accessibilityValue(controller.agentActivity?.label ?? "Agent control")',
    ):
        assert token in browser_host
    for token in ("琥珀色细描边", "绿色", "至少保留 800ms", "固定宽度", "减少动态效果"):
        assert token in guide
    for token in ("不持久化", "至少 800ms", "用户接管或断线则立即清除", "不新增 WebSocket 字段"):
        assert token in architecture
    for token in ("activity", "1100 ms", "低幅呼吸反馈", "交叉淡入"):
        assert token in design


def test_webui_api_reference_documents_file_preview_metadata(tmp_path: Path) -> None:
    from autoverse.frontends.web.files import read_file_preview_payload

    rows = _markdown_table_rows_after_heading(
        "docs/reference/webui-api.md",
        "### `/file` 预览元数据",
    )
    documented_fields = {_strip_code(row["字段"]) for row in rows}
    sample = tmp_path / "sample.txt"
    sample.write_text("hello\n", encoding="utf-8")
    payload = read_file_preview_payload(tmp_path, "sample.txt")

    assert {"kind", "path", "root", "name", "size", "mime"} <= documented_fields
    assert payload["root"] == str(tmp_path)
    reference = (ROOT / "docs" / "reference" / "webui-api.md").read_text(
        encoding="utf-8"
    )
    assert "`root` | session cwd 的绝对路径，用于前端复制绝对路径" in reference
    assert "逐项匹配" in reference
    assert "任意猜测的 cwd 外路径仍返回" in reference


def test_webui_guide_matches_file_preview_actions() -> None:
    guide = (ROOT / "docs" / "guides" / "using-webui.md").read_text(
        encoding="utf-8"
    )
    app_js = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "预览面板可复制绝对路径" in guide
    assert "图片可全屏查看" in guide
    assert "`↑` / `↓` 在所有可见文件与文件夹间移动" in guide
    assert "`→` 展开文件夹或进入首个子节点" in guide
    assert "选中文件夹保留原预览" in guide
    assert "Changed/Memory 条目使用相同入口" in guide
    assert "只开放当前会话明确展示的那个文件" in guide
    assert "点击文件会收起 Browser 并保留其标签状态" in guide
    assert "raw/download" not in guide
    assert "document.addEventListener" in app_js
    assert "autoverseFilePreview" in app_js
    assert "data-fullscreen-file" in app_js
    assert "openImageLightbox" in app_js
    assert "data-download-file" not in app_js


def test_chat_web_link_bridge_is_documented() -> None:
    guide = (ROOT / "docs/guides/using-webui.md").read_text(encoding="utf-8")
    reference = (ROOT / "docs/reference/webui-api.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/architecture/frontend-adapters.md").read_text(
        encoding="utf-8"
    )
    apple = (ROOT / "apple/README.md").read_text(encoding="utf-8")

    assert "显式 Markdown" in guide
    assert "裸网址" in guide
    assert "代码样式中的完整网址" in guide
    assert "续行保持正文的悬挂缩进" in guide
    assert "不会覆盖当前标签" in guide
    assert "`autoverseBrowserLink` handler" in reference
    assert "不新增 REST/SSE/WebSocket 字段" in reference
    assert "复用\n`BrowserURLPolicy`" in architecture
    assert "creates a\nnew active Browser tab" in apple


def test_webui_api_reference_documents_secret_request_shapes_once() -> None:
    reference = (ROOT / "docs" / "reference" / "webui-api.md").read_text(
        encoding="utf-8"
    )
    rows = _markdown_table_rows_after_heading(
        "docs/reference/webui-api.md",
        "## 全局查询",
    )
    documented = {
        f"{_strip_code(row['方法'])} {_strip_code(row['路径'])}": row[
            "Body / Query"
        ].strip()
        for row in rows
    }

    assert "Secrets 请求形状：" not in reference
    assert documented == {
        "GET /api/commands": "-",
        "GET /api/models": "-",
        "GET /api/workspace": "-",
        "GET /api/secrets": "query `session_id?`",
        "POST /api/secrets/session": "body `{session_id, key, value}`",
        "DELETE /api/secrets/session/{key}": "query `session_id`",
        "POST /api/secrets/vault": "body `{key, value, session_id?}`",
        "DELETE /api/secrets/vault/{key}": "query `session_id?`",
        "GET /api/providers/account": "query `key`, `session_id?`",
        "POST /api/restart": "-",
    }


def test_secret_docs_match_runtime_priority_layers() -> None:
    config_ref = (ROOT / "docs" / "reference" / "config.md").read_text(
        encoding="utf-8"
    )
    config_guide = (ROOT / "docs" / "guides" / "configuration.md").read_text(
        encoding="utf-8"
    )
    webui_api = (ROOT / "docs" / "reference" / "webui-api.md").read_text(
        encoding="utf-8"
    )
    web_session = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "session.py"
    ).read_text(encoding="utf-8")
    vault = (ROOT / "src" / "autoverse" / "infra" / "secrets" / "vault.py").read_text(
        encoding="utf-8"
    )

    assert '"priority": ["env", "session", "file"]' in web_session
    assert "effective priority is env →\n    session → vault" in vault
    assert "`env -> session -> file(vault)`" in config_ref
    assert "WebUI session secret 作为临时会话层" in config_guide
    assert '`priority` 给出，目前为 `["env", "session", "file"]`' in webui_api
    assert "`effective_source`" in webui_api


def test_cli_reference_matches_argument_parsers() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    entry_rows = _markdown_table_rows_after_heading("docs/reference/cli.md", "## 入口点")
    documented_entries = {_strip_code(row["命令"]): row for row in entry_rows}

    assert set(documented_entries) == set(scripts)
    for name, target in scripts.items():
        assert _strip_code(documented_entries[name]["pyproject target"]) == target

    cli_rows = _markdown_table_rows_after_heading(
        "docs/reference/cli.md",
        "### 参数和 flags",
    )
    cli_documented = {_strip_code(row["参数"]).split()[0] for row in cli_rows}

    cli_parser = build_parser()
    cli_actual = {"prompt"}
    ui_choices: tuple[str, ...] = ()
    for action in cli_parser._actions:
        cli_actual.update(opt for opt in action.option_strings if opt.startswith("--"))
        if "--ui" in action.option_strings:
            ui_choices = tuple(str(choice) for choice in (action.choices or ()))
    cli_actual.discard("--help")
    assert cli_documented == cli_actual

    ui_rows = _markdown_table_rows_after_heading("docs/reference/cli.md", "### UI 模式")
    documented_ui_modes = {_strip_code(row["模式"]) for row in ui_rows}
    assert documented_ui_modes == set(ui_choices)

    web_rows = _markdown_table_rows_after_heading("docs/reference/cli.md", "### flags")
    web_documented = {_strip_code(row["参数"]).split()[0] for row in web_rows}
    server = (ROOT / "src" / "autoverse" / "frontends" / "web" / "server.py").read_text(
        encoding="utf-8"
    )
    web_actual = set(re.findall(r"parser\.add_argument\(\s*\"(--[^\"]+)\"", server))
    assert web_documented == web_actual


def test_cli_reference_documents_one_shot_and_no_tui_semantics() -> None:
    from autoverse.frontends.cli.main import _resolve_ui_mode

    text = (ROOT / "docs" / "reference" / "cli.md").read_text(encoding="utf-8")
    parser = build_parser()

    assert _resolve_ui_mode(parser.parse_args(["--no-tui"])) == "line"
    assert _resolve_ui_mode(parser.parse_args(["--ui", "tui", "--no-tui"])) == "tui"
    assert "交互模式等价于 `--ui line`" in text
    assert "一次性执行时禁用 ANSI 彩色输出" in text
    assert "传入 `prompt` 就会跑一轮后退出" in text
    assert "从推荐档开始" in text
    assert "one-shot only" in text
    assert "交互模式下会提示需要提供 prompt 或使用 `--print`" in text


def test_cli_guide_uses_real_ui_mode_tokens() -> None:
    guide = (ROOT / "docs" / "guides" / "using-cli-and-tui.md").read_text(
        encoding="utf-8"
    )
    quickstart = (ROOT / "docs" / "guides" / "getting-started.md").read_text(
        encoding="utf-8"
    )
    parser = build_parser()
    ui_choices: tuple[str, ...] = ()
    for action in parser._actions:
        if "--ui" in action.option_strings:
            ui_choices = tuple(str(choice) for choice in (action.choices or ()))
            break

    documented_modes = set(
        re.findall(r"^- `([^`]+)`：", guide, flags=re.MULTILINE)
    )
    assert documented_modes == set(ui_choices) - {"auto"}
    assert "`fullscreen`：" not in guide
    assert "`plain`：one-shot only" in guide
    assert "交互模式优先切到\n`line`" in guide
    assert "或 `plain`" not in guide
    assert "弱终端会降级到\nline REPL" in quickstart
    assert "`plain` 是脚本友好的 one-shot 输出档" in quickstart
    assert "line/plain" not in quickstart


def test_troubleshooting_guide_matches_current_runtime_boundaries() -> None:
    text = (ROOT / "docs" / "guides" / "troubleshooting.md").read_text(
        encoding="utf-8"
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    web_deps = pyproject["project"]["optional-dependencies"]["web"]
    for dep, label in (
        ("fastapi", "FastAPI"),
        ("uvicorn", "Uvicorn"),
        ("cryptography", "`cryptography`"),
    ):
        assert any(
            item.partition(">=")[0].partition("[")[0] == dep for item in web_deps
        )
        assert label in text
    assert 'pip install -e ".[web]"' in text
    assert 'pip install -e ".[dev,web]"' in text

    parser = build_parser()
    ui_choices: tuple[str, ...] = ()
    for action in parser._actions:
        if "--ui" in action.option_strings:
            ui_choices = tuple(str(choice) for choice in (action.choices or ()))
            break
    assert {"tui", "inline", "line", "plain"} <= set(ui_choices)
    assert "--ui line" in text
    assert "--ui plain" in text
    assert "one-shot only" in text
    assert "line/plain" not in text
    assert "`plain`/`--print` one-shot" in text

    server = (ROOT / "src" / "autoverse" / "frontends" / "web" / "server.py").read_text(
        encoding="utf-8"
    )
    assert '@app.get("/api/sessions/{sid}/stream")' in server
    assert "/api/sessions/{sid}/stream" in text

    from autoverse.infra.llm.codec.openai import OpenAIWireCodec
    from autoverse.infra.llm.taxonomy import ErrorKind, LLMError
    from autoverse.infra.tools.vision_tool import VisionTool
    from autoverse.types.messages import ImageBlock

    try:
        OpenAIWireCodec().supports(
            [ImageBlock(media_type="image/png", data="QQ==")],
            ProviderCapabilities(vision=False),
        )
    except LLMError as exc:
        assert exc.kind is ErrorKind.UNSUPPORTED
    else:  # pragma: no cover - defensive contract assertion
        raise AssertionError("non-vision OpenAI codec must reject ImageBlock")
    assert VisionTool.name == "read_image"
    assert "ImageBlock" in text
    assert "`read_image`" in text
    assert "OPENROUTER_API_KEY" in text

    file_tools = (
        ROOT / "src" / "autoverse" / "core" / "tools" / "builtin" / "files.py"
    ).read_text(encoding="utf-8")
    tracker = (ROOT / "src" / "autoverse" / "core" / "tools" / "tracker.py").read_text(
        encoding="utf-8"
    )
    assert "workspace_escape" in file_tools
    assert "stale_or_unread" in file_tools
    assert "Read it before writing." in tracker
    assert "workspace_escape" in text
    assert "read-before-write / stale-read" in text


def test_docs_do_not_advertise_internal_fullscreen_ui_token() -> None:
    offenders: list[str] = []
    for path in _iter_documentation_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "fullscreen" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_frontend_adapter_reference_lists_existing_frontends() -> None:
    rows = _markdown_table_rows_after_heading(
        "docs/architecture/frontend-adapters.md",
        "## 前端类型",
    )
    documented = {_strip_code(row["前端"]): _strip_code(row["主要代码"]) for row in rows}

    assert documented == {
        "CLI": "frontends/cli/",
        "TUI": "frontends/repl/app.py",
        "Line REPL": "frontends/repl/line_repl.py",
        "WebUI": "frontends/web/",
        "Gateway": "frontends/gateway/",
    }
    for path in documented.values():
        assert (ROOT / "src" / "autoverse" / path).exists()


def test_entry_docs_mention_gateway_frontend_when_present() -> None:
    assert (ROOT / "src" / "autoverse" / "frontends" / "gateway").is_dir()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    frontend_adapters = (
        ROOT / "docs" / "architecture" / "frontend-adapters.md"
    ).read_text(encoding="utf-8")

    for text in (readme, docs_index, frontend_adapters):
        assert "Gateway" in text


def test_hooks_reference_matches_engine_contract() -> None:
    from autoverse.infra.hooks.shell import parse_hooks_config

    event_rows = _markdown_table_rows_after_heading("docs/reference/hooks.md", "## 事件")
    documented_events = {_strip_code(row["事件"]): row for row in event_rows}

    assert set(documented_events) == {event.value for event in HookEvent}
    for event in HookEvent:
        row = documented_events[event.value]
        assert row["阻塞默认"] == (
            "是" if event in {HookEvent.PRE_TOOL_USE, HookEvent.STOP} else "否"
        )
        assert row["工具上下文"] == (
            "是"
            if event in {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE}
            else "否"
        )

    async def _noop_hook(_hook_input: HookInput) -> HookOutcome:
        return HookOutcome()

    default_hook = HookDef(event=HookEvent.PRE_TOOL_USE, handler=_noop_hook)
    field_rows = _markdown_table_rows_after_heading(
        "docs/reference/hooks.md",
        "## 配置形状",
    )
    documented_fields = {_strip_code(row["字段"]): row for row in field_rows}

    assert set(documented_fields) == {
        "command",
        "matcher",
        "timeout",
        "blocking",
        "description",
    }
    assert _strip_code(documented_fields["command"]["默认值"]) == "必填"
    assert _strip_code(documented_fields["matcher"]["默认值"]) == f'"{default_hook.matcher}"'
    assert float(_strip_code(documented_fields["timeout"]["默认值"])) == default_hook.timeout
    assert (
        _strip_code(documented_fields["blocking"]["默认值"])
        == str(default_hook.blocking).lower()
    )
    assert _strip_code(documented_fields["description"]["默认值"]) == '""'
    text = (ROOT / "docs" / "reference" / "hooks.md").read_text(encoding="utf-8")
    assert "shell hook 非零退出" in text
    assert "stderr 只用于人工诊断" in text
    assert "非法 timeout" in text
    assert len(
        parse_hooks_config(
            {
                "PreToolUse": [
                    {"command": "echo bad", "timeout": "bad"},
                    {"command": "echo bad", "blocking": "false"},
                    {"command": "echo ok"},
                ]
            }
        )[HookEvent.PRE_TOOL_USE]
    ) == 1


def test_hooks_guide_matches_default_runtime_boundary() -> None:
    text = (ROOT / "docs" / "guides" / "hooks.md").read_text(encoding="utf-8")

    assert "`PreToolUse` 和 `PostToolUse`" in text
    assert "`SessionStart` 和 `Stop`" in text
    assert "默认 builder 尚未调用" in text
    assert "../reference/hooks.md" in text
    assert "脚本非零退出会被当成 hook failure" in text


def test_hook_adr_does_not_claim_current_tool_input_rewrite_support() -> None:
    adr = (ROOT / "docs" / "adr" / "0007-hook-no-silent-rewrite.md").read_text(
        encoding="utf-8"
    )

    assert {field.name for field in fields(HookOutcome)} == {
        "additional_context",
        "decision",
        "reason",
    }
    assert "当前实现只支持放行、`deny`、`reason` 和 `additionalContext`" in adr
    assert "若未来提供改写能力" in adr
    assert "改写作为**显式 opt-in 能力**提供" not in adr


def test_extensions_reference_documents_gateway_runtime_components() -> None:
    text = (ROOT / "docs" / "reference" / "extensions.md").read_text(encoding="utf-8")

    assert "Gateway 是 frontend adapter" in text
    assert "审批和重试" in text
    for symbol in ("PlatformAdapter", "GatewayRunner"):
        assert symbol in text
    assert "src/autoverse/frontends/gateway/" in text
    assert (ROOT / "src" / "autoverse" / "frontends" / "gateway").is_dir()


def test_extensions_reference_does_not_duplicate_command_subcommand_lists() -> None:
    extensions = (ROOT / "docs" / "reference" / "extensions.md").read_text(
        encoding="utf-8"
    )
    commands = (ROOT / "docs" / "reference" / "commands.md").read_text(
        encoding="utf-8"
    )

    assert "commands.md#skills-和-plugins" in extensions
    for command in (
        "/skills show <name>",
        "/skills doctor <name> [prompt]",
        "/plugin show <id>",
        "/plugin doctor <id>",
    ):
        assert command in commands
        assert command not in extensions


def test_extensions_reference_matches_current_plugin_runtime_capabilities() -> None:
    extensions = (ROOT / "docs" / "reference" / "extensions.md").read_text(
        encoding="utf-8"
    )
    manager = (ROOT / "src" / "autoverse" / "infra" / "plugins" / "manager.py").read_text(
        encoding="utf-8"
    )
    loader = (ROOT / "src" / "autoverse" / "infra" / "plugins" / "loader.py").read_text(
        encoding="utf-8"
    )

    activate_body = manager.split("def _activate", 1)[1].split(
        "    def contributed_tools", 1
    )[0]
    for capability in ('"tools"', '"commands"', '"hooks"', '"skills"'):
        assert capability in activate_body
    assert '"mcp"' not in activate_body
    assert '"assets"' not in activate_body

    assert 'mcp=bool(value.get("mcpServers", False))' in loader
    assert 'assets=bool(value.get("assets", False) or value.get("interface", False))' in loader
    assert "当前 runtime manager 消费 tools、commands、hooks 和 skills" in extensions
    assert "manifest parser 能识别 MCP servers/assets 声明" in extensions
    assert "默认 manager 还不会把它们装配成运行时能力" in extensions


def test_mcp_reference_matches_config_and_manager_contract() -> None:
    from autoverse.app.extensions import _translate_mcp
    from autoverse.infra.mcp.transport import default_client_factory

    text = (ROOT / "docs" / "reference" / "mcp.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "guides" / "mcp.md").read_text(encoding="utf-8")
    manager = (ROOT / "src" / "autoverse" / "infra" / "mcp" / "manager.py").read_text(
        encoding="utf-8"
    )
    runtime_fields = {field.name for field in fields(RuntimeMCPServerConfig)}
    assert runtime_fields == {
        "name",
        "command",
        "url",
        "capability",
        "needs_network",
        "rpc_timeout",
    }
    assert "config.md#mcpserversname" in text
    assert "| 字段 | 类型 | 默认值 | 说明 |" not in text
    assert "stdio client" in text
    assert "stdio client" in guide
    assert "Production currently uses the stdio JSON-RPC client" in manager
    assert "Concrete stdio / streamable-http clients implement this in later work" not in manager

    config_rows = _markdown_table_rows_after_heading(
        "docs/reference/config.md", "### `mcp_servers.<name>`"
    )
    config_documented = {_strip_code(row["字段"]): row for row in config_rows}
    assert set(config_documented) == set(AppMCPServerConfig.model_fields)
    for field_name in ("env", "cwd", "url", "headers"):
        assert "保留" in config_documented[field_name]["说明"]
    assert "stdio" in config_documented["args"]["说明"]
    assert "当前保留" in config_documented["url"]["说明"]

    translated = _translate_mcp({"remote": AppMCPServerConfig(url="https://mcp.example")})
    assert translated["remote"].url == "https://mcp.example"
    assert translated["remote"].command == ""
    try:
        default_client_factory(translated["remote"])
    except ValueError as exc:
        assert "no command" in str(exc)
    else:  # pragma: no cover - defensive contract assertion
        raise AssertionError("default MCP client factory must require command")

    local = _translate_mcp(
        {
            "local": AppMCPServerConfig(
                command="python -m docs_mcp",
                args=["--root", "/tmp/project with spaces"],
            )
        }
    )["local"]
    assert shlex.split(local.command) == [
        "python",
        "-m",
        "docs_mcp",
        "--root",
        "/tmp/project with spaces",
    ]

    assert MCP_DEFAULT_TIER is CapabilityTier.PROCESS
    assert "CapabilityTier.PROCESS" in text

    status_values = _markdown_table_values("docs/reference/mcp.md", "状态")
    assert status_values == set(get_args(ServerStatus))

    resource_rows = _markdown_table_rows_after_heading("docs/reference/mcp.md", "## 资源工具")
    documented_tools = {_strip_code(row["工具"]): row for row in resource_rows}
    assert set(documented_tools) == {
        ListMcpResourcesTool.name,
        ReadMcpResourceTool.name,
    }
    assert {
        _strip_code(row["能力"]) for row in documented_tools.values()
    } == {"read_only"}


def test_skills_reference_matches_spec_and_tool_contract() -> None:
    rows = _markdown_table_rows_after_heading(
        "docs/reference/skills.md",
        "## Frontmatter 字段",
    )
    documented_frontmatter = {_strip_code(row["字段"]): row for row in rows}
    assert set(documented_frontmatter) == {"name", "description"}

    text = (ROOT / "docs" / "reference" / "skills.md").read_text(encoding="utf-8")
    assert SKILL_NAME_RE.pattern in text

    diag_rows = _markdown_table_rows_after_heading("docs/reference/skills.md", "## Diagnostics")
    documented_diag_fields = {_strip_code(row["字段"]) for row in diag_rows}
    assert documented_diag_fields == {field.name for field in fields(SkillDiagnostic)}

    assert set(SkillArgs.model_fields) == {"skill", "args"}
    assert "`skill` 名称" in text
    assert "`args` 显式调用参数" in text


def test_skill_design_progressive_disclosure_matches_runtime_boundary() -> None:
    design = (ROOT / "docs" / "design" / "skills.md").read_text(encoding="utf-8")
    registry = (ROOT / "src" / "autoverse" / "infra" / "skills" / "registry.py").read_text(
        encoding="utf-8"
    )
    skill_tool = (ROOT / "src" / "autoverse" / "infra" / "skills" / "tool.py").read_text(
        encoding="utf-8"
    )

    assert "render_available_skills" in registry
    assert "SkillTool" in registry
    assert "skill.render(args.args)" in skill_tool
    assert "prompt assembler 投影轻量 skill index" in design
    assert "轻量 index + `Skill` tool 读取正文" in design
    assert "可用 skill body”收敛为 budgeted index" not in design
    assert "budgeted skill index" not in design


def test_skills_docs_match_discovery_and_install_boundaries(tmp_path: Path) -> None:
    from autoverse.infra.skills.discovery import default_skill_roots

    guide = (ROOT / "docs" / "guides" / "skills.md").read_text(encoding="utf-8")
    reference = (ROOT / "docs" / "reference" / "skills.md").read_text(encoding="utf-8")
    extensions = (ROOT / "docs" / "reference" / "extensions.md").read_text(
        encoding="utf-8"
    )
    commands = (
        ROOT / "src" / "autoverse" / "frontends" / "repl" / "builtin_cmds.py"
    ).read_text(encoding="utf-8")
    repl_app = (ROOT / "src" / "autoverse" / "frontends" / "repl" / "app.py").read_text(
        encoding="utf-8"
    )
    line_repl = (
        ROOT / "src" / "autoverse" / "frontends" / "repl" / "line_repl.py"
    ).read_text(encoding="utf-8")
    web_server = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "server.py"
    ).read_text(encoding="utf-8")
    web_session = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "session.py"
    ).read_text(encoding="utf-8")

    home = tmp_path / "home"
    roots = default_skill_roots(tmp_path / "work" / "child", home=home)

    assert roots[0].path.name == "skills"
    assert roots[-1].path == home / ".autoverse" / "skills"
    assert roots[-1].source == "user"
    assert len(roots) > 2  # no .git ancestor means the implementation scans parents.
    assert "filesystem root" not in guide
    assert "filesystem root" in reference
    assert "standalone skill package copy/install helper" in reference
    assert "没有默认 CLI/REPL/WebUI install" in reference
    assert "uninstall 或 reload 入口" in reference
    assert "字段全集和精确命名规则只维护在" in guide
    assert "| 字段 | 用途 |" not in guide
    assert "完整安装市场" not in guide
    assert "完整安装市场" not in reference
    assert "marketplace" not in extensions
    assert "../design/skills.md#开放设计问题" in extensions
    assert 'if action == "install"' not in commands
    assert "/api/extensions/skills/{skill_id}/install" not in web_server
    assert "TUI 和 Line REPL" in reference
    assert "WebUI 当前不注册这些" in reference
    assert "动态 skill prompt-template slash commands 当前只由 TUI 和 Line REPL 注册" in (
        ROOT / "docs" / "reference" / "commands.md"
    ).read_text(encoding="utf-8")
    assert "register_skill_template_commands" in repl_app
    assert "register_skill_template_commands" in line_repl
    assert "register_skill_template_commands" not in web_server
    assert "register_skill_template_commands" not in web_session


def test_guides_do_not_copy_reference_level_protocol_or_inventory_tables() -> None:
    guide_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "docs" / "guides").glob("*.md"))
    )

    banned_snippets = (
        "字段形状如下",
        "mcp__<server>__<tool>",
        "`list_mcp_resources`",
        "`read_mcp_resource`",
        "\n## Hook 输入\n",
        "\n## Hook 输出\n",
        "| 事件 | 适合用途 |",
        "| 能力 | 适用场景 |",
        "Plugin browser [disabled, !1]",
        "filesystem root",
    )
    offenders = [snippet for snippet in banned_snippets if snippet in guide_text]
    assert not offenders, "\n".join(offenders)


def test_event_reference_matches_stream_event_types() -> None:
    rows = _markdown_table_rows_after_heading(
        "docs/reference/events-and-storage.md",
        "## StreamEvent 类型",
    )
    documented = {_strip_code(row["事件"]): _strip_code(row["Payload"]) for row in rows}

    assert documented == {
        event.value: payload.__name__ for event, payload in PAYLOAD_FOR.items()
    }


def test_event_storage_reference_lists_actual_implementation_sources() -> None:
    text = (ROOT / "docs" / "reference" / "events-and-storage.md").read_text(
        encoding="utf-8"
    )
    design = (ROOT / "docs" / "design" / "persistence-observability.md").read_text(
        encoding="utf-8"
    )
    builder = (ROOT / "src" / "autoverse" / "app" / "builder.py").read_text(
        encoding="utf-8"
    )
    subagent_wiring = (ROOT / "src" / "autoverse" / "app" / "subagent.py").read_text(
        encoding="utf-8"
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    for source in (
        "src/autoverse/types/events.py",
        "src/autoverse/ports/__init__.py",
        "src/autoverse/ports/context_archive.py",
        "src/autoverse/app/builder.py",
        "src/autoverse/app/subagent.py",
        "src/autoverse/core/subagent/jobs.py",
        "src/autoverse/infra/persist/serialize.py",
        "src/autoverse/infra/persist/compaction_archive.py",
        "src/autoverse/infra/persist/jsonl_store.py",
        "src/autoverse/infra/persist/redis_store.py",
        "src/autoverse/infra/persist/job_store.py",
        "src/autoverse/infra/persist/task_store.py",
        "src/autoverse/infra/observ/collector.py",
        "src/autoverse/infra/observ/model.py",
        "src/autoverse/infra/observ/timeline.py",
        "src/autoverse/infra/observ/export.py",
        "src/autoverse/infra/observ/store.py",
        "src/autoverse/infra/observ/viewer/server.py",
    ):
        assert source in text
        assert (ROOT / source).exists()
    assert "SQLite FTS 是可删除重建的派生索引" in text
    assert "JSONL/blob 是恢复真相" in text
    assert "JSONLStore" in text
    assert "RedisStore" in text
    assert "trace 是由事件和请求快照派生的只读观测模型" in text
    assert "不应该成为第二套消息协议或控制面" in text
    assert "TaskStore(data_dir)" in builder
    assert "InMemoryJobStore()" in subagent_wiring
    assert "后台子代理 job 和持久化 job 表目前不是同一条默认路径" in text
    assert "默认 builder 不把它作为后台" in text
    assert "InMemoryJobStore" in design
    assert "不是默认 `EngineBuilder` 的后台 job 真相源" in design
    assert "redis" in pyproject["project"]["optional-dependencies"]
    assert any(
        dependency.startswith("redis>=")
        for dependency in pyproject["project"]["optional-dependencies"]["redis"]
    )
    assert 'pip install -e ".[redis]"' in text


def test_workflow_reference_matches_builtin_definitions() -> None:
    rows = _markdown_table_rows_after_heading(
        "docs/reference/workflows.md",
        "## 内建 WorkflowRegistry definitions",
    )
    documented = {_strip_code(row["工作流"]): row for row in rows}
    definitions = {definition.name: definition for definition in builtin_workflow_definitions()}
    command_specs = {spec.name for spec in build_default_registry()._unique_specs()}
    text = (ROOT / "docs" / "reference" / "workflows.md").read_text(encoding="utf-8")

    assert set(documented) == set(definitions)
    for name, definition in definitions.items():
        row = documented[name]
        assert _strip_code(row["trigger"]) == definition.trigger.value
        assert _strip_code(row["orchestration"]) == definition.orchestration.value
        assert _strip_code(row["advance"]) == definition.advance.value
        assert _strip_code(row["termination"]) == ",".join(
            item.value for item in definition.termination
        )

    assert "engine.run_workflow()" in text
    assert "当前默认 slash command 表没有对应 `/fix`、`/dream` 或 `/distill`" in text
    for source in (
        "src/autoverse/app/workflow/registry.py",
        "src/autoverse/app/workflow/gates.py",
        "src/autoverse/app/workflow/goal.py",
        "src/autoverse/app/workflow/pipelines.py",
        "src/autoverse/app/workflow/self_improve.py",
        "src/autoverse/core/workflow/stop_gate.py",
    ):
        assert source in text
        assert (ROOT / source).exists()
    assert "workflows" in command_specs
    for name in ("fix", "dream", "distill"):
        assert name not in command_specs
        assert "/workflows" in documented[name]["当前入口"]
        assert f"`/{name}`" not in documented[name]["当前入口"]


def test_extension_reference_marks_admin_actions_as_session_local() -> None:
    extensions = (ROOT / "docs" / "reference" / "extensions.md").read_text(
        encoding="utf-8"
    )
    webui_api = (ROOT / "docs" / "reference" / "webui-api.md").read_text(
        encoding="utf-8"
    )

    for text in (extensions, webui_api):
        assert "session-local" in text
        assert "不会重新装配" in text
        assert "不持久化" in text


def test_semantic_work_is_kept_in_design_docs_not_roadmap_files() -> None:
    documentation = (ROOT / "docs" / "DOCUMENTATION.md").read_text(encoding="utf-8")
    template = (ROOT / "docs" / "templates" / "design.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "design" / "skills.md").read_text(encoding="utf-8")

    assert not (ROOT / "docs" / "roadmap").exists()
    assert not (ROOT / "docs" / "templates" / "roadmap.md").exists()
    assert "只有主题确实需要描述未来边界或尚未决定的语义时" in template
    assert "## 开放设计问题" in design
    assert "语义待办" in documentation
    assert "所属 `design/` 文档的“开放设计问题”" in documentation
    assert "open / in-progress / done" not in documentation
    assert "open / in-progress / dropped" not in documentation
    assert "dropped" not in documentation
    assert "解决后删除开放问题" in documentation


def test_no_done_todo_or_archive_status_tracking_docs() -> None:
    offenders: list[str] = []
    forbidden_phrases = (
        "> 文档类型：",
        "> 状态：",
        "> 状态：done",
        "> 状态：completed",
        "> 状态：closed",
        "> 状态：dropped",
        "> 状态：cancelled",
        "> 状态：已完成",
        "> 状态：已取消",
        "## 已完成",
        "## Done",
        "## Archive",
        "## 归档",
        "## Roadmap",
        "Roadmap 条目",
    )

    for path in sorted((ROOT / "docs").rglob("*.md")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            for phrase in forbidden_phrases:
                if phrase in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {phrase}")
    assert not offenders, "\n".join(offenders)


def test_docs_do_not_use_body_level_progress_status_paragraphs() -> None:
    offenders: list[str] = []
    forbidden_phrases = (
        "具体能力状态见正文状态说明",
        "状态：已实现",
        "状态：部分实现",
        "状态：设计中",
        "已实现基础",
    )

    for path in sorted((ROOT / "docs").rglob("*.md")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            stripped = line.strip()
            if stripped.startswith("状态："):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")
                continue
            for phrase in forbidden_phrases:
                if phrase in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {phrase}")
    assert not offenders, "\n".join(offenders)


def test_loose_maintenance_todo_done_markers_stay_out_of_current_docs() -> None:
    allowed = {
        ROOT / "docs" / "DOCUMENTATION.md",
    }
    marker = re.compile(r"\b(?:TODO|FIXME|XXX|DONE)\b")
    offenders: list[str] = []

    for path in sorted((ROOT / "docs").rglob("*.md")):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if marker.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_status_tracking_policy_is_centralized_in_documentation_standard() -> None:
    offenders: list[str] = []
    centralized_phrases = (
        "已经落地、取消或过期的实施计划",
        "历史追溯使用 git",
        "完成清单",
        "历史副本",
        "以 `done`、`todo`、`archive`、`plan`、`check`",
    )
    allowed = {
        ROOT / "docs" / "DOCUMENTATION.md",
    }

    for path in sorted((ROOT / "docs").rglob("*.md")):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for phrase in centralized_phrases:
                if phrase in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {phrase}")
    assert not offenders, "\n".join(offenders)


def test_skill_design_gaps_reflect_current_install_boundary() -> None:
    design = (ROOT / "docs" / "design" / "skills.md").read_text(encoding="utf-8")
    extension_admin = (ROOT / "src" / "autoverse" / "app" / "extension_admin.py").read_text(
        encoding="utf-8"
    )
    commands = (
        ROOT / "src" / "autoverse" / "frontends" / "repl" / "builtin_cmds.py"
    ).read_text(encoding="utf-8")
    web_server = (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "server.py"
    ).read_text(encoding="utf-8")

    assert "install_skill_package" in extension_admin
    assert "## 开放设计问题" in design
    assert "Standalone skill package 安装是否应进入默认管理面" in design
    assert "install record、hash-bound trust、卸载和 reload 的单一真相源" in design
    assert 'if action == "install"' not in commands
    assert "/api/extensions/skills/{skill_id}/install" not in web_server


def test_hook_docs_reference_existing_example_script() -> None:
    guide = (ROOT / "docs" / "guides" / "hooks.md").read_text(encoding="utf-8")
    reference = (ROOT / "docs" / "reference" / "hooks.md").read_text(encoding="utf-8")
    script = ROOT / "scripts" / "deny_env_write.py"
    module = runpy.run_path(str(script))
    decide = module["decision_for_payload"]

    assert "scripts/deny_env_write.py" in guide
    assert "scripts/deny_env_write.py" in reference
    assert "`file_path`" in guide or '"file_path"' in guide
    assert decide({"tool_input": {"file_path": ".env"}})["decision"] == "deny"
    assert decide({"tool_input": {"file_path": "README.md"}})["decision"] == "proceed"


def test_skill_design_doc_keeps_gaps_concise_and_non_reference_like() -> None:
    design = (ROOT / "docs" / "design" / "skills.md").read_text(encoding="utf-8")

    assert "## 开放设计问题" in design
    assert "../roadmap/" not in design
    assert len(design.splitlines()) < 260
    for phrase in (
        "## 与现有实现的关系",
        "## TUI 和 WebUI 管理面",
        "## 配置",
        "当前已实现的",
        "当前实现覆盖",
        "仍未落地的差距",
        "tools:\n  allow",
        "tool policy overlay",
        "POST /api/extensions/skills/install",
        "autoverse plugin list",
        "[[plugins.config]]",
        "trusted_hash",
    ):
        assert phrase not in design


def test_adr_index_lists_every_current_adr() -> None:
    index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    adr_files = sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))

    assert "## 当前决策" in index
    assert "../templates/adr.md" in index
    assert "P1..P8" not in index
    assert "docs/*.md" not in index
    for path in adr_files:
        assert f"]({path.name})" in index
    assert "0014-snapshot-port.md" not in index


def test_adr_docs_do_not_reference_stale_section_numbers() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "docs" / "adr").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "§" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_adr_status_is_not_duplicated_in_body() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not any(line.startswith("> 决策状态：") for line in lines[:8]):
            offenders.append(f"{path.relative_to(ROOT)}: missing decision status")
        for lineno, line in enumerate(lines, start=1):
            if line.startswith("- **状态**：") or line.startswith("> 状态："):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_current_docs_do_not_reference_removed_internal_principle_codes() -> None:
    offenders: list[str] = []
    stale_code = re.compile(
        r"\bP[0-9]\b|KDR|gap[0-9]|Workstream|Phase [0-9]|cc-webui|MiMo-Code"
    )

    for path in sorted((ROOT / "docs").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if stale_code.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_mockup_directory_has_no_webui_source_copies() -> None:
    docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    mockup_dir = ROOT / "docs" / "mockups"
    mockups = (
        sorted(path for path in mockup_dir.iterdir() if path.is_file())
        if mockup_dir.exists()
        else []
    )

    assert not mockups
    assert "mockups/" not in docs_index
    assert "docs/mockups/autoverse-webui.html" not in (
        ROOT / "src" / "autoverse" / "frontends" / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")


def test_documentation_impact_map_is_parseable_and_points_to_existing_files() -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "check_doc_impact.py"))
    areas = module["parse_impact_map"](DOC_IMPACT_PATH)

    assert areas
    assert {area.name for area in areas} >= {
        "cli-and-commands",
        "tools-and-permissions",
        "config-secrets-memory",
        "documentation-governance",
    }

    missing: list[str] = []
    for area in areas:
        assert area.code, f"{area.name} must declare code patterns"
        assert area.required_docs, f"{area.name} must declare required docs"
        for code_path in area.code:
            if code_path.endswith("/**"):
                matches = [
                    path
                    for path in (ROOT / code_path[:-3]).rglob("*")
                    if path.is_file()
                ]
                if not matches:
                    missing.append(f"{area.name}: {code_path}")
            elif not (ROOT / code_path).exists():
                missing.append(f"{area.name}: {code_path}")
        for doc_path in (*area.required_docs, *area.conditional_docs, *area.tests):
            if not (ROOT / doc_path).exists():
                missing.append(f"{area.name}: {doc_path}")
    assert not missing, "\n".join(missing)


def test_documentation_impact_checker_supports_staged_mode() -> None:
    script = (ROOT / "scripts" / "check_doc_impact.py").read_text(encoding="utf-8")
    documentation = (ROOT / "docs" / "DOCUMENTATION.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert '"--staged"' in script
    assert '"--cached"' in script
    assert "python scripts/check_doc_impact.py --strict --staged" in documentation
    assert "python scripts/check_doc_impact.py --strict --staged" in agents


def test_documentation_impact_map_routes_known_code_paths_to_docs() -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "check_doc_impact.py"))
    areas = module["parse_impact_map"](DOC_IMPACT_PATH)
    impacted_areas = module["impacted_areas"]

    cli_impacted = impacted_areas({"src/autoverse/frontends/repl/builtin_cmds.py"}, areas)
    cli_docs = {doc for area in cli_impacted for doc in area.required_docs}
    assert "docs/reference/commands.md" in cli_docs

    gateway_impacted = impacted_areas({"src/autoverse/frontends/gateway/runner.py"}, areas)
    gateway_docs = {doc for area in gateway_impacted for doc in area.required_docs}
    assert "docs/reference/extensions.md" in gateway_docs
    assert "docs/architecture/frontend-adapters.md" in gateway_docs

    tool_impacted = impacted_areas({"src/autoverse/core/tools/dispatcher.py"}, areas)
    tool_docs = {doc for area in tool_impacted for doc in area.required_docs}
    assert "docs/reference/tools.md" in tool_docs

    config_impacted = impacted_areas({"src/autoverse/app/config.py"}, areas)
    config_docs = {doc for area in config_impacted for doc in area.required_docs}
    assert "docs/reference/config.md" in config_docs

    store_port_impacted = impacted_areas({"src/autoverse/ports/__init__.py"}, areas)
    store_port_docs = {doc for area in store_port_impacted for doc in area.required_docs}
    assert "docs/reference/events-and-storage.md" in store_port_docs
    assert "docs/architecture/state-ownership.md" in store_port_docs

    context_impacted = impacted_areas(
        {"src/autoverse/core/context/compaction.py"}, areas
    )
    context_docs = {doc for area in context_impacted for doc in area.required_docs}
    assert {
        "docs/reference/context-and-compaction.md",
        "docs/architecture/runtime-flow.md",
        "docs/architecture/state-ownership.md",
    } <= context_docs


def test_documentation_governance_is_documented_for_agents() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    documentation = (ROOT / "docs" / "DOCUMENTATION.md").read_text(encoding="utf-8")

    required_phrases = [
        "python scripts/check_doc_impact.py --strict",
        "python scripts/check_doc_impact.py --strict --staged",
        "Docs-Impact: updated",
        "Docs-Impact: none",
        "docs/doc-impact.yml",
    ]
    for phrase in required_phrases:
        assert phrase in agents

    assert "文档影响治理协议" in documentation
    assert "开放设计问题" in documentation
    assert "语义待办" in documentation
    assert "历史追溯使用 git" in documentation


def test_agents_routes_every_code_change_through_documentation_impact() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    documentation = (ROOT / "docs" / "DOCUMENTATION.md").read_text(
        encoding="utf-8"
    )

    assert "### 文档影响流程（每次代码改动都必须执行）" in agents
    assert "匹配 `docs/doc-impact.yml` 中每个 `code`" in agents
    assert "同一路径可能匹配多个区域" in agents
    assert "多个区域同时匹配时必须取并集" in agents
    assert "没有匹配到有意义的区域" in agents
    assert "影响检查器通过只证明路径覆盖，不证明语义完整" in agents
    assert "代码、测试和文档一起完成" in agents
    assert "检查 `git diff --name-only`" in agents
    assert "git diff --cached --name-only" in agents
    assert "不能为了通过检查而\n暂存用户的无关工作" in agents
    assert "不能为了让代码通过而削弱\n或删除断言" in agents

    rows = _markdown_table_rows_after_heading(
        "AGENTS.md", "### 文档影响流程（每次代码改动都必须执行）"
    )
    assert len(rows) == 5
    actions = "\n".join(row["文档动作"] for row in rows)
    for layer in ("docs/guides/", "docs/reference/", "docs/architecture/", "docs/design/"):
        assert layer in actions
    assert "internal-only waiver" in actions

    non_waivable = (
        "command/flag",
        "配置字段/default",
        "HTTP route 或 request/response shape",
        "name/schema/capability",
        "event/payload",
        "model registry entry/capability",
        "sub-agent\nrole",
        "工作流定义",
        "导入边界",
        "运行时序",
        "权威状态所有者",
    )
    for phrase in non_waivable:
        assert phrase in agents

    assert "不能使用 internal-only waiver" in documentation
    assert "运行时序、生命周期或权威状态所有者" in documentation
    assert "“不需要文档”不是可审查的理由" in documentation


def test_project_docs_do_not_position_autoverse_as_external_product_clone() -> None:
    offenders: list[str] = []
    forbidden = ("Claude" + " Code", "claude" + "-code", "re" + "implementation")
    paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        *(ROOT / "docs").rglob("*.md"),
    ]

    for path in sorted(paths):
        text = path.read_text(encoding="utf-8")
        for phrase in forbidden:
            if phrase in text:
                offenders.append(f"{path.relative_to(ROOT)}: {phrase}")
    assert not offenders, "\n".join(offenders)


def test_all_markdown_docs_have_standard_metadata_headers() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()[:14]
        if not lines or not lines[0].startswith("# "):
            offenders.append(f"{path.relative_to(ROOT)}: missing H1 title")
            continue
        for prefix in REQUIRED_DOC_HEADER_PREFIXES:
            if not any(line.startswith(prefix) for line in lines):
                offenders.append(f"{path.relative_to(ROOT)}: missing {prefix}")
    assert not offenders, "\n".join(offenders)


def test_guide_docs_have_required_user_navigation_sections() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "docs" / "guides").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for section in GUIDE_REQUIRED_SECTIONS:
            if section not in text:
                offenders.append(f"{path.relative_to(ROOT)}: missing {section}")
    assert not offenders, "\n".join(offenders)


def test_quickstart_does_not_duplicate_terminal_navigation_blocks() -> None:
    quickstart = (ROOT / "docs" / "guides" / "getting-started.md").read_text(
        encoding="utf-8"
    )

    assert "## 5. 下一步" not in quickstart
    assert quickstart.count("## 相关文档") == 1
    assert "../reference/commands.md" in quickstart
    assert "../architecture/layers.md" in quickstart
    assert "../architecture/runtime-flow.md" in quickstart


def test_guides_do_not_copy_generic_template_preconditions() -> None:
    phrase = "在仓库根目录或目标项目目录运行命令；涉及 WebUI/API 的页面需要安装对应 extra。"
    offenders: list[str] = []
    for path in sorted((ROOT / "docs" / "guides").glob("*.md")):
        if phrase in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "\n".join(offenders)


def test_reference_docs_have_development_entries() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "docs" / "reference").glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        if "## 开发入口" not in text:
            offenders.append(f"{path.relative_to(ROOT)}: missing ## 开发入口")
    assert not offenders, "\n".join(offenders)


def test_non_index_docs_are_linked_from_current_navigation() -> None:
    docs_root = ROOT / "docs"
    markdown_files = sorted(docs_root.rglob("*.md"))
    incoming: set[Path] = set()
    link_re = re.compile(r"\[[^\]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)")

    for source in markdown_files:
        for target in link_re.findall(source.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            resolved = (source.parent / target).resolve()
            if resolved.is_relative_to(docs_root.resolve()):
                incoming.add(resolved)

    exempt = {docs_root / "README.md"}
    orphans = [
        str(path.relative_to(ROOT))
        for path in markdown_files
        if path not in exempt and path.resolve() not in incoming
    ]
    assert not orphans, "\n".join(orphans)


def test_design_docs_follow_required_structure_without_checklists() -> None:
    required = (
        "## 背景",
        "## 关键取舍",
        "## 边界与非目标",
        "## 关联参考",
    )
    offenders: list[str] = []

    for path in sorted((ROOT / "docs" / "design").glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        for heading in required:
            if heading not in text:
                offenders.append(f"{path.relative_to(ROOT)}: missing {heading}")
        if re.search(r"^- \[[ xX]\]", text, flags=re.MULTILINE):
            offenders.append(f"{path.relative_to(ROOT)}: implementation checklist")
        if re.search(r"\bTBD\b", text):
            offenders.append(f"{path.relative_to(ROOT)}: TBD placeholder")
    assert not offenders, "\n".join(offenders)


def test_architecture_documents_component_lifecycles_and_state_owners() -> None:
    component_map = (
        ROOT / "docs" / "architecture" / "component-map.md"
    ).read_text(encoding="utf-8")
    state_ownership = (
        ROOT / "docs" / "architecture" / "state-ownership.md"
    ).read_text(encoding="utf-8")
    runtime_flow = (
        ROOT / "docs" / "architecture" / "runtime-flow.md"
    ).read_text(encoding="utf-8")
    conversation = (
        ROOT / "src" / "autoverse" / "core" / "state" / "conversation.py"
    ).read_text(encoding="utf-8")
    jobs = (
        ROOT / "src" / "autoverse" / "core" / "subagent" / "jobs.py"
    ).read_text(encoding="utf-8")
    prompt_assembler = (
        ROOT / "src" / "autoverse" / "core" / "prompt" / "assembler.py"
    ).read_text(encoding="utf-8")

    assert "每 session" in component_map
    assert "tool call 级" in component_map
    assert "SessionBridge" in component_map
    assert "trace、Web block log 和 sidebar state 是事件派生视图" in component_map

    rows = _markdown_table_rows_after_heading(
        "docs/architecture/state-ownership.md", "## 当前所有权"
    )
    documented_states = {row["状态"] for row in rows}
    assert {
        "会话消息",
        "会话元数据",
        "当前 turn 控制状态",
        "工具调用状态",
        "后台子代理 job",
        "Extension 管理状态",
        "Trace",
        "Web block replay",
        "Workspace snapshot",
        "Worktree isolation",
    } <= documented_states
    assert "store-first" in state_ownership
    assert "SSE 断线" in state_ownership
    assert "The write contract\nis *store-first*" in conversation
    assert "not a persistent store" in jobs

    prompt_rows = _markdown_table_rows_after_heading(
        "docs/architecture/component-map.md", "## Prompt 装配顺序"
    )
    assert [int(row["顺序"]) for row in prompt_rows] == list(range(1, 10))
    source_markers = (
        "PROMPT_SECTIONS.intro",
        "if self.tool_list:",
        "PROMPT_SECTIONS.subagent_workflow",
        "for name in sorted(self._fragments)",
        "self._session_guidance()",
        "PROMPT_SECTIONS.compaction_awareness",
        "if self.environment:",
        "if self.memory:",
        "if self.append:",
    )
    source_positions = [prompt_assembler.index(marker) for marker in source_markers]
    assert source_positions == sorted(source_positions)

    assert runtime_flow.count("```mermaid") >= 4
    assert "PreToolUse" in runtime_flow
    assert "Store/Conversation owns messages" in runtime_flow


def test_runtime_flow_matches_hook_dispatcher_order() -> None:
    runtime_flow = (
        ROOT / "docs" / "architecture" / "runtime-flow.md"
    ).read_text(encoding="utf-8")
    tool_design = (
        ROOT / "docs" / "design" / "tools-and-permissions.md"
    ).read_text(encoding="utf-8")
    builder = (ROOT / "src" / "autoverse" / "app" / "builder.py").read_text(
        encoding="utf-8"
    )
    hooks = (
        ROOT / "src" / "autoverse" / "infra" / "hooks" / "engine.py"
    ).read_text(encoding="utf-8")

    assert "middleware(c, ctx, dispatcher.dispatch_one)" in builder
    assert "outcome = await self.run(HookEvent.PRE_TOOL_USE" in hooks
    assert "result = await nxt(call, ctx)" in hooks
    assert "await self.run(HookEvent.POST_TOOL_USE" in hooks
    assert "先运行 `PreToolUse`；deny 会在 dispatcher 前短路" in runtime_flow
    assert "PreToolUse hook、lookup/schema、plan-mode gate、permission" in tool_design


def test_context_reference_matches_compaction_and_rewind_boundaries() -> None:
    text = (
        ROOT / "docs" / "reference" / "context-and-compaction.md"
    ).read_text(encoding="utf-8")
    policy = (
        ROOT / "src" / "autoverse" / "core" / "context" / "compaction.py"
    ).read_text(encoding="utf-8")
    loop = (
        ROOT / "src" / "autoverse" / "core" / "loop" / "turn_loop.py"
    ).read_text(encoding="utf-8")

    rows = _markdown_table_rows_with_columns(
        "docs/reference/context-and-compaction.md", {"App 配置", "Core policy 含义"}
    )
    assert {_strip_code(row["App 配置"]) for row in rows} == {
        "compact_threshold",
        "summary_threshold",
        "emergency_threshold",
    }
    assert "_over_hard_ceiling" in policy
    assert "_over_compact_threshold" in policy
    assert "_cooldown_active" in policy
    documented_kinds = set(
        re.findall(
            r"`(micro|truncate|overflow|overflow_truncate|no_reduction|summary)`",
            text,
        )
    )
    actual_kinds = set(re.findall(r'kind="([a-z_]+)"', policy))
    assert documented_kinds == actual_kinds
    assert "`compaction.context_window > 0` 时它是显式覆盖" in text
    assert "文件恢复失败时不截断历史" in text
    assert "Fail-closed: if the file restore raises" in loop


def test_contributor_guide_covers_layering_docs_and_full_validation() -> None:
    guide = (ROOT / "docs" / "guides" / "contributing.md").read_text(
        encoding="utf-8"
    )
    for layer in ("types/", "ports/", "core/", "infra/", "app/", "frontends/"):
        assert f"`src/autoverse/{layer}`" in guide
    for command in (
        "pytest",
        "mypy src/autoverse",
        "import-linter lint",
        "python scripts/check_doc_impact.py --strict",
        "pytest tests/contract/test_documentation_contracts.py",
        "git diff --check",
    ):
        assert command in guide
    assert "../architecture/state-ownership.md" in guide
    assert "internal-only waiver" in guide


def test_document_templates_exist_and_are_referenced() -> None:
    missing = [path for path in REQUIRED_TEMPLATE_FILES if not (ROOT / path).exists()]
    assert not missing, "\n".join(missing)

    documentation = (ROOT / "docs" / "DOCUMENTATION.md").read_text(encoding="utf-8")
    template_index = (ROOT / "docs" / "templates" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "templates/README.md" in documentation
    for path in REQUIRED_TEMPLATE_FILES:
        if path.endswith("README.md"):
            continue
        assert f"]({Path(path).name})" in template_index


def test_design_template_separates_design_from_status_tracking() -> None:
    template = (ROOT / "docs" / "templates" / "design.md").read_text(encoding="utf-8")
    documentation = (ROOT / "docs" / "DOCUMENTATION.md").read_text(encoding="utf-8")

    assert "模板是各类文档结构的唯一规范来源" in documentation
    assert "## 关键取舍" in template
    assert "## 边界与非目标" in template
    assert "目标形态" in template
    assert "开放设计问题" in template
    assert "只有主题确实需要" in template
    assert "不要保留空章节" in template
    assert "roadmap" not in template.lower()
    assert "## 当前实现" not in template
    assert "## 待验证问题" not in template
