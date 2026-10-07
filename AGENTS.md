# AutoVerse — Agent 约定

## 项目

**AutoVerse** 是一个分层清晰、可持续演进的 AI agent harness。代码位于
`src/autoverse/`，采用严格的分层架构，并要求所有共享类型遵守单一真相源原则。

## 构建、测试与检查

| 命令 | 工具 | 说明 |
|------|------|------|
| `pytest` | pytest | `asyncio_mode = auto`，测试目录为 `tests/` |
| `mypy src/autoverse` | mypy | `--strict`、Python 3.11、Pydantic plugin |
| `import-linter lint` | import-linter | 强制执行下文的分层契约 |

**开发安装：** `pip install -e ".[dev]"`

**提交代码改动前，还必须运行：**

```bash
python scripts/check_doc_impact.py --strict
pytest tests/contract/test_documentation_contracts.py
git diff --check
```

如果 worktree 中有不属于本次提交的本地改动，先只暂存本次提交文件，再运行：

```bash
python scripts/check_doc_impact.py --strict --staged
```

如果文档影响检查器报告了映射文档，应更新相应文档。只有下文流程证明本次改动
确实是纯 private refactor 时，才可以使用具体的 internal-only waiver 重新运行：

```bash
python scripts/check_doc_impact.py --strict \
  --allow-none-reason "private helper extraction only; commands, schema, errors, runtime path, and public imports unchanged"
```

每个包含代码变化的 commit 或 PR 都必须明确记录文档判断：

```text
Docs-Impact: updated
```

或者：

```text
Docs-Impact: none
Docs-Impact-Reason: private helper extraction only; commands, schema, errors,
runtime path, and public imports unchanged
```

**入口点**（新增入口统一定义在 `pyproject.toml` 的 `[project.scripts]`）：

- `autoverse` — CLI 前端（`autoverse.frontends.cli.main:main`）
- `autoverse-web` — Web server（`autoverse.frontends.web.server:main`）

## 分层架构（由 import-linter 强制执行）

```text
┌─────────────────────────────────────┐
│  frontends/   CLI · REPL · Web · Gateway
│  app/         DI container、EngineBuilder facade
├─────────────────────────────────────┤
│  core/        Loop、context、subagent、tools、prompt、state
│  ports/       Protocols（LLMProvider、Store、PermissionPort 等）
│  types/       Frozen value types（Message、StreamEvent、ToolCall 等）
├─────────────────────────────────────┤
│  infra/       LLM、存储、密钥、skills 等具体 adapter
└─────────────────────────────────────┘
```

**硬性规则：**

- `types` 不依赖任何 AutoVerse 内部层。
- `ports` 只能依赖 `types`。
- `core` 只能依赖 `types`、`ports` 和 sibling `core`；绝不能依赖
  `infra`、`frontends` 或 `app`，`TYPE_CHECKING` 下的类型引用除外。
- 具体实现放在 `infra`；`app` 负责把所有组件装配在一起。
- `pyproject.toml` 中的三条 import-linter contract 负责守住这些边界。
  新增模块时必须确认没有破坏依赖方向。

## 包结构

```text
src/autoverse/
├── types/         # 消息、事件、LLM、工具、abort、capability 等共享值类型
├── ports/         # Protocol 与共享 dataclass（SessionMeta、IsoSpec 等）
├── core/
│   ├── context/   # Conversation、CompactionPolicy、RecoveryPolicy
│   ├── loop/      # TurnLoop、accumulator、repair、types
│   ├── prompt/    # PromptAssembler
│   ├── state/     # Memory、goal state
│   ├── subagent/  # Sub-agent runner
│   ├── tools/     # ToolContext、registry、dispatcher、builtin tools
│   └── workflow/  # Stop gates、gate registry
├── app/           # AppContext、Config、EngineOptions、EngineBuilder
├── frontends/     # CLI、REPL、Gateway、Web
├── infra/
│   ├── config/    # 配置文件/环境变量加载和写入
│   ├── llm/       # Codec、transport、provider、fallback、streaming
│   ├── memory/    # Memory backend
│   ├── persist/   # Store 实现
│   ├── secrets/   # Secret vault
│   ├── skills/    # Skill system
│   ├── isolation/ # Worktree isolation
│   ├── observ/    # Trace、metrics、viewer
│   └── ...        # hooks、mcp、snapshot、permission 等
└── __init__.py    # 只 re-export core 公共 API
```

## 代码约定

### 每个模块

```python
"""说明本模块内容并在需要时引用当前设计文档的 docstring。"""

from __future__ import annotations

# 标准库 imports
# 第三方 imports
# autoverse 内部 imports

# ... code ...

__all__ = ["PublicSymbol", "AnotherPublicSymbol"]
```

- 始终包含 `from __future__ import annotations`。
- 每个公开模块都必须有 `__all__`。
- 模块 docstring 使用三引号。架构敏感模块在有助于维护时应按路径引用当前文档，
  例如 `architecture/contracts.md`、`reference/tools.md`；小型包标记模块和局部
  helper 可以使用简短 docstring。
- 不要给顶层 legacy 索引页新增带 section number 的引用。

### 值类型（`types/`）

- 共享数据使用冻结 dataclass：`@dataclass(frozen=True)`。
- Enum 使用普通 `Enum` 或 `IntEnum`，并带自己的 docstring。
- 每个类型只能定义一次；`__init__.py` 只做 re-export，不能重新定义。
  这是 contract C9，由 contract tests 强制执行。

### Ports（`ports/`）

- Protocol 使用 `@runtime_checkable`。
- 所有异步方法使用 `async def ...` 声明。
- Port 只描述 core 真正依赖的行为；具体 adapter 放在 `infra` 或 `frontends`。

### Core

- `TurnLoop` 是 agent cycle 的核心，但它把判断交给注入的 compaction、recovery、
  gate 等 policy；loop body 保持精简。
- `AppContext` 只能在 `TYPE_CHECKING` 下导入；core 运行时不能进入 app。
- 裸 `except Exception` 只有在带 `# noqa: BLE001` 和明确理由时才允许，例如
  “never break a turn on tool derivation”。

### Infra

- LLM 层中 Codec 负责编码/解码，Transport 负责发送字节，WireProvider 组合二者并
  实现 `LLMProvider` port。
- Fallback 和 recovery 是相互独立的 middleware，不能直接内联进 provider 或 loop。

### Tests

- **框架：** pytest + `pytest-asyncio`，`asyncio_mode = auto`。
- **目录：**
  - `tests/contract/` — 架构和文档不变量，优先运行；
  - `tests/unit/` — 模块级行为，目录结构镜像 `src/`；
  - `tests/acceptance/` — 端到端场景。
- **命名：** 文件使用 `test_<feature>.py`，函数使用 `test_<scenario>`。
- 共享 test double 放在 `tests/_doubles.py`。
- Fixture 放在各测试目录自己的 `conftest.py`。
- 使用 `_doubles.py` 中的 `FakeTransport`、`ScriptedProvider`、`MemStore`、
  `FixedGate` 等，保持测试无网络、可重复。

## 命名约定

| 模式 | 用途 | 示例 |
|------|------|------|
| `*Config` | 配置数据类 | `CompactionConfig`、`RecoveryConfig` |
| `*Policy` | 接收 `*Config` 的行为类 | `CompactionPolicy`、`RecoveryPolicy` |
| `*Port` | Protocol 接口 | `LLMProvider`、`PermissionPort`、`EventSink` |
| `*Payload` | 冻结事件 payload | `TextDeltaPayload`、`ErrorPayload` |
| `*Error` / `*Exception` | 异常类 | `AbortError`、`IsolationError` |
| `Fake*` / `Scripted*` | Test double | `FakeTransport`、`ScriptedProvider` |

## 设计与文档

文档位于 `docs/`，遵守 `docs/DOCUMENTATION.md`。当前契约入口是
`docs/architecture/contracts.md`。共享类型字段以源模块及其 re-export 位置为准，
不能在 prose table 中复制第二份定义。

修改代码前，必须检查 `docs/doc-impact.yml` 中可能受影响的文档。不能依赖记忆判断
是否需要更新文档。

### 文档影响流程（每次代码改动都必须执行）

文档是实现的一部分，不是后续补充工作。即使用户没有提到文档，也必须执行：

1. **编辑前**，用准备修改的代码路径匹配 `docs/doc-impact.yml` 中每个 `code`
   pattern。读取所有匹配区域的 `required_docs`，并读取语义相关的
   `conditional_docs`，再决定实现边界。同一路径可能匹配多个区域。
2. **先定位真相源**，再修改 prose。先修改权威代码模型或 registry，再更新解释其
   用户可见含义的文档。命令、配置字段、route、event、tool、model、role、workflow
   的定义不能以 prose table 为真相源。
3. **按下表判断语义影响。**一次代码改动可能需要更新多个文档层。
4. **代码、测试和文档一起完成。**不能留下“稍后更新文档”的说明。如果一个开放
   设计问题已解决，应删除该问题，把当前事实移入 reference/architecture；只有需要
   长期保留争议和翻案背景的决策才使用 ADR。
5. **结束前**，检查 `git diff --name-only`，重新核对每个匹配的影响区域，运行严格
   checker 和文档契约，并说明更新了哪些文档，或为何符合 internal-only waiver。

| 变化含义 | 文档动作 |
|----------|----------|
| 用户步骤、默认行为、成功信号或排障路径 | 更新所属 `docs/guides/`；链接 reference，不复制 inventory |
| 命令/flag、配置字段/default、route、schema、状态 token、event、tool、model、role 或 workflow | 更新所属 `docs/reference/` 及其 code-to-doc contract test |
| 依赖方向、port 所有权、装配、运行时序、生命周期或权威状态所有者 | 更新 `docs/architecture/` 及相关 import/state/runtime contract |
| 设计理由、长期边界、非目标或未解决的语义选择 | 更新所属 `docs/design/`；只有长期争议决策才新增或更新 ADR |
| 不改变行为、API、schema、真相源、生命周期和架构的纯 private refactor | 文档可以不变，但必须提供具体的 internal-only waiver |

`required_docs` 表示该页是对应代码区域的当前契约：映射事实变化时必须更新；只有 diff
确实没有改变任何已记录行为和边界时，才能提供具体 internal-only waiver。
`conditional_docs` 表示必须检查，但只有用户路径、设计理由、非目标或开放问题变化时
才更新。多个区域同时匹配时必须取并集，不能处理第一个匹配后就停止。

如果新模块或用户可见能力没有匹配到有意义的区域，必须在同一改动中给
`docs/doc-impact.yml` 增加最小责任区域。如果新文档成为某项行为或边界的当前所有者，
必须把它加入相应影响区域和入口索引。影响检查器通过只证明路径覆盖，不证明语义完整；
仍需检查示例、负能力边界、默认值、失败行为和跨前端影响。

Registry 或 schema 变化导致文档契约失败时，应同时更新当前 reference 和对照契约。
除非文档契约本身被明确改变且有覆盖相同行为的新断言，否则不能为了让代码通过而削弱
或删除断言。

以下变化**不能使用 internal-only waiver**：新增、删除、重命名或改变用户可见
command/flag、配置字段/default、HTTP route 或 request/response shape、tool
name/schema/capability、event/payload、model registry entry/capability、sub-agent
role、工作流定义、导入边界、运行时序或权威状态所有者。这些变化必须更新映射的当前
文档，并更新负责将文档 inventory 或边界与代码
对照的 contract test。

Waiver 必须指出实际保持不变的 invariant。好的例子：

```text
Docs-Impact: none
Docs-Impact-Reason: extracted a private parser helper only; command tokens,
config schema, error behavior, runtime sequence, and public imports are unchanged
```

坏的例子：`Docs-Impact-Reason: no docs needed`。

在 dirty worktree 中，不能根据无关本地改动判断文档影响。只暂存本次 patch，检查
`git diff --cached --name-only`，再用 `--staged` 运行 checker。不能为了通过检查而
暂存用户的无关工作。

关键文档：

- `docs/DOCUMENTATION.md` — 文档分层、模板和影响治理协议
- `docs/doc-impact.yml` — 代码路径到文档责任的映射
- `docs/guides/contributing.md` — 完整贡献流程和验证路径
- `docs/architecture/contracts.md` — 当前契约和真相源地图
- `docs/architecture/layers.md` — 导入边界
- `docs/architecture/component-map.md` — 运行时组件和生命周期
- `docs/architecture/state-ownership.md` — 权威状态所有者和派生视图
- `docs/reference/*.md` — 当前用户可见行为和字段参考
- `docs/design/*.md` — 设计理由、目标边界和开放设计问题

不要在 docs 中新增实现历史或进度跟踪文档。仍然有效的事实应移入当前
guide/reference/architecture/design；语义未来工作只保留在所属 design 文档的“开放设计
问题”中。历史从 git 或外部 issue 获取。

不要创建名为 done、todo、archive、plan、check 或同义词的文档目录。

不要在文档或项目元数据中通过与其他 agent 产品比较来定义 AutoVerse；直接描述当前
AutoVerse 的行为。

## 依赖

依赖名称、版本约束、Python 要求、scripts 和 extras 的权威来源是
`pyproject.toml`。不要在这里复制 package list；需要变化时更新 `pyproject.toml` 和
面向用户的安装文档。
