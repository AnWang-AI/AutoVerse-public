# 上下文与压缩参考

> 当前事实来源：`src/autoverse/core/state/conversation.py`、`src/autoverse/core/context/`、`src/autoverse/core/loop/turn_loop.py`、`src/autoverse/app/engine.py` 和相关测试。
> 维护规则：消息所有权、上下文估算、压缩级联、clear/compact/rewind 行为或事件变化时必须同步。

本页说明一场 session 的消息上下文如何保存、估算、压缩、清空和回退。配置字段默认值仍以 [`config.md`](config.md) 为准。

## 当前边界

- `Conversation` 是当前 session 的权威内存消息视图，通过 `Store` store-first 持久化。
- `ConversationView` 是不可变快照，并提供当前消息的 token 估算。
- Web view session 可用 `SessionHistoryPort` 取得的 Store-derived tail snapshot 调用
  `Conversation.hydrate_from`，避免冷打开重复加载；这只是 activation seed。普通 turn
  `sync_from_store`，compact/clear/rewind/命令先 `EngineHandle.sync_history`。
- `CompactionPolicy` 决定何时折叠、摘要或截断；实际消息 rewrite 仍由 `Conversation` 完成。
- prompt、trace、Web block log 和 sidebar token 数是派生数据，不是消息真相源。

## 自动压缩级联

有效 context window 由 app 注入的 live window provider 提供：`compaction.context_window > 0` 时它是显式覆盖，否则从当前模型 registry 读取；未知模型使用 200,000 的保守窗口。没有 live provider 的独立 core 路径才使用 core config 或安全 fallback。

自动级联按当前实现执行：

1. 达到 `emergency_threshold`：忽略 cooldown，保留受保护尾部并执行紧急截断。
2. 未达到 `compact_threshold`：不处理。
3. cooldown 尚未结束：不处理。
4. 存在可折叠的旧工具噪声：执行不调用 LLM 的 micro compact。
5. 没有可折叠内容且达到 `summary_threshold`：调用 LLM 生成 meta summary。

配置名称与 core 内部名称的映射为：

| App 配置 | Core policy 含义 |
|----------|------------------|
| `compact_threshold` | micro-fold 起点 |
| `summary_threshold` | LLM summary 起点 |
| `emergency_threshold` | hard truncate 起点 |

压缩完成后会根据实际 token reduction 设置分级 cooldown。连续低收益压缩会延长 cooldown，避免对不可压缩上下文反复工作。

## 手动命令

| 命令 | 当前行为 |
|------|----------|
| `/context` | 显示估算占用、窗口和压缩阈值，不修改消息 |
| `/compact` | 立即调用 `force_compact()`；改变时通过 `Conversation.rewrite()` 持久化 |
| `/clear` | 清空当前会话消息并清理当前 goal；保留 session 本身 |
| `/rewind` | 在 snapshot 可用时先恢复文件，再截断对应 turn 及其后的历史 |

`/rewind` 采用 fail-closed 顺序：文件恢复失败时不截断历史，避免“文件未恢复但对话已经忘记修改”的半完成状态。workspace snapshot 默认由 `snapshot_enabled` 控制，并不等同于 worktree isolation。

## 事件与可观测性

压缩通过 `compacting` 事件报告 kind、前后 token 数和消息数。当前 policy 发出的公开
事件 kind 是 `micro`、`truncate`、`overflow` 和 `overflow_truncate`。内部
`CompactOutcome` 还用 `no_reduction` 表示候选 rewrite 没有真实降低 token；archive
segment 用 `summary` 标记由 rolling-summary 路径移除的原始消息。这两个内部 kind
不伪装成一次成功的公开压缩事件。精确 payload 字段以
`src/autoverse/types/events.py` 为准。

trace 可以记录 request snapshot 和压缩事件，但不能触发或阻止压缩。WebUI/TUI 根据事件更新展示，刷新和 replay 也不能反向改写 Conversation。

## 失败语义

- 自动 summary 失败时，policy 可降级到受保护尾部截断，不能让上下文无限增长。
- provider 报 context overflow 时，recovery 可以强制压缩后重试；如果 token 没有真实下降，则耗尽恢复预算并停止重试。
- 压缩的 store rewrite 失败时，内存消息视图保持不变。
- 手动 compact 没有产生变化时，应报告前后 token 信息，而不是伪造已压缩结果。

## 开发入口

- `src/autoverse/core/state/conversation.py`
- `src/autoverse/core/context/compaction.py`
- `src/autoverse/core/context/config.py`
- `src/autoverse/core/context/split.py`
- `src/autoverse/core/loop/turn_loop.py`
- `src/autoverse/app/policy_map.py`
- `src/autoverse/app/engine.py`

配置字段见 [`config.md`](config.md)，状态归属见 [`../architecture/state-ownership.md`](../architecture/state-ownership.md)。
