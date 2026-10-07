# ADR-0009：json_recovery 做成仅 degradation_prone 后端启用的中间件，而非默认路径

> 决策状态：已采纳（2026-06）
> 当前事实来源：相关 `docs/architecture/`、`docs/design/` 和代码实现。
> 维护规则：记录决策和后果；不要把 ADR 当作实施计划或当前 API 参考。

- **相关设计**：[模型与协议参考](../reference/models.md)、[模型与协议设计](../design/llm-protocol.md)

## 背景（Context）

部分 LLM 后端会把工具调用当作裸文本吐出（格式退化）。AutoVerse 提供一套启发式恢复（`RecoveryMiddleware`）把这些被当文本吐出的 `tool_use` JSON 救回、补发为 `TOOL_START` 事件。问题是：这套恢复应对**所有后端默认开启**，还是仅对声明 `capabilities.degradation_prone == True` 的后端挂载？

一种直觉是"多一层防护没坏处，反正诚实后端不会触发那些启发式分支，默认开更省心"。

## 决策（Decision）

**json_recovery 是仅当 `capabilities.degradation_prone == True` 才挂载的可选中间件，包在事件流外侧；诚实后端的事件流一字节都不经过它。** 行为由能力位推导，不维护"哪些后端要特判"的旁路名单。

## 理由（Rationale）

- 这套恢复（brace-counting 扫描、多策略宽松 JSON 解析、单引号 dict 兜底、回声去重）对正常后端的**合法文本是纯风险**：可能把模型正文里引用的 JSON 误判为工具调用而错误补发执行。
- 每条流都付扫描开销——对诚实后端是无收益的恒定成本。
- 能力位驱动让行为从声明推导，而非维护易漂移的旁路名单；协议和退化特性归在 infra 的能力声明里。
- 退化恢复因此可单独开关、单独单测、单独演进，与核心解析路径解耦。

## 后果（Consequences）

- 诚实后端：零开销、零误判。
- 退化后端：显式声明 `degradation_prone=True` 即获恢复能力；退役时手改 registry 置回 `False`，不做运行时自动探测。
- 多一个能力位需要在接入新后端时诚实填写。

## 可证伪边界（何时重新审视）

若退化恢复演进到对合法文本**零误判**、且扫描开销可忽略，则"默认开"的反对意见成立，可考虑把它升为对所有后端透明的观察者。在此之前，能力位门控是必要的安全边界。
