# ADR-0003：工作区快照独立于 Isolator，用影子 git 而非 per-file 内存存档

> 决策状态：已采纳（2026-06）
> 当前事实来源：相关 `docs/architecture/`、`docs/design/` 和代码实现。
> 维护规则：记录决策和后果；不要把 ADR 当作实施计划或当前 API 参考。

- **相关设计**：[工具参考](../reference/tools.md)、[运行路径](../architecture/runtime-flow.md)、[子代理与隔离参考](../reference/subagents.md)

## 背景（Context）

AutoVerse 是一个会**自主改文件**的 harness——agent 可能连改十几个文件后用户才发现方向错了，因此需要"撤销"安全网。已有两个相关机制：`Isolator`（git worktree 隔离）与原 `FileTracker.snapshot_before_write`（per-file 内存存档）。

问题有两层：① 既然已有 `Isolator` 的 worktree，为何不复用它做撤销？② per-file 内存存档更轻，为何要起一个影子 git 仓库这么重？且早先把"回滚 API"标记为 YAGNI 暂缓，现在做是否过早？

## 决策（Decision）

**把"撤销 agent 改动"建模为独立的 `SnapshotPort`，默认实现用影子 git 仓库（独立 gitdir，turn 级快照绑定会话消息），与 `Isolator` 并列正交；淘汰 per-file 内存存档。**

## 理由（Rationale）

- **正交（对①）**：`Isolator` 解决"把一组修改关进盒子，事后整体 review/merge/丢"；`SnapshotPort` 解决"主线工作区每个 turn 有回退点，可精确退到第 N 条消息"。强行用 worktree 实现撤销，会要求"任何会改文件的会话都先开 worktree"，违背"默认放行可逆操作、不打扰"，也把隔离的生命周期/合并复杂度强加给只想要撤销的场景。两个 git 用途不同，不是重复；同理，session persistence 与 retrieval memory 即使都落在 infra，也因为关注点不同而分开。
- **影子 git 优于 per-file 内存（对②）**：per-file 内存存档有三个硬伤——不覆盖 `bash` 写、不跨进程、会话结束即丢、且无法绑定历史。影子 git 一次性解决全部，边际复杂度由 `infra/snapshot/` 一个 adapter 收口，core 只见 `SnapshotPort`。
- **YAGNI 的正确理解**：YAGNI 是"不为臆想的需求买单"，不是"有证据也不做"。已有实际使用反馈 + 用户明确拍板，已构成需求证据，故从暂缓升级为采纳。

## 后果（Consequences）

- 撤销与隔离两个关注点各自演进、互不耦合；可"主工作区直接改 + 快照兜底"，也可"worktree 隔离里改"。
- 多一个 git 用途（影子仓库），但与用户项目 `.git` 完全隔离、不污染其 commit/branch。
- `/rewind` 因此可作为一等能力落地：文件侧 restore 和历史侧 rewrite 通过 snapshot 绑定保持一致。

## 可证伪边界（何时重新审视）

若实测中 `/rewind` 极少被使用、或影子 git 在大仓库上带来不可接受的 `track` 延迟（每 turn 都卡），则应降级——把快照改为**显式 opt-in**（仅用户开启或仅高风险操作前打快照），而非每 turn 自动。
