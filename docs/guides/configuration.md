# 配置与密钥

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

AutoVerse 把配置、密钥和项目记忆分开处理。配置描述“本次会话怎么运行”，密钥提供 provider 鉴权，项目记忆给 agent 补充仓库上下文。

## 前置条件

- 明确本次要改的是全局用户配置，还是当前项目的 `.autoverse/` 配置。
- 如果要配置模型密钥，准备对应 provider 的 API key 或可写 vault。

## 配置负责什么

配置通常包含：

- 默认模型和模型参数。
- 上下文压缩、恢复、工作流等阈值。
- 权限默认模式。

配置最终会合并成不可变的 `Config` 树，写入 `AppContext.config`。core 读取这个快照，不直接读取磁盘或环境变量。

当前加载优先级从低到高是：内置默认值、全局 `~/.autoverse/config.json` 或 `config.toml`、向上查找的项目 `.autoverse/config.json` 或 `config.toml`、`AUTOVERSE_` 环境变量、CLI 覆盖。环境变量用双下划线表示嵌套字段，例如 `AUTOVERSE_MODEL__MODEL_ID`。

## 密钥负责什么

密钥不应该写进配置文件。模型配置只引用密钥别名，例如 `OPENROUTER_API_KEY`。实际取值由 `SecretProvider` 完成：

1. 环境变量优先。
2. WebUI session secret 作为临时会话层。
3. vault 作为持久化密钥库。
4. 找不到必需密钥时，报出面向人的错误。

WebUI 的设置面板可以暴露 session-scoped secret 和 vault 管理能力；命令行也可以直接使用环境变量。

## 最小环境变量示例

```bash
export OPENROUTER_API_KEY="..."
export DEEPSEEK_API_KEY="..."
```

具体需要哪些变量，取决于你启用的模型 transport。模型参考见 [`../reference/models.md`](../reference/models.md)。

## 配置加载原则

加载逻辑属于 app/infra 层；运行时消费属于 core/app 层：

- `app` 定义配置值类型。
- `infra` 负责从磁盘、环境变量或 vault 读取。
- `EngineBuilder` 把最终快照注入 `AppContext`。
- `core` 不 import 配置加载器。

## 修改配置时的建议

- 新增阈值时，放进对应的 Pydantic 子模型，并给默认值。
- 不要在 `core` 里散落魔法数字。
- 配置字段应该拒绝未知键，并给出清晰错误。
- 密钥只存别名，不存明文。

## 相关文档

- 配置字段参考：[`../reference/config.md`](../reference/config.md)
- 模型和鉴权：[`../reference/models.md`](../reference/models.md)
- MCP 配置：[`mcp.md`](mcp.md)
- Hooks 配置：[`hooks.md`](hooks.md)
- 设计背景：[`../design/llm-protocol.md`](../design/llm-protocol.md)
