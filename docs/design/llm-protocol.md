# 模型与协议设计

> 当前事实来源：`docs/reference/`、`docs/architecture/`、`src/autoverse/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

AutoVerse 的 LLM 层围绕一个目标设计：core 只依赖 `LLMProvider`，协议方言、鉴权方式、端点、fallback 和 JSON 恢复都留在 infra 层。

## 背景

模型名称、wire 协议、传输端点和鉴权方式并不是同一个变化轴。把它们合成“每个后端一个 provider 类”会复制协议转换、重试和能力判断，并迫使 core 理解供应商细节。

## 关键取舍

### 为什么拆成 registry、codec、transport

模型后端通常有两个独立变化轴：

- wire 协议：OpenAI 风格、Anthropic 风格或其他方言。
- 传输鉴权：Bearer、无鉴权、本地 mock、平台定制鉴权。

如果按“每个后端一个 provider 类”实现，协议翻译会在多个鉴权变体里重复。拆成 codec 和 transport 后，同一 codec 可以配多个 transport，同一逻辑模型也可以声明多个 transport 作为切换候选。

### ModelRegistry 是单一真相源

模型能力、上下文窗口、别名、价格、fallback、可选 transport 都应从 registry 派生。picker、路由、usage 计费和 fallback 不应该维护第二份表。

### 能力位驱动行为

能力声明应该可执行：

- vision 不支持时，codec 明确拒绝图片。
- 工具 schema 是否出现当前由 ToolRegistry/TurnLoop 决定；如果以后接入不支持
  工具调用的后端，应先新增 registry 能力位，再让请求装配按能力 gate。
- 容易输出坏 JSON 的模型才挂 recovery。
- 模型别名只解析到规范 ID。

### Fallback 的分层

推荐把失败处理分层：

1. 同 transport 重试。
2. 同模型切换 transport。
3. 切换到 fallback model。

这样可以把短暂网络问题、协议通道问题和模型级不可用分开观测。
当前代码已有这三层框架；当前 registry 还没有声明多 transport 或
`fallback_model_id` 的规范模型，所以默认模型路径主要实际使用重试层。

## 边界与非目标

LLM 层不做工具执行、不做权限、不做对话循环、不做上下文压缩。它只把内部消息发给模型，再把响应转换回事件。

- Registry 声明能力和候选 transport，不根据单次网络状态修改产品策略。
- Codec 只负责内部消息与 wire format 的双向转换，不读取 vault 或选择 fallback。
- Transport 只负责端点、鉴权、超时、流读取和结构化错误；同 transport 重试由 fallback/retry 编排层负责。
- JSON recovery 只对声明 `degradation_prone` 的后端启用，不能成为诚实后端的默认文本扫描路径。
- 模型价格和 context window 不在 frontend、trace 或 picker 复制第二份表。

## 关联参考

- 当前模型、能力和鉴权：[`../reference/models.md`](../reference/models.md)
- 配置字段：[`../reference/config.md`](../reference/config.md)
- 上下文与压缩：[`../reference/context-and-compaction.md`](../reference/context-and-compaction.md)
- JSON recovery 决策：[`../adr/0009-json-recovery-capability-gated.md`](../adr/0009-json-recovery-capability-gated.md)
