# 工作流参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

工作流是在普通 agent 循环之上增加的收敛机制。它回答“什么时候继续、什么时候停止、按什么阶段推进”。

## 核心概念

| 概念 | 说明 |
|------|------|
| StopGate | 在模型声称完成后检查是否真的可以停 |
| GateDecision | gate 的结构化判定：停止、继续或需要用户 |
| Pipeline | 多阶段或多子代理的编排 |
| WorkflowDefinition | 工作流的触发、阶段、终止条件和显示元数据 |

## 工作流四要素

定义一个工作流时，先写清楚：

1. 触发：用户命令、模型工具调用、自动条件还是生命周期事件。
2. 编排：单轮、固定阶段、动态循环还是多代理 DAG。
3. 推进信号：用户审批、工具事件、机器校验、评分还是 prompt-only。
4. 终止判据：测试通过、评分达标、外部裁判、阶段完成或资源耗尽。

示例：

| 工作流 | 触发 | 编排 | 推进信号 | 终止判据 |
|--------|------|------|----------|----------|
| plan | 用户命令 | 单轮或固定阶段 | 用户审批 | 用户接受计划 |
| fix | 用户命令 | 动态循环 | 测试/工具事件 | 机器可验证通过 |
| review | 用户命令或子代理 | 单轮/多代理 | reviewer 输出 | PASS/FAIL 结论 |
| goal | 用户命令 | 动态循环 | 外部裁判 | 裁判判定达成 |

## 内建 WorkflowRegistry definitions

当前 `WorkflowRegistry` 内建定义来自 `builtin_workflow_definitions()`：

| 工作流 | trigger | orchestration | advance | termination | 当前入口 |
|--------|---------|---------------|---------|-------------|----------|
| `goal` | `user_command` | `dynamic_loop` | `artifact_check` | `external_judge` | `/goal` |
| `fix` | `user_command` | `dynamic_loop` | `artifact_check` | `machine_verifiable` | `run_workflow`, `/workflows` |
| `dream` | `user_command` | `multi_agent_dag` | `artifact_check` | `fixed_phases_done` | `run_workflow`, `/workflows` |
| `distill` | `user_command` | `multi_agent_dag` | `score_threshold` | `fixed_phases_done` | `run_workflow`, `/workflows` |
| `commit` | `user_command` | `multi_agent_dag` | `artifact_check` | `fixed_phases_done` | `/commit` |
| `review` | `user_command` | `multi_agent_dag` | `score_threshold` | `score_threshold` | `/review` |
| `pr` | `user_command` | `multi_agent_dag` | `artifact_check` | `fixed_phases_done` | `/pr` |
| `init` | `user_command` | `multi_agent_dag` | `artifact_check` | `fixed_phases_done` | `/init` |
| `coordinate` | `user_command` | `multi_agent_dag` | `score_threshold` | `score_threshold` | `/coordinate` |

`/workflows` 会列出注册到 `WorkflowRegistry` 的 definitions；当前前端提供 DialogHost 选择器时，选择项会通过 `engine.run_workflow()` 执行。`fix`、`dream` 和 `distill` 已注册为 workflow definitions，但当前默认 slash command 表没有对应 `/fix`、`/dream` 或 `/distill` 命令。
可视化工作流管理不属于当前默认 UI 能力。

## StopGate 的边界

StopGate 只判断是否继续，不执行工具、不改状态、不做权限裁决。它可以向模型注入反馈消息，解释为什么还没完成以及下一步该做什么。

如果某机制需要回滚工具或重排历史，它不是 StopGate。

## GateDecision

GateDecision 应包含足够信息让系统可观测：

- 哪个 gate 做出决定。
- verdict 是 stop、continue 还是 need user。
- continue 时注入给模型的反馈。
- 给用户或 trace 的 reason。
- gate 自身错误和任务未达成要区分。

gate 自身失败时，不应该把“裁判挂了”当成“模型没做好”注入回模型。

## 新增工作流流程

1. 写清楚四要素。
2. 判断是否需要 StopGate、Pipeline，还是普通命令即可。
3. 如果需要子代理，先定义角色能力和隔离策略。
4. 给用户可见的状态、错误和停止原因。
5. 写测试覆盖完成、继续、失败和资源耗尽。
6. 更新命令参考或工作流参考。

## 常见问题

### 为什么不用 prompt 直接要求模型多做几轮？

prompt-only 没有机制兜底。对可验证任务，应该用测试、工具事件、gate 或用户审批把完成条件外化。

### StopGate 能不能执行工具？

不应该。StopGate 是判定器，不是执行器。执行工具会让“检查是否完成”和“改变世界”混在一起，难以追踪和恢复。

### 工作流和子代理是什么关系？

工作流是编排方法，子代理是执行节点。一个 workflow 可以调用多个子代理，也可以完全不使用子代理。

## 开发入口

- `src/autoverse/app/workflow/registry.py`
- `src/autoverse/core/workflow/stop_gate.py`
- `src/autoverse/app/workflow/gates.py`
- `src/autoverse/app/workflow/goal.py`
- `src/autoverse/app/workflow/pipelines.py`
- `src/autoverse/app/workflow/self_improve.py`
- `src/autoverse/core/loop/turn_loop.py`

Goal 状态以 `SessionMeta.extra["goal_state"]` 持久化。支持 turn rewind 的前端会在
提交前保存 checkpoint；回退时该 checkpoint 以替换语义恢复，因此被丢弃 turn
创建、完成或清除的 goal 不会泄漏到新的对话分支。

设计背景见 [`../design/subagents-and-workflows.md`](../design/subagents-and-workflows.md)。
