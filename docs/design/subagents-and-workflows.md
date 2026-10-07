# 子代理与工作流设计

> 当前事实来源：`docs/reference/`、`docs/architecture/`、`src/autoverse/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

子代理和工作流解决同一个大问题：让模型完成更复杂的任务时有外部结构，而不是全靠一次 prompt 自觉。

## 背景

复杂任务既需要把工作交给独立执行节点，也需要判断整个流程何时可以停止。子代理解决“谁执行、能做什么”，workflow 解决“如何编排、按什么信号推进、何时结束”；把两者混成一个万能 runner 会使权限、隔离和终止条件无法独立演进。

## 关键取舍

### 子代理

子代理是受限角色，不是无限权限的第二个主 agent。每个角色应声明：

- 身份和输出格式。
- 可用工具能力上限。
- 模型 tier。
- 最大回合数。
- 是否需要隔离工作区。

真正的安全边界由 runner 的工具过滤、worktree 隔离和事后审计提供，prompt 只是让角色更容易合作。

### 前台和后台统一路径

前台子代理和后台 job 应共享 runner。前台只是父流程等待结果，后台只是把运行中的任务登记到 job 表，之后再 fan-in。

### 工作流

工作流把“完成”外化为机制：

- 固定阶段确保步骤顺序。
- StopGate 检查完成条件。
- Pipeline 把多个子任务编排成 DAG。
- 评分或机器校验比模型自称完成更可靠。

### StopGate 的同构骨架

StopGate 适合这种流程：

```text
模型结束一轮 -> 检查判据 -> 满足则停 -> 不满足则注入反馈继续
```

如果某个机制不符合这个骨架，就不要强塞进 StopGate。

### 设计目标

- 新增角色不改主循环。
- 新增 gate 不改 TurnLoop。
- 新增 workflow 不复制工具调度。
- 子代理失败也要返回可用产物或清晰失败原因。

### 隔离、能力和审计正交

能力过滤决定角色能看到哪些工具；worktree 隔离限制写入影响范围；事后审计验证结果是否满足角色约束。三者解决不同风险，不能只靠 prompt 或其中一个机制替代全部边界。

### 四维度定义 workflow

每个 workflow 以触发、编排、推进和终止四个正交维度描述。一个机制如果无法合理填入四维度，应先判断它是否真的是 workflow，而不是把新行为硬塞进 StopGate 或 Pipeline。

## 边界与非目标

- 子代理不是拥有更高权限的第二个主 agent；runner 的工具过滤和隔离才是安全边界。
- Workflow 不复制工具 dispatcher，不直接绕过 permission port。
- StopGate 只判定 stop/continue/need user，不执行工具或改写外部世界。
- 默认后台 job 不承诺跨进程恢复；前台和后台共享 runner，但等待方式和生命周期不同。
- 子代理原始 child event transcript 是展示派生数据，不是第二套持久会话协议。

## 开放设计问题

- 如果后台子代理需要跨进程恢复，必须先确定持久 job 的唯一真相源、恢复时的模型/工具快照以及孤儿进程清理语义；不能只把 `InMemoryJobStore` 换成文件 adapter。
- 如果工作流需要跨 session 暂停和恢复，必须明确 phase artifact、gate decision 和用户审批的持久化边界，并验证恢复后不会重复执行有副作用的阶段。

## 关联参考

- 子代理当前行为：[`../reference/subagents.md`](../reference/subagents.md)
- 工作流当前行为：[`../reference/workflows.md`](../reference/workflows.md)
- 状态所有权：[`../architecture/state-ownership.md`](../architecture/state-ownership.md)
- 四维度决策：[`../adr/0010-workflow-four-dimensions.md`](../adr/0010-workflow-four-dimensions.md)
