# 分层与导入边界

> 当前事实来源：`src/autoverse/`、`pyproject.toml`、import-linter 和 contract tests。
> 维护规则：分层、端口、入口、导入边界或真相源位置变化时必须同步。

本页说明每层能依赖谁，以及新增代码应该放在哪里。

## `types`

共享值类型、枚举和事件 payload。这里的类型会被 core、ports、infra 和 frontends 共同使用。

规则：

- 不依赖任何 AutoVerse 内部层。
- 值类型尽量不可变。
- 不放 IO、网络、配置加载或 UI 逻辑。

## `ports`

core 依赖的协议接口，例如模型、存储、权限、隔离等能力。

规则：

- 只依赖 `types`。
- 用窄接口表达 core 真正需要的行为。
- 具体实现放在 `infra` 或 `frontends`。
- 跨前端共享的只读查询快照（例如 `SessionSummary`）可以与 query Protocol 同置；
  adapter 应一次生成完整快照，frontend 不再穿透 store 补展示字段。
- 高频导航字段和有界历史读取分别通过 `SessionCatalogPort`、`SessionHistoryPort` 表达；
  port 不携带 SQLite、JSONL offset 或 Web block 表示。
- 浏览器工具只依赖 `BrowserPort`；有序页面集合、稳定 `page_id` 和活动页可以由
  `infra/browser` 的 Playwright 持有，也可以由 Apple frontend 的可见 WebKit host 持有，
  core 不区分具体内核、页数量或进程位置。两个 adapter 都以 `types` 中不可变的
  `BrowserPageObservation` 返回一次 revision 校验后的页面状态、语义快照和可选截图，
  port 不暴露 Playwright/WKWebView 对象。`BrowserPort.evaluate` 同样不允许 Promise、DOM node
  或其它 engine host object 跨层，只返回已经等待完成且可 JSON 序列化的最终值；adapter
  把不支持的返回类型和超时转换为结构化 `BrowserError`。

## `core`

agent 行为骨架：对话循环、上下文管理、工具调度、子代理、工作流。

规则：

- 不 import `infra`、`frontends` 或 `app`。
- 不读取环境变量、配置文件或 UI 状态。
- 外部能力都通过 port、context 或注入对象进入。

上下文压缩不能直接读取 trace 文件。core 通过 `CompactionArchivePort` 先归档后
删除；本地 JSONL/blob 布局属于 infra。

## `app`

组装层。负责读取选项、创建配置快照、装配 provider、store、permission、prompt assembler 和 engine。

规则：

- 可以知道具体实现。
- 不把具体实现泄漏回 `core`。
- 新入口优先通过 `EngineBuilder` 接入。

## `infra`

具体 adapter，例如 LLM transport、JSONL/Redis store、SQLite WAL session catalog、secret
vault、worktree isolation。默认 JSONL adapter 同时实现 turn-aligned history window port，
opaque cursor 的文件偏移细节不越过 port 契约。

规则：

- 实现 port。
- 可以依赖第三方库和系统资源。
- 错误要转换成上层可理解的结构化反馈。

## `frontends`

用户交互 adapter。CLI、TUI、Line REPL 和 WebUI 都在这里；可见原生浏览器的
WebSocket broker 也属于 frontend adapter。

规则：

- 不实现对话循环。
- 不绕过 engine 反射 core 私有状态。
- 只通过公开 handle、port 和事件流交互。

## 当前 import-linter 契约

以下契约来自 `pyproject.toml`，提交前用 `import-linter lint` 检查：

| 契约 | type | source_modules | forbidden_modules |
|------|------|----------------|-------------------|
| `core does not depend on infra or frontends or app` | `forbidden` | `autoverse.core` | `autoverse.infra`, `autoverse.frontends`, `autoverse.app` |
| `ports only depend on types` | `forbidden` | `autoverse.ports` | `autoverse.core`, `autoverse.infra`, `autoverse.frontends`, `autoverse.app` |
| `types depend on nothing internal` | `forbidden` | `autoverse.types` | `autoverse.core`, `autoverse.infra`, `autoverse.frontends`, `autoverse.app`, `autoverse.ports` |
