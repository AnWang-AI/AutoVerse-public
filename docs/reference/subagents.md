# 子代理参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

子代理让主 agent 把一部分工作派发给受限角色。它们共享同一套工具协议、权限模型、隔离机制和事件流。

当前生产子代理使用独立的静态角色 prompt 加派发时提供的 task/context，不继承主 agent 的 `PromptAssembler.memory`。需要传入项目记忆或目录规则时，主 agent 应把相关内容显式放入派发 context。

## 子代理由什么组成

| 概念 | 说明 |
|------|------|
| AgentDefinition | 角色名称、prompt、模型 tier、工具能力上限、回合上限 |
| SubAgentRunner | 创建上下文、过滤工具、运行子会话、汇总结果 |
| AgentTool | 暴露给主 agent 的派发工具壳 |
| Job | 后台子代理任务，可 fan-in 汇总 |

## 什么时候使用子代理

适合使用子代理的场景：

- 大范围只读探索。
- 代码审查或安全审查。
- 隔离环境里的实现尝试。
- 多个互不依赖任务并行执行。
- 需要独立上下文避免污染主对话。

不适合使用子代理的场景：

- 一个简单文件查找。
- 需要主 agent 连续交互的小改动。
- 权限边界不清楚的高风险写入。
- 子代理输出无法验证的任务。

## 前台与后台

前台子代理会被父流程 await，结果直接回到当前 turn。后台子代理以 job 形式运行，父流程可以继续工作，之后再收集结果。

两者应该共享同一条执行路径，差别只在是否等待。
当前后台路径提供基础 fan-in、job 管理工具和 UI drill-down。后台续派和长期
job 持久化不属于默认能力。

取消后台 job 会等待其协程完成 cancellation cleanup 后才返回。这样需要恢复
工作区的上层流程可以把“job 已停止”作为恢复前的屏障，避免旧 job 在恢复后
继续写入文件。

## 子代理上下文 drill-down

子代理的原始 child event 不直接写入父会话 scrollback。生产 sink 会把 child
loop 中的文本、工具、turn、usage 等事件包装成 `subagent_child`，前端按 `job_id`
保存为每个子代理的独立 transcript。

当前默认前端消费路径：

- TUI：`/subagents` 和 `ctrl+r` 打开子代理选择器；点击 sidebar 或子代理卡片也会进入 `AgentView`。
- Line/headless：`/subagents` 走文本 fallback，列出已派发的子代理，不提供 in-place `AgentView`。
- WebUI：`GET /api/sessions/{sid}/agent/{job}` 返回对应 job 的上下文视图。

这个视图是展示派生状态，不是第二套会话真相源；长期 job 持久化仍不属于默认能力。

## 角色定义

一个子代理角色至少要说明：

- `id` / 名称。
- 角色身份和任务边界。
- 可用工具或最大能力等级。
- 是否允许网络。
- 模型 tier。
- 最大 turn 数。
- 输出格式。
- 是否需要隔离或审计。

安全不能只写在 prompt 里。prompt 负责合作体验，runner 的工具过滤和隔离负责真实边界。

当前内置角色以 `src/autoverse/core/subagent/definition.py` 为准：

| 角色 | 能力上限 | 网络 | 模型 tier | 最大 turn | 工具策略 | 隔离/审计 |
|------|----------|------|-----------|-----------|----------|-----------|
| `scout` | `READ_ONLY` | 否 | `FAST` | 30 | `read_file`, `glob`, `grep` | 无 |
| `architect` | `READ_ONLY` | 否 | `DEEP` | 60 | `read_file`, `glob`, `grep` | 无 |
| `builder` | `PROCESS` | 是 | `BALANCED` | 400 | 当前等级下非禁用工具 | 无 |
| `validator` | `PROCESS` | 是 | `FAST` | 30 | 当前等级下非禁用工具，bash check mode | 无 |
| `reviewer` | `PROCESS` | 是 | `BALANCED` | 50 | 当前等级下非禁用工具，bash check mode | 无 |
| `e2e-verifier` | `PROCESS` | 是 | `BALANCED` | 80 | 当前等级下非禁用工具 | 强制 worktree，`git_diff_must_be_empty`，bound process |
| `debugger` | `PROCESS` | 是 | `DEEP` | 150 | 当前等级下非禁用工具 | 无 |
| `researcher` | `READ_ONLY` | 是 | `BALANCED` | 50 | `read_file`, `glob`, `grep`, `web_fetch`, `web_search` | 省略项目记忆 |
| `generalist` | `PROCESS` | 是 | `BALANCED` | 60 | 当前等级下非禁用工具 | 无 |
| `checkpoint-writer` | `CONTROLLED_WRITE` | 否 | `FAST` | 20 | `read_file`, `glob`, `grep`, `write_file`, `edit_file` | 系统派发角色 |

## 隔离

高风险写入类子代理应使用 worktree 隔离。隔离层负责：

- 创建独立工作区。
- 约束文件系统影响范围。
- 任务完成后做事后审计。
- 清理进程和临时资源。

## 能力过滤

子代理不能靠 prompt 自觉遵守边界。运行前需要按 `CapabilityTier` 和工具属性过滤可用工具。只读角色拿不到写工具，写角色也不自动获得进程或网络能力。

可见性可以理解为：

```text
tool.capability <= agent.max_capability
and (not tool.needs_network or agent.can_network)
```

具体实现以当前 runner/filter 代码为准。

## 输出和验收

子代理输出应该可被父 agent 消费：

- reviewer 类角色给结论和证据。
- builder 类角色说明改了什么、测了什么。
- explorer 类角色返回文件路径和关键发现。
- verifier 类角色返回 PASS/FAIL 和失败原因。

对于写入类子代理，父流程应有事后审计或验证步骤，避免“子代理说完成了”直接当真。

## 常见问题

### 为什么子代理拿不到某个工具？

通常是角色能力等级不足，或者工具需要网络但角色未允许网络。

### 子代理失败是否等于任务失败？

不一定。子代理应尽量返回部分结果、失败原因和下一步建议。父 agent 再决定重试、降级或转人工。

### 为什么写入子代理需要隔离？

隔离可以降低并行任务冲突和错误写入主工作区的风险，也方便事后审计。

## 开发入口

- `src/autoverse/core/subagent/definition.py`
- `src/autoverse/core/subagent/filter.py`
- `src/autoverse/core/subagent/jobs.py`
- `src/autoverse/core/subagent/runner.py`
- `src/autoverse/core/subagent/audit.py`
- `src/autoverse/app/subagent.py`
- `src/autoverse/infra/isolation/worktree.py`
- `src/autoverse/frontends/repl/tui_sink.py`
- `src/autoverse/frontends/repl/agent_view.py`
- `src/autoverse/frontends/web/session.py`

设计背景见 [`../design/subagents-and-workflows.md`](../design/subagents-and-workflows.md)。
