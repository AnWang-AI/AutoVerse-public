# 契约与共享类型

> 当前事实来源：`src/autoverse/`、`pyproject.toml`、import-linter 和 contract tests。
> 维护规则：分层、端口、入口、导入边界或真相源位置变化时必须同步。

契约文档只描述当前架构边界和真相源位置；字段全集以代码和对应 reference 为准。

## 现在应该读什么

- 分层边界：[`layers.md`](layers.md)
- 组件与生命周期：[`component-map.md`](component-map.md)
- 状态权威所有者：[`state-ownership.md`](state-ownership.md)
- 运行路径：[`runtime-flow.md`](runtime-flow.md)
- 事件和存储：[`../reference/events-and-storage.md`](../reference/events-and-storage.md)
- 工具参考：[`../reference/tools.md`](../reference/tools.md)
- 模型参考：[`../reference/models.md`](../reference/models.md)

## 原则

共享类型的权威位置应该在 `types` 或具体下放的实现模块中。不要为了写文档复制第二份字段定义；文档只说明在哪里查和如何使用。

## 当前权威位置

| 主题 | 当前权威位置 | 说明 |
|------|--------------|------|
| 共享消息、事件、工具结果、能力等级 | `src/autoverse/types/` | `types/__init__.py` 只 re-export，不重新定义 |
| core 依赖的协议 | `src/autoverse/ports/` | 具体 adapter 放在 `infra/` 或 `frontends/` |
| 浏览器观察值 | `src/autoverse/types/browser.py` 的 `BrowserPageObservation` | 一次 revision 一致的页面状态、语义快照和可选截图；字段只在该 frozen type 定义 |
| 浏览器操作协议 | `src/autoverse/ports/browser.py` 的 `BrowserPort` | Playwright infra adapter 与 Apple 原生 WebKit frontend host 实现同一窄接口；页面用稳定 `page_id` 定位，adapter 持有活动页并实现 `observe_page`；`evaluate` 等待受支持的异步函数体并只允许 JSON 兼容最终值跨端口 |
| 会话栏查询值 | `src/autoverse/ports/query.py` 的 `SessionSummary` | catalog/query adapter 生成完整展示快照；前端不另读 Store 补字段 |
| 会话导航状态 | `src/autoverse/ports/session_catalog.py` | `SessionCatalogPort` 拥有 pinned/last-viewed；`SessionProjection` 其余字段可从 Store 重建 |
| 会话历史窗口 | `src/autoverse/ports/session_history.py` | `SessionHistoryPort` 返回完整 turn 的 tail/before 窗口和 opaque cursor |
| 配置树 | `src/autoverse/app/config.py` | `docs/reference/config.md` 说明用户可见字段 |
| 模型规格和价格 | `src/autoverse/infra/llm/registry.py` | `docs/reference/models.md` 说明 registry 语义 |
| 子代理角色 | `src/autoverse/core/subagent/definition.py` | `docs/reference/subagents.md` 说明角色边界 |
| 工具注册和执行 | `src/autoverse/core/tools/` | `docs/reference/tools.md` 说明工具安全边界 |
| 压缩档案值类型 | `src/autoverse/types/context.py` | segment ref、分页和搜索结果的唯一类型定义 |
| 压缩档案协议 | `src/autoverse/ports/context_archive.py` | core 删除历史前依赖的持久化/检索能力 |
| turn 编号协议 | `src/autoverse/ports/turn_index.py` | 跨进程原子分配 session turn index |
| 运行时装配 | `src/autoverse/app/builder.py` | `app/` 可以知道具体实现，`core` 不可以 |

`EngineOptions.initial_messages` 是 frontend/app 间的默认化 DI seam，不是新的消息真相源。
它只能接收 Store-derived snapshot；`initial_messages_complete=false` 明确表示这是有界尾页，
builder 不得用它覆盖完整会话的 catalog projection。turn 开始和非 turn 历史修改仍通过公开
engine sync 回到 Store。`EngineHandle.sync_history` 的用途仅是这条一致性边界。

assistant `Message.metadata` 中的 per-request usage/model 是 Store-backed replay metadata：
它由 accumulator 从 `UsagePayload` 派生，不进入 prompt 正文，也不成为另一套 usage 类型定义。
字段含义仍以 `Usage` 和 `UsagePayload` 为准；frontend 只用它恢复派生 dashboard。

`EngineOptions.browser` 是另一个 defaulted DI seam：Apple WebUI 可以注入 frontend-owned
的可见浏览器；没有注入时 builder 仍按配置创建 Playwright session。工具只面向
`BrowserPort`，不能通过 core import 任一具体浏览器实现。页面动作反馈通过
`BrowserPageObservation` 跨层传递；adapter 必须在观察前后核对同一页的单调 revision，
不能把不同导航版本的 DOM、URL 或截图拼成一个结果。
