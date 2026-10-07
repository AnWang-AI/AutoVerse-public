# 模型与协议参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

AutoVerse 通过 `LLMProvider` Port 调用模型。core 不知道具体 provider、HTTP 鉴权或 wire 协议，只消费统一的 `StreamEvent`。

## 三层结构

| 层 | 责任 |
|----|------|
| ModelRegistry | 声明逻辑模型、别名、能力、上下文窗口、transport 和 fallback |
| Codec | 内部 `Message` 与 provider wire format 的双向翻译 |
| Transport | HTTP 端点、鉴权、超时、SSE/流读取和结构化错误 |

同 transport 重试、同模型切换 transport、模型级 fallback 都在
`infra.llm.fallback.FallbackPlan` 里编排，不属于 Transport 职责。

这三层拆开后，一个协议 codec 可以被多个 transport 复用，一个逻辑模型也可以声明多个 transport 作为切换候选。

## 用户如何选择模型

模型的用户可见名称来自 registry。当前 registry 使用规范 ID；别名表为空，
`resolve_alias()` 对已知 ID 和未知输入都保持原值。未知模型名不会变成别名，
而是走 OpenRouter passthrough 规格。

```text
openrouter/deepseek-v4-pro
deepseek/deepseek-v4-flash
```

Picker 和本文表格只列出 `src/autoverse/infra/llm/registry.py` 当前 `_SPECS`
里的已登记模型。高级调用仍可输入未登记模型名；Router 会使用保守的
OpenRouter passthrough 规格。不要从设计记录里的目标模型列表推断当前已登记模型。

## Registry 模型

当前 registry 中的规范模型 ID 和 picker 元数据：

| 模型 ID | label | context window | picker | capabilities |
|---------|-------|----------------|--------|--------------|
| `openrouter/deepseek-v4-pro` | DeepSeek V4 Pro (OpenRouter) | 1000000 | yes | streaming, thinking, cache |
| `openrouter/deepseek-v4-flash` | DeepSeek V4 Flash (OpenRouter) | 1000000 | yes | streaming, thinking, cache |
| `deepseek/deepseek-v4-pro` | DeepSeek V4 Pro (Official) | 1000000 | yes | streaming, thinking, cache |
| `deepseek/deepseek-v4-flash` | DeepSeek V4 Flash (Official) | 1000000 | yes | streaming, thinking, cache |
| `openrouter/glm-5.2` | GLM 5.2 (OpenRouter) | 1000000 | yes | streaming, thinking |
| `opencode-go/glm-5.2` | GLM 5.2 (OpenCode Go) | 1000000 | yes | streaming, thinking |
| `volcengine-plan/glm-5.2` | GLM 5.2 (Volcengine Ark Plan) | 1024000 | yes | streaming, thinking |
| `openrouter/kimi-k2.7-code` | Kimi K2.7 Code | 262144 | yes | streaming, thinking, vision |
| `openrouter/mimo-v2.5-pro` | MiMo V2.5 Pro | 1000000 | yes | streaming, thinking |
| `openrouter/minimax-m3` | MiniMax M3 | 1000000 | yes | streaming, thinking, vision |
| `echo` | Echo (mock) | 200000 | no | streaming |

模型选择通常由配置、命令或子代理 tier 映射决定：

- 主 agent 使用默认模型配置。
- 子代理可以声明 `deep`、`balanced`、`fast` 等抽象 tier。
- `/model` 或配置热替换可以切换当前模型。

## 模型规格

模型规格通常包含：

- `model_id`：内部规范名。
- `aliases`：用户输入别名；当前所有规范模型都没有别名。
- `context_window`：上下文窗口。
- `capabilities`：如 streaming、thinking、cache、vision 等能力。
- `transports`：按偏好排序的传输通道。
- `fallback_model_id`：模型级降级目标。
- 计费字段：input、output、cache read/write 价格。

当前 registry 的规范模型均只有一个 transport，且 `fallback_model_id` 都是
`None`。fallback 和 protocol-switch 机制已在 `infra.llm.fallback` 中实现并测试；
只有当 registry 开始声明多 transport 或 fallback 目标时，默认模型路径才会实际
切换 transport 或切换模型。

`LLMProvider.count_tokens()` 的默认实现是无网络的保守估算，覆盖 system prompt、
消息、tool name/description/JSON schema 和图片名义成本。TurnLoop 用它计算完整请求
预算，并用 provider 返回的 input/cache usage 做会话内校准；它不是计费真相，计费
仍以 Usage 事件为准。

## 能力位

能力位应该驱动行为，而不是维护旁路名单。例子：

- 不支持 vision 的模型收到 `ImageBlock` 时，codec 应明确拒绝。
- 声明为易退化的模型才挂 JSON recovery。
- picker、路由和 fallback 都从 registry 派生。

常见能力：

| 能力 | 影响 |
|------|------|
| vision | 是否允许 `ImageBlock` 直传 |
| streaming | 是否真实流式返回 |
| thinking | 是否发送 thinking / reasoning 参数并解析 thinking delta |
| cache | 是否支持 cache control 或缓存计费 |
| degradation-prone | 是否启用 JSON/tool-call 恢复策略 |

工具 schema 注入当前不是 `ProviderCapabilities` 字段。TurnLoop 从工具注册表生成
`LLMRequest.tools`，codec 按请求内容编码；如果以后接入不支持工具调用的后端，
应先新增明确能力位和 contract test，再让请求装配按该能力 gate。

## 鉴权

鉴权由 transport/credential 层表达。常见形式：

- Bearer token。
- 无鉴权本地/mock endpoint。
- 定制平台鉴权可用组合 credential 表达；例如 Volcengine Ark Plan 传输同时发送
  `x-api-key: <ARK_API_KEY>` 和静态 `anthropic-version: 2023-06-01`。

运行时由 `CredentialResolver` 解析密钥 alias：先读同名环境变量，再读注入的
`SecretProvider`；两者都没有值时不写对应鉴权 header。密钥值不写入模型配置。

## 新增模型流程

1. 在 registry 里新增 `ModelSpec`。
2. 选择已有 codec，或实现新 wire codec。
3. 声明 transport、endpoint 和 credential。
4. 填上下文窗口、能力位、别名和价格。
5. 如有 fallback，声明 `fallback_model_id`。
6. 写 codec 单测和路由单测。
7. 更新本页，写明 picker 可见性、credential alias 和能力位。

## 常见问题

### 为什么同一个模型要有多个 transport？

如果能力、上下文窗口和价格都相同，只是送达路径或鉴权方式不同，就应该是同一个逻辑模型的多个 transport，而不是复制多个 model spec。

### 图片为什么被拒绝？

当前模型没有声明 vision，或 codec 不支持图片 wire format。切换到 vision 模型，或者先用 `read_image` 降级为文本。WebUI 对用户上传图片会在进入 engine 前做这一步；浏览器截图工具在 text-only 模型下也会在权限允许时自动委托 `read_image`，把视觉结果作为普通文本返回。

### 价格从哪里来？

usage 和 cost 应从 registry 的模型价格字段派生。不要在前端或 trace 里另写一份价格表。

## 开发入口

- `src/autoverse/infra/llm/registry.py`
- `src/autoverse/infra/llm/transport/credential.py`
- `src/autoverse/app/builder.py`

设计背景见 [`../design/llm-protocol.md`](../design/llm-protocol.md)。
