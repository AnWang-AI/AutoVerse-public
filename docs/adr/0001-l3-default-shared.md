# ADR-0001：L3 会话工作区默认 SHARED，worktree 隔离 opt-in

> 决策状态：已采纳（2026-06）
> 当前事实来源：相关 `docs/architecture/`、`docs/design/` 和代码实现。
> 维护规则：记录决策和后果；不要把 ADR 当作实施计划或当前 API 参考。

- **相关设计**：[子代理与隔离参考](../reference/subagents.md)、[子代理与工作流设计](../design/subagents-and-workflows.md)

## 背景（Context）

L3（会话级工作区隔离）让多个会话可以并发修改同一项目而互不踩踏，机制是每会话一个 git worktree + 分支。问题是：`WorkspaceMode` 的**默认值**该是 `SHARED`（所有会话共用 cwd，即现状），还是默认隔离（每会话自动开 worktree）？

并发安全很重要，一种直觉是"让安全成为默认"，即默认隔离。

## 决策（Decision）

**默认 `SHARED`，会话级 worktree 需通过 `WorkspaceSpec(mode=SESSION_WORKTREE)` 显式开启。**

## 理由（Rationale）

- 默认隔离会给单会话 / 只读 / 简单场景平添 git worktree 的复杂度与依赖重装开销。
- 默认隔离会破坏现有 WebUI 和外部调用方的"每会话传 cwd"行为（向后兼容）。
- "安全默认"的正确含义是"**危险操作默认拦截**"——这已由权限层负责，而非"所有场景默认上重型隔离"。
- 并发改同一项目是**特定场景**，对它 opt-in 恰当；让多数不需要隔离的场景为少数场景买单不合理。

## 后果（Consequences）

- 单会话 / 现有 WebUI 调用零改动、零额外开销。
- 需要并发隔离的部署必须显式声明 `SESSION_WORKTREE`，否则退回共享 cwd 的运行期踩踏风险——这是用户的明确选择，不是静默默认。

## 可证伪边界（何时重新审视）

若实践中"多会话并发改同项目"成为绝对主流，且共享模式频繁出事，则应翻转默认为隔离。
