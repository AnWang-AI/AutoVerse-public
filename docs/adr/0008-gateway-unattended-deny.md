# ADR-0008：IM 网关无人值守审批默认 deny（fail-safe），而非 allow

> 决策状态：已采纳（2026-06）
> 当前事实来源：相关 `docs/architecture/`、`docs/design/` 和代码实现。
> 维护规则：记录决策和后果；不要把 ADR 当作实施计划或当前 API 参考。

- **相关设计**：[扩展边界参考](../reference/extensions.md)、[前端 Adapter](../architecture/frontend-adapters.md)

## 背景（Context）

IM 网关把 bot 暴露给公开/半公开的聊天平台。当某次工具调用需要审批（`PermissionPort` 给出 `ask`）而**无人值守**或审批超时时，默认动作该是 `deny` 还是 `allow`？默认 `permission_mode` 该是 `deny_all` 还是 `allow_all`？

直觉上 `deny_all` 让开箱即用的 bot 几乎什么都干不了，体验差；无人值守下 ask 超时直接 deny 会让长任务半途夭折。

## 决策（Decision）

**公开 bot 默认 `permission_mode: deny_all`；`ask` 超时无人响应按 `deny` 裁决（fail-safe），并告知"审批超时，已拒绝"。** 只读/网络类显式 allow、受控写按路径白名单 allow、任意写/进程类默认 ask 或 deny。`deny_all` 是默认而非强制——受信部署可显式配 `allow_all` 或放宽规则。

## 理由（Rationale）

- **不对称风险**：一次误 allow 的任意写/进程类工具在公开 bot 上的代价（数据损毁、越权）远高于一次误 deny 的代价（用户重试）。fail-safe 要求在"不确定"时倒向安全侧。
- 体验问题用 prompt 反馈缓解：把"本会话写操作受限/需审批"写进 `system_prompt_append`，让模型主动避开而非反复触发审批疲劳；只读/网络类默认放行保证基本可用。
- 安全姿态可由运维**知情下调**（显式配 `allow_all`），但不可隐式上调——默认值守住最坏情况。

## 后果（Consequences）

- 公开部署默认安全：未显式放行的危险工具不会在无人审批时执行。
- 首次试用可能撞到拒绝，需运维显式配置放行规则——这是知情的安全成本。
- 长任务在纯无人值守下若依赖 ask 类工具会被 deny 中断，应通过预配 allow 规则或保持有人审批解决。

## 可证伪边界（何时重新审视）

若某受信内网部署场景中"误 allow 的代价"被证明极低（如全只读 bot、或强隔离沙箱内运行），可在该部署显式配 `allow_all`；但全局**默认**仍应是 `deny_all`。若线上数据显示默认 deny 导致绝大多数合法操作被误拒且无安全收益，才考虑翻转默认。
