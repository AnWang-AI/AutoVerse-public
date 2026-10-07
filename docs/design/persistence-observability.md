# 持久化与可观测性设计

> 当前事实来源：`docs/reference/`、`docs/architecture/`、`src/autoverse/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

持久化和可观测性服务于两个目标：让会话可恢复，让运行问题可诊断。它们不应该成为 core 的控制面。

## 背景

消息存储、后台任务、trace 和 Web replay 都会记录运行信息，但它们的可靠性、生命周期和查询方式不同。把它们合并为一套“状态数据库”会让观测数据反向控制运行，也会掩盖默认后台 job 仍是进程内状态的事实。

## 关键取舍

### 持久化

持久化层保存：

- 会话消息。
- 会话元数据。
- session-isolated 任务状态。
- 可回放的历史。

core 只依赖 store port，具体 JSONL、Redis 或其他存储实现属于 infra。

导航状态不与 transcript 混写。`SessionCatalogPort` 的 SQLite WAL adapter 拥有
pinned/last-viewed，小型原子更新不会触发 JSONL rewrite；标题、cwd、消息计数和活动结果
是 Store-derived projection，按 source mtime 修复。catalog 故障只能降低 rail 查询能力，
不能回滚或删除已成功的 Conversation。

后台子代理 job 的当前默认路径是 `InMemoryJobStore` fan-in；持久化
`JobStore` 属于可选 adapter，不是默认 `EngineBuilder` 的后台 job 真相源。
如果以后要让后台 job 跨进程恢复，应先把这个取舍升成明确设计变更，再同步
reference、app 装配和 contract tests。

### 追踪

追踪层记录事件时间线、工具调用、usage、错误、fallback 和子代理关系。它适合用于：

- 调试工具失败。
- 分析模型成本。
- 回放一次会话。
- 解释子代理做了什么。

追踪不应该决定 turn 是否继续，也不应该被工具执行路径反向依赖。

usage 的 UI 恢复采用双层派生：assistant message metadata 保存每次 provider request 的
usage/model，使 Store 在 trace 关闭时仍能恢复精确的最后一次 Context；trace 保留跨请求的
累计 usage，并在恢复 collector 时重新播种 meter。旧 trace 只有 whole-turn aggregate 时
可以作为带明确降级标签的观测回退，但不能被解释成精确 context occupancy。这样 dashboard
恢复不需要新增控制状态，也不需要为了小型展示字段重写 transcript meta。

### 压缩档案

压缩档案与 trace 是不同职责：archive 是压缩前的持久化恢复面，trace 是压缩事件的
观测面。archive 通过 port 注入 core，采用 archive-first、conversation-rewrite-second
顺序；trace 只引用 archive segment。禁用 trace 不会破坏压缩恢复，trace 格式或
viewer 变化也不会改变模型上下文。

archive 的读取必须 session-scoped、分页且有输出预算。归档内容在落盘前递归脱敏，
同时仍标记为可能包含外部不可信输入。

恢复不依赖单一自然语言搜索：模型可浏览 catalog、按 literal/regex grep，再分页读取目标
segment；FTS/BM25 只提供辅助召回。派生索引可删除重建，JSONL/blob 才是档案真相。

### 会话栏和查询 Port

前端展示会话列表、状态、未读、删除、重命名时，走 `SessionCatalogPort` 支撑的 query/admin
接口，不反射 engine 私有字段。Store rename/delete 先成功，再同步 catalog；Store 写入后的
projection 更新失败可由下次 mtime repair 恢复。

只读 transcript 浏览走独立 `SessionHistoryPort`。默认 JSONL adapter 反向扫描文件并按
真实用户 prompt 切完整 turn，因此冷打开不需要全量 hydrate Conversation。opaque cursor
只对该 adapter 有意义；消息写入和跨进程一致性仍回到 Store/Conversation。

### Web SSE 日志

WebUI 需要断线重连和 replay，因此后端应维护可寻址块日志。SSE 是传输，不是状态真相；会话真相仍在 store/core。

## 边界与非目标

- `Store` 保存会话消息和语义元数据；catalog 只拥有导航字段，trace/SSE 都不替代 Store。
- Trace 从事件和 request snapshot 派生；关闭或损坏 trace 不应改变 turn 结果。
- `SessionBridge` 为当前 Web 进程提供显示 replay，不承诺跨进程持久恢复。
- 默认 `InMemoryJobStore` 只解决后台子代理 fan-in；不把可选持久 `JobStore` 描述成默认能力。
- 当前不默认导出 OpenTelemetry；接入外部 telemetry 不能改变 core 控制流。
- 数据保留、隐私和跨进程 job 恢复需要独立的明确策略，不能由某个 frontend 的缓存偶然决定。

## 关联参考

- 当前事件、存储和 trace：[`../reference/events-and-storage.md`](../reference/events-and-storage.md)
- 状态所有权：[`../architecture/state-ownership.md`](../architecture/state-ownership.md)
- Web SSE/API：[`../reference/webui-api.md`](../reference/webui-api.md)
