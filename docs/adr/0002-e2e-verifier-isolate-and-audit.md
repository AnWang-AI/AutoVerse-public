# ADR-0002：e2e-verifier 靠"隔离 + 事后审计"约束，而非事前剥夺写/执行能力

> 决策状态：已采纳（2026-06）
> 当前事实来源：相关 `docs/architecture/`、`docs/design/` 和代码实现。
> 维护规则：记录决策和后果；不要把 ADR 当作实施计划或当前 API 参考。

- **相关设计**：[子代理参考](../reference/subagents.md)、[子代理与工作流设计](../design/subagents-and-workflows.md)

## 背景（Context）

e2e-verifier 的职责是验证系统**真能端到端跑起来**——这本质上需要写配置、装依赖、起进程（`max_tier=PROCESS`，无 check_mode）。但它**不应改动被跟踪的源码**（否则验证就成了"边改边验"的自欺）。

如何强制"不改源码"有两条路：
- **事前剥夺**：像 validator 那样上 `bash_check_mode`，直接拿掉写工具。
- **事后审计**：放它真写，结束后用 `git diff --name-only HEAD --` 检查被跟踪源文件是否被改，改了则判 `tainted`、verdict 作废。

## 决策（Decision）

**采用"worktree 强制隔离 + 事后 `git_diff_must_be_empty` 审计"，而非事前剥夺写/执行能力。** 审计只看被 git 跟踪的源文件，未跟踪副产物（日志/缓存/`__pycache__`）容忍；审计必须在 `finalize`(autocommit) **之前**执行。

## 理由（Rationale）

- 剥夺写/执行能力等于让 e2e-verifier 无法完成本职（起服务、装依赖都需要写与执行）。
- 真正的约束点不是"能不能写"，而是"**改的是不是被跟踪的源码**"——这个语义事前能力剥夺表达不了。
- 事后审计能精确区分"源文件改动（taint）"与"未跟踪副产物（容忍）"，避免误杀。
- 隔离（worktree，见 [ADR-0001](0001-l3-default-shared.md) 和 [子代理参考](../reference/subagents.md) 的 fail-closed 隔离规则）保证即使它乱写也只污染分支、不漏出主工作区，把"写权限"的风险关进盒子。

## 后果（Consequences）

- e2e-verifier 能真实地端到端验证，且改源码会被机制检出（`[VERIFICATION_TAINTED]`、verdict 失效）。
- 代价是多一道审计时序约束（必须先审后提）与对"跟踪文件"语义的依赖。

## 可证伪边界（何时重新审视）

若某类验证任务确实**零写入**即可完成，应为它单独建一个 `bash_check_mode` 只读角色，而非把 e2e-verifier 改成只读；若审计的二元粒度（改源码即作废）频繁误伤合法的"标注文件改动"，再扩展为细粒度 policy。
