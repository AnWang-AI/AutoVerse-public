# 事件、追踪与持久化参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

AutoVerse 的运行时以 `StreamEvent` 作为前端实时显示、Web replay 和 trace 采集的共同输入。会话存储负责序列化 `Message` / `SessionMeta` 等会话类型；trace 是由事件和请求快照派生的只读观测模型，不应该成为第二套消息协议或控制面。

压缩后需要恢复细节时使用独立的 compaction archive。archive 保存经过脱敏的被移除
消息，长内容使用 content-addressed blob；trace 的 compaction 记录只保存 segment
引用和统计信息。

archive 的 JSONL/blob 是恢复真相；SQLite FTS 是可删除重建的派生索引。正常 list、
grep 和 search 不加载无关 blob，read 只加载目标 segment。索引缺失时会从权威档案
重建；坏记录和完整性错误必须作为诊断显式返回。

## 事件流

core 在 turn 运行中发出事件，例如：

- 文本增量和最终回答。
- 工具开始和结束；工具错误由 `tool_end` 的错误标记或通用 `error` 事件承载。
- usage、cost、cache 信息。
- 子代理活动。
- compaction、gate decision、retry、error。

前端 adapter 消费这些事件并转成终端卡片或 Web 块。

provider codec 产生的工具请求起点和 LLM iteration 终点只在 accumulator 内部用于
构造 assistant message；它们不直接进入公共事件流。公共 `tool_start` 由 dispatcher
在权限判定后发出，公共 `turn_end` 由 TurnLoop 在整个 user turn 完成后发出。这样一个
实际工具执行只有一个 `tool_start`/`tool_end` 配对，一个多 iteration turn 也只有一个
最终 `turn_end`，trace 不会把同一 user turn 拆成多条伪 turn。

accumulator 还把该次 assistant/provider call 的 usage 与规范模型 id 写入 assistant
message metadata。它不改变模型上下文内容，但让 TUI/Web 在进程重启后从 Store 精确恢复
最后一次 request 的 Context，并累计恢复 cache/cost；工具结果 metadata 同理用于恢复
Todo 和 Changed 面板。旧消息没有这组 usage metadata 时，前端可以从 trace 恢复累计
统计，但 trace 的旧格式只有整 turn 聚合，因此必须标成 “last turn”，不能冒充精确
context occupancy；两条来源都没有时显示 unavailable，而不是零。

压缩摘要的内部 text/thinking 不进入前端，但它的 `usage` 会进入公共流和 trace meter，
因此自动压缩和失败重试产生的 token/cost 不会从会话用量中消失。

## 事件分类

| 类别 | 例子 | 主要消费者 |
|------|------|------------|
| 文本流 | text delta、thinking delta、final | TUI、Line REPL、WebUI |
| 工具 | tool start、tool end、error | 前端、trace、模型上下文 |
| 用量 | usage、cost、cache | footer、sidebar、trace |
| 工作流 | gate decision、compaction、retry | TurnLoop、前端、trace |
| 子代理 | subagent activity、job update | 前端、`InMemoryJobStore`、trace |
| 请求快照 | model request/system/tools snapshot | trace 和调试 |

事件 payload 的字段以当前 `types` 定义为准。文档不要复制第二份字段全集。

## StreamEvent 类型

| 事件 | Payload | 说明 |
|------|---------|------|
| `text_delta` | `TextDeltaPayload` | assistant 文本增量 |
| `thinking_delta` | `ThinkingDeltaPayload` | thinking 文本增量 |
| `tool_start` | `ToolStartPayload` | 工具调用开始和权限决策 |
| `tool_end` | `ToolEndPayload` | 工具调用结果、错误和 metadata |
| `compacting` | `CompactingPayload` | 上下文压缩、清理或溢出处理 |
| `gate_decision` | `GatePayload` | StopGate / workflow gate 判定 |
| `usage` | `UsagePayload` | 模型 token 与成本用量 |
| `turn_start` | `TurnStartPayload` | turn 开始 |
| `turn_end` | `TurnEndPayload` | turn 结束 |
| `subagent_activity` | `SubagentActivityPayload` | 子代理生命周期和进度 |
| `subagent_child` | `SubagentChildPayload` | 子代理原始事件转发 |
| `error` | `ErrorPayload` | 可恢复或不可恢复错误 |
| `retry` | `RetryPayload` | 恢复、fallback 或重试 |
| `approval` | `ApprovalPayload` | 权限审批请求 |
| `request` | `RequestPayload` | 模型请求快照 |

字段全集仍以 `src/autoverse/types/events.py` 的 payload dataclass 为准；本表只列当前事件集合和 payload 归属。

## 持久化

持久化层负责：

- 保存会话消息。
- 保存会话元数据。
- 保存 session-isolated 任务状态。
- 支持历史回放。
- 提供会话列表、删除、重命名等只读/管理查询。

会话数据分为两条明确路径：

| 数据 | Port / 默认 adapter | 一致性 |
|------|---------------------|--------|
| Message 与语义 `SessionMeta` | `Store` / `JSONLStore` | store-first；Conversation 权威内存视图 |
| pinned、last-viewed、rail projection | `SessionCatalogPort` / `SQLiteSessionCatalog` | SQLite WAL 原子小字段更新；projection 可重建 |
| Web 只读历史窗口 | `SessionHistoryPort` / `JSONLStore` 反向 mmap 扫描 | 完整 turn 的 tail/before 窗口；不写消息 |

默认 catalog 位于 session data directory 的 `session-catalog.sqlite3`。首次看到旧 JSONL 时，
query adapter 迁移 `extra.pinned`/`extra.last_viewed_at`；之后这两个字段以 catalog 为准，
projection upsert 不覆盖它们。查询会比较 JSONL mtime，只解析新增或变化的 session；缺失行
和 stale projection 自动修复。SQLite 损坏时 adapter 把数据库及 WAL/SHM 文件改名为
`.corrupt-*` 后重建索引，绝不删除 transcript。

core 只依赖 `src/autoverse/ports/__init__.py` 里的 `Store` port，不依赖具体 JSONL、Redis 或文件布局实现。默认会话存储是 `JSONLStore`；`RedisStore` 是可选 adapter，需要安装 `redis` extra 并连接外部 Redis server：

```bash
pip install -e ".[redis]"
```

后台子代理 job 和持久化 job 表目前不是同一条默认路径。默认 `EngineBuilder`
通过 `build_subagent_runner()` 安装 `core.subagent.jobs.InMemoryJobStore`
作为 `app.jobs`，用于后台 fan-in、`JobOutput`、job list/stop 和
`BackgroundGate` 的 pending 检查。`infra.persist.JobStore` 是 JSONL
snapshot job 状态机 adapter，但当前默认 builder 不把它作为后台
sub-agent job 的持久化真相源。

## WebUI SSE 和 replay

WebUI 需要处理浏览器断线和刷新，因此后端要维护可寻址的块日志：

```text
StreamEvent -> TuiEventSink/WebLog -> SessionBridge -> SSE -> Browser
```

SSE 是传输层，不是会话真相。刷新后应该从 store 或 bridge replay 恢复显示。Web
首次连接先由 `SessionHistoryPort` 读取最近两个完整 turn，再发送 `SessionBridge` 的有界
尾页；更早 turn 通过 cursor 查询。首包/page 同时受 200 blocks 和 128 KiB JSON 预算，
单个超预算 block 返回预览和显式展开句柄；超大 turn 仍直接显示用户消息、最终回答和
footer，只把中间执行过程延迟到用户展开时读取。图片正文通过 session resource route
懒加载。分页 cursor、resource URL、page bridge 和浏览器 DOM 都是可丢弃的展示投影，
不能反向覆盖 `Conversation` 或 `Store`。

## 可观测性

追踪层适合记录：

- turn 时间线。
- 工具输入输出摘要。
- 模型 usage 和成本。
- 错误、重试和 fallback。
- 子代理父子关系。

追踪数据由事件和请求快照派生，用于调试和回放，不应该成为 core 的控制面。
当前默认实现提供本地 trace model、timeline、export 和只读 viewer；不默认导出
OpenTelemetry。

`CompactingPayload.folded` 仅用于读取旧事件；当前生产路径不再把原始 folded 消息
送入 trace，而是记录 `archive_segment_id`、`folded_count` 和内容 hash。compaction
记录会立即进入 trace JSONL，因此崩溃恢复仍能看到压缩发生过，但原文只能通过受控
archive port 读取。

恢复已有会话时 collector 合并增量 trace，turn index 由独立的原子 allocator 分配，
不会因进程重启或两个前端同时运行而重新从 0 开始。恢复 collector 时 meter 也从已持久
turn usage 重新播种，所以 `/usage`、`/cost` 和前端累计费用不会只计算本次进程启动后的请求。

`TraceCollector` 从 `ToolEndPayload` 保留稳定工具错误种类和可行动错误消息。当前 trace
错误分类显式包含 `unsupported_result`，因此 `browser_evaluate` 的非 JSON/WebKit host
object 返回值不会在 trace 中退化为 `unknown`；调试时可以区分协议返回类型错误、timeout
和一般 `tool_error`。新的稳定错误种类必须同时更新 trace `ErrorKind` 和验收测试。

## 调试路径

如果用户看到的内容和模型上下文不一致，按这个顺序查：

1. `StreamEvent` 是否发出。
2. 前端 sink 是否消费并渲染。
3. store 是否保存消息。
4. trace 是否记录请求快照。
5. WebUI replay 是否遗漏块。

如果只在实时显示出现、刷新后消失，通常是持久化或 replay 问题。如果模型上下文里没有，通常是 message/store 或 prompt assembler 问题。

## 常见问题

### 为什么 trace 不应该控制运行？

trace 是观测面。让 trace 参与控制会让“记录发生了什么”和“决定接下来做什么”互相依赖，增加恢复和测试复杂度。

### 为什么前端不能自己维护会话真相？

前端可以缓存展示派生状态，但真相应在 core/store。否则 TUI、WebUI、Line REPL 会出现状态漂移。

## 开发入口

- `src/autoverse/types/events.py`
- `src/autoverse/ports/__init__.py`
- `src/autoverse/ports/context_archive.py`
- `src/autoverse/ports/session_catalog.py`
- `src/autoverse/ports/session_history.py`
- `src/autoverse/app/builder.py`
- `src/autoverse/app/subagent.py`
- `src/autoverse/core/subagent/jobs.py`
- `src/autoverse/infra/persist/`
- `src/autoverse/infra/persist/serialize.py`
- `src/autoverse/infra/persist/compaction_archive.py`
- `src/autoverse/infra/persist/jsonl_store.py`
- `src/autoverse/infra/persist/session_catalog.py`
- `src/autoverse/infra/persist/redis_store.py`
- `src/autoverse/infra/persist/job_store.py`
- `src/autoverse/infra/persist/task_store.py`
- `src/autoverse/infra/observ/collector.py`
- `src/autoverse/infra/observ/model.py`
- `src/autoverse/infra/observ/timeline.py`
- `src/autoverse/infra/observ/export.py`
- `src/autoverse/infra/observ/store.py`
- `src/autoverse/infra/observ/viewer/server.py`
- `src/autoverse/frontends/web/bridge.py`
- `src/autoverse/frontends/repl/tui_sink.py`

设计背景见 [`../design/persistence-observability.md`](../design/persistence-observability.md)。
