# 状态所有权

> 当前事实来源：`src/autoverse/core/state/`、`src/autoverse/ports/`、`src/autoverse/app/`、`src/autoverse/infra/persist/`、`src/autoverse/infra/observ/` 和 frontend adapters。
> 维护规则：状态权威所有者、持久化策略、生命周期或派生视图变化时必须同步。

本页定义运行时可变状态的唯一权威所有者。这里的“所有者”是允许裁决和写入该状态的组件；缓存、trace、SSE replay 和 UI 面板只保存派生视图。

## 背景

多个 frontend、持久化 adapter 和观测组件会看到同一场会话。如果不区分权威状态与派生状态，刷新、恢复、跨进程写入和后台任务很容易产生两个互相冲突的真相源。

## 当前所有权

| 状态 | 权威所有者 | 生命周期 | 默认持久化 | 允许的派生视图 |
|------|------------|----------|------------|----------------|
| 会话消息 | `Conversation` + `Store`；写入采用 store-first | session | `JSONLStore` | TUI/Web conversation blocks、trace request snapshot |
| 会话元数据 | `Store` 的 `SessionMeta`（语义字段：model、cwd、title、活动结果等） | session | 是 | catalog projection、session sidebar |
| 会话导航元数据 | `SessionCatalogPort`（pinned、last-viewed） | user/session | SQLite WAL | rail summary；其余 projection 可从 Store 重建 |
| 当前 turn 控制状态 | `TurnLoop` / engine | turn | 仅通过消息、事件和 trace 派生 | running indicator、elapsed、stop reason |
| system prompt | `PromptAssembler` 根据已注入输入渲染 | session 配置，逐 request 渲染 | 不单独作为会话真相 | trace request snapshot |
| 工具调用状态 | `ToolDispatcher` 执行链和 `StreamEvent` | tool call | trace 可记录 | tool cards、Web blocks |
| 原生浏览器页面、活动页与 profile | Apple `BrowserHostController` / WKWebView named data store | App/profile | controller 持有有序页面集合、稳定 `page_id` 和单调 navigation revision；WebKit 持久化 cookie、站点存储和凭据 | Python broker 缓存最近 agent 已知的 compact 页面快照（id、revision、URL、标题、loading）和活动 id，在 turn 边界刷新；`BrowserPageObservation` 是单次不可变派生值 |
| 浏览器控制权 | Apple `BrowserHostController.controlOwner` | App/browser host；下一条有效聊天消息自动恢复 agent | 否 | Take Over、状态栏、`user_control` 工具错误 |
| Todo/任务状态 | 注入 `AppContext.tasks` 的 task store | session | adapter 决定 | sidebar Todo 面板 |
| 后台子代理 job | 默认 `InMemoryJobStore` | process/session | 否 | job tools、Agents 面板、子代理 transcript |
| Goal 状态 | `GoalStore`/工作流装配 | session | 通过 session meta 恢复 | `/goal` 展示和 gate event |
| Extension 管理状态 | `ExtensionAdminService` | session | 当前否 | TUI/Web `Extensions` 面板和 API 响应 |
| Trace | `TraceCollector` 从事件和 request snapshot 派生 | session/run | 配置开启时本地持久化 | `/trace`、export、viewer |
| Sidebar usage/Todo/Changed | Store 中 assistant usage metadata 与 tool result metadata 的事件派生视图；trace 只补累计/旧格式 | session/view | 随 Message/trace 持久化，不另建控制真相 | TUI/Web Context、Todo、Changed |
| Web block replay | `SessionBridge` + `SessionHistoryPort` | Web session/进程 | 内存块日志；Store 窗口按需读取 | 有界 SSE snapshot、turn-aligned history page、resource reference 和 deltas |
| Workspace snapshot | `SnapshotPort` adapter；TurnLoop 保存消息绑定 | turn/session | `snapshot_enabled` 时 shadow git | `/rewind` 选择和变更摘要 |
| Worktree isolation | `Isolator` / `WorktreeRegistry` | session/subagent job | git worktree/registry adapter | mode indicator、审计结果 |

## 一致性规则

- `Conversation` 的消息修改先写 `Store`，成功后更新内存视图；store 失败不能留下只存在于内存的消息。
- 每个 turn 开始前可从 `Store` 同步，以吸收其他 frontend/进程写入；前端 block log 不能覆盖消息存储。
- Store 消息/语义 meta 成功写入后，app 更新 catalog projection；projection 更新失败不能
  回滚 Conversation。catalog 查询会按 JSONL mtime 修复缺失或过期 projection。
- pinned 和 last-viewed 只由 catalog 原子更新；首次迁移可从旧 `SessionMeta.extra` 取值，
  后续 projection repair 不覆盖 catalog 自己的字段。catalog 损坏时隔离 SQLite/WAL/SHM
  文件并从 Store 重建，不删除 transcript。
- `SessionBridge` 的有界 snapshot/history page 只重建浏览器显示。cursor、lazy resource、
  SSE 断线、subscriber queue 丢弃或 block reset 不应删除会话消息。
- trace 是只读观测模型。关闭 trace 不得改变 turn、工具或 gate 结果。
- sidebar 冷恢复优先重放 Store message metadata；trace 可以补齐累计费用/cache 或旧会话
  whole-turn usage，但不能把 whole-turn aggregate 解释为精确 context occupancy。缺少可靠
  来源时派生视图必须显示 unavailable，而不是把 collector/sink 初始值零当成历史事实。
- 默认后台 job 表是进程内 fan-in 状态；`infra.persist.JobStore` 不是默认子代理 job 真相源。
- extension 的 enable/disable/trust 当前只改变 admin service 的 session-local 展示状态，不等价于持久化或重建 runtime capability graph。
- `NativeBrowserBroker` 不复制页面集合、DOM、cookie 或导航历史。一个已连接 Apple host
  是原生页面顺序、revision、URL/title/loading 和活动页的权威所有者；session proxy 只缓存 compact
  派生快照，在 Web turn 边界通过只读 `list_pages` 与 host 对账。页面集合、活动 id 或单页
  快照变化只作为 LLM-only 消息 metadata 派生给当前请求，不能反向覆盖 host 页面状态，
  也不改变持久化的人类消息；断线时待完成操作失败。
- `BrowserPageObservation` 只表示一次观察，不成为新的可写状态。adapter 在读取语义 DOM、
  可选截图和最终页面字段前后核对 revision；连续变化时报告观察不可用，不能返回跨 revision
  拼接的数据，也不能把已成功的页面动作回滚为失败。
- workspace snapshot 与 worktree isolation 正交：前者支持 turn 级回退，后者限制一组修改的工作区范围。

## Frontend 边界

Frontend 可以拥有布局、选中项、滚动位置、modal、临时输入和事件渲染缓存。Frontend 不得拥有或自行重建：

- 会话消息真相；
- 权限裁决规则；
- 工具执行状态机；
- workflow 终止判据；
- extension 的独立 enable/trust 真相；
- 持久 job 真相。

需要新的管理视图时，应先提供 frontend-agnostic query/admin service，再由各 frontend 渲染。

## 相关代码

- `src/autoverse/core/state/conversation.py`
- `src/autoverse/ports/__init__.py`
- `src/autoverse/ports/session_catalog.py`
- `src/autoverse/ports/session_history.py`
- `src/autoverse/app/context.py`
- `src/autoverse/app/builder.py`
- `src/autoverse/core/subagent/jobs.py`
- `src/autoverse/app/extension_admin.py`
- `src/autoverse/infra/observ/collector.py`
- `src/autoverse/infra/persist/session_catalog.py`
- `src/autoverse/frontends/web/bridge.py`
- `src/autoverse/ports/snapshot.py`
