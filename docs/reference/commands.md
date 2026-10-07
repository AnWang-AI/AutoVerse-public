# 命令参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

AutoVerse 的 `/` 命令由同一张命令注册表派生：帮助文本、补全、palette、键位和 handler 都来自 `CommandSpec`。新增命令时，不要维护第二份命令列表。

## 快速索引

- 查现有命令：见[内建命令](#内建命令)。
- 理解 workflow 入口：见[Workflows](#workflows)。
- 管理 skills/plugins：见[Skills 和 plugins](#skills-和-plugins)。
- 编写 Markdown 命令：见[提示词模板](#提示词模板)和[用户自定义命令](#用户自定义命令)。
- 查展示与 LLM 上下文边界：见[交互显示](#交互显示)。

## 命令类型

| 类型 | 用途 | 输出是否进入 LLM 上下文 |
|------|------|--------------------------|
| 普通命令 | 显示状态、切换设置、列会话、打开帮助 | 否 |
| 提示词命令 | 把模板展开成一轮 prompt | 是 |
| 交互命令 | 选择模型、选择会话、确认危险操作 | 取决于命令结果 |
| 技能命令 | 由 skill 注册的命令入口 | 取决于 skill |

## 内建命令

下表按当前 `build_default_registry()` 生成。`busy` 表示命令可在 turn 运行中执行；空白表示通常要求空闲状态或由命令自身判断。

| 命令 | 分组 | 摘要 | 快捷键/别名 | busy |
|------|------|------|-------------|------|
| `/help` | view | show all commands | `?` | yes |
| `/context` | view | context-window token occupancy |  | yes |
| `/cost` | view | cost grouped by model |  | yes |
| `/usage` | view | per-model token usage this session |  | yes |
| `/diff` | view | git diff of this session's changes |  | yes |
| `/export` | view | export trace to a file (.html/.md/.json) |  |  |
| `/memory` | view | show loaded memory sources |  |  |
| `/session-tree` | view | sub-agent session hierarchy |  | yes |
| `/subagents` | view | open a sub-agent's captured context | `ctrl+r` | yes |
| `/details` | view | toggle showing completed tools | `ctrl+o` | yes |
| `/expand` | view | how to view truncated output (trace) |  | yes |
| `/clear` | session | clear history, keep session |  |  |
| `/compact` | session | manually compact context |  |  |
| `/rewind` | session | revert files + history to a message |  |  |
| `/sessions` | session | list/resume stored sessions |  |  |
| `/goal` | session | show/set the active completion goal |  |  |
| `/queue` | session | show queued inbound messages |  | yes |
| `/workflows` | session | list registered workflows |  | yes |
| `/commit` | session | inspect → message → commit pipeline |  |  |
| `/review` | session | independent code-review pipeline |  |  |
| `/pr` | session | summarize → draft → open a pull request |  |  |
| `/init` | session | scan repo → write AGENTS.md |  |  |
| `/coordinate` | session | architect → builder → reviewer scoring loop |  |  |
| `/stop` | session | interrupt the running turn |  | yes, only while running |
| `/quit` | session | exit after the current turn |  |  |
| `/exit` | session | exit after the current turn |  |  |
| `/model` | model | view or switch the model | `ctrl+m` |  |
| `/tiers` | model | view or set sub-agent models | aliases: `/subagent-model`, `/subagent-models` |  |
| `/doctor` | system | environment self-check |  | yes |
| `/config` | system | show effective config |  |  |
| `/permissions` | system | show active permission rules |  |  |
| `/secret` | system | manage model API keys (values hidden) | alias: `/secrets` |  |
| `/trace` | system | current session trace summary |  | yes |
| `/skills` | extend | list loaded skills / manage skill state |  | yes |
| `/mcp` | extend | show MCP server status/tools |  | yes |
| `/plugin` | extend | list loaded plugins |  |  |

`/sessions` 的列表/切换视图读取共享 session catalog summary；消息恢复仍由 Store 完成。
查看或置顶一条会话只更新 catalog-owned 导航字段，不重写 transcript JSONL。命令执行前若
Web engine 由有界 view snapshot 延迟激活，会先通过公开 `sync_history` 补齐需要读写的完整
Conversation。

## 用户如何使用

普通对话直接输入文本。以 `/` 开头的输入会先进入命令系统：

```text
/help
/model
/sessions
/compact
/review src/autoverse/core/tools
```

命令可以返回展示文本，也可以返回一个 prompt，让前端把它作为新的用户消息提交给 engine。展示文本不会进入模型上下文；提示词命令展开后的 prompt 会进入上下文。

## Workflows

`/workflows` 列出 `WorkflowRegistry` 中当前注册的 workflow definitions。带
DialogHost 的前端会打开选择器；选择任意条目后通过 `engine.run_workflow()`
执行。没有直接 slash command 的注册目标会以裸名显示，例如 `dream`，不会显示
成不存在的 `/dream`。

`/commit`、`/review`、`/pr`、`/init` 和 `/coordinate` 是默认注册的直接
slash command。`fix`、`dream`、`distill` 是 `run_workflow` 目标，但当前默认
命令表没有 `/fix`、`/dream` 或 `/distill`。

## Skills 和 plugins

`/skills` 的主语是已加载 skills，而不是所有 extension 分组命令。TUI、Line REPL 和 WebUI
的内建 extension 管理命令都通过 command registry dispatch，因此输入同一条管理命令会得到同一语义：

```text
/skills
skills:
  excalidraw-diagram [user] enabled - Draw diagrams
```

有 `ExtensionAdminService` 时，`/skills` 默认等价于 `/skills list`，列出当前 session 加载到 skill registry/admin service 的 skill：名称、来源、状态和摘要。它不会再把 `/plugin`、`/mcp` 或 `/skills` 这些 extension-management builtins 混进 skill 列表。

支持的 skill 管理子命令：

| 命令 | 含义 |
|------|------|
| `/skills list` / `/skills ls` | 列出已加载 skills |
| `/skills show <name>` | 显示 skill 详情和正文 |
| `/skills doctor <name> [prompt]` | 诊断加载、匹配和触发情况 |
| `/skills enable <name>` | 在当前 admin service 中启用 skill |
| `/skills disable <name>` | 在当前 admin service 中禁用 skill |
| `/skills trust <name>` | 标记 skill 为 trusted |

如果当前上下文没有 extension admin，`/skills` 会退回到只列出真实注册的 skill slash commands，例如 `/demo-skill`。这个退化路径只用于无 admin 的最小环境。动态 skill prompt-template slash commands 当前只由 TUI 和 Line REPL 注册；WebUI 暴露的是 `/skills` 管理命令、extension endpoints 和 registry 贡献的 `Skill` 工具。

Plugin 管理使用 `/plugin`：

| 命令 | 含义 |
|------|------|
| `/plugin` / `/plugin list` | 列出加载到 plugin manager 的 plugins |
| `/plugin show <id>` | 显示 plugin 详情 |
| `/plugin doctor <id>` | 诊断 plugin 状态和 capabilities |
| `/plugin enable <id>` | 启用 plugin |
| `/plugin disable <id>` | 禁用 plugin |
| `/plugin trust <id>` | 标记 plugin 为 trusted |

右侧栏的 `Extensions` 面板是只读状态镜像：它展示所有加载的 skills/plugins、状态和诊断数量；实际管理动作仍通过 `/skills`、`/plugin` 或 WebUI extension endpoints 执行。

## 提示词模板

提示词命令支持这些模板能力：

- `$ARGUMENTS`：原始参数串。
- `$1`、`$2`：位置参数；最后一个位置占位符可以吃掉剩余参数。
- `` !`cmd` ``：在项目目录执行 shell 并把 stdout 注入模板。
- `@path`：保留原始引用，并把可读文件内容作为 `--- @path ---` 文本块追加到同一个 prompt；缺失、不可读或超大文件按 best-effort 跳过或截断。

如果模板没有占位符但用户传了参数，参数会追加到模板末尾。

示例模板：

```md
---
description: Review a path
---

Review the following path and report correctness issues:

$1

Current diff:

!`git diff -- $1`
```

调用：

```text
/review-path src/autoverse/frontends/web
```

展开后会把 `$1` 替换成路径，并把 shell 输出嵌入 prompt。

## 用户自定义命令

用户可以用 Markdown 文件声明命令。frontmatter 放描述、model 以及兼容预留的 agent/subtask 元数据，正文作为模板。

推荐把团队常用工作流沉淀为模板命令，而不是写 Python handler。

### 加载路径和优先级

当前 loader 只以启动时的 `cwd` 和用户 home 为基准，不向上查找 git root。每个目录下递归加载 `*.md`；命令名来自文件 stem。同名冲突采用 first-wins：内建命令、skill 命令或更早发现的文件命令已经占用名称时，后续文件会被静默跳过，避免启动失败。

| 顺序 | 目录 |
|------|------|
| 1 | `<cwd>/.autoverse/command` |
| 2 | `<cwd>/.autoverse/commands` |
| 3 | `<cwd>/.claude/command` |
| 4 | `<cwd>/.claude/commands` |
| 5 | `<cwd>/command` |
| 6 | `<cwd>/commands` |
| 7 | `<home>/.config/autoverse/command` |
| 8 | `<home>/.config/autoverse/commands` |

常见 frontmatter 字段：

| 字段 | 含义 |
|------|------|
| `description` | 命令说明，显示在帮助和补全里 |
| `model` | 单轮模型覆盖元数据；TUI prompt-template turn 会通过 `switch_model` 临时生效并在 turn 后恢复，Line REPL/WebUI 当前不应用该字段 |
| `agent` | 已解析但当前 inline 命令路径不实际路由；TUI 会提示尚未支持，Line REPL/WebUI 当前不额外提示 |
| `subtask` | 已解析但当前 inline 命令路径不创建隔离子任务；TUI 会提示尚未支持，Line REPL/WebUI 当前不额外提示 |

## 交互显示

终端前端会从注册表派生：

- `/` 内联补全。
- 命令 palette。
- 禁用态和隐藏态。
- hints 和参数提示。
- toast、select、confirm、text screen 等交互结果。

显示层有一个重要边界：**命令展示输出不进入 LLM 上下文**。例如 `/help`、`/cost`、`/sessions` 输出给用户看，但不污染下一轮模型输入。只有真实用户消息和提示词命令展开结果会进入对话。TUI 中 `/cost` 和 `/usage` 的多行明细从 3 行起进入 scrollable viewer，避免占用 conversation scroll 区。

## 新增命令的建议

优先顺序：

1. 能用 Markdown 模板表达的，用提示词命令。
2. 需要读取状态或展示信息的，用普通 Python handler。
3. 需要用户选择或确认的，接入 dialog host。
4. 需要长期复用的一组命令，考虑放进 skill。

新增命令时应同时确认：

- 是否进入 LLM 上下文。
- 是否允许 busy 状态下执行。
- 是否有 aliases。
- 是否应该显示在 palette。
- 是否需要 keybind。
- 是否有 tests 覆盖命令结果。

## 常见问题

### 命令输出为什么没有影响模型？

普通命令只显示给用户，不进上下文。如果希望命令影响模型，命令需要返回 prompt。

### 模板里的 shell 失败怎么办？

模板展开应尽量把失败信息作为文本注入，而不是让命令崩溃。这样模型能看到“shell 失败了什么”，用户也能继续操作。

### 什么时候不要用 shell 注入？

不要在模板里放高风险、有副作用或需要权限审批的 shell。模板 shell 适合读状态，例如 `git diff`、`git status`、`pytest --collect-only` 这类命令。

## 开发入口

- `src/autoverse/frontends/repl/builtin_cmds.py`
- `src/autoverse/frontends/repl/commands_template.py`
- `src/autoverse/frontends/repl/dialogs/`

设计背景见 [`../design/frontend.md`](../design/frontend.md)。
