# MCP 参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

MCP server 是扩展源。它们在 app/infra 层启动，暴露的工具会被包装成 core `Tool`。

## 配置字段

`mcp_servers` 是按 server name 索引的对象：

```json
{
  "mcp_servers": {
    "docs": {
      "command": "python",
      "args": ["-m", "docs_mcp"],
      "env": {"DOCS_ROOT": "/path/to/docs"},
      "cwd": "/path/to/project",
      "capability": "read_only",
      "needs_network": false,
      "rpc_timeout": 30.0
    }
  }
}
```

字段全集、默认值和配置层保留说明只维护在
[`config.md`](config.md#mcpserversname) 的 `mcp_servers.<name>` 小节。

约束：`command` 和 `url` 至少设置一个。当前真实 client 支持 `command` stdio；`args` 会追加到 command 字符串后，再由 stdio client 解析成 argv。只配置 `url` 能通过配置校验，但默认 client factory 会因为缺少 `command` 而连接失败，直到 HTTP transport 落地。

## 工具命名

MCP 工具包装名：

```text
mcp__<server>__<tool>
```

这个格式由 `mcp_tool_name()` 生成，目的是和常见模型工具命名习惯兼容。

## 能力等级

如果 server 没有显式 `capability`，工具默认声明为：

```text
CapabilityTier.PROCESS
```

并继承 `needs_network`。MCP 层只声明能力，不直接决定权限；是否允许调用仍由权限系统处理。

## 生命周期

1. `MCPManager.setup()` 并行连接所有配置 server。
2. 单个 server 初始化失败不会阻止其他 server。
3. 失败 server 标记为 `failed`，并启动指数退避重连。
4. 重连成功后，manager 更新工具列表并递增 `tool_list_changed_count`。
5. `teardown()` 取消重连任务并关闭 connected clients。

状态值：

| 状态 | 说明 |
|------|------|
| `connected` | 初始化和工具列表读取成功 |
| `failed` | 当前不可用 |
| `reconnecting` | 正在等待或尝试重连 |

## 资源工具

当存在 connected client 时，manager 可提供：

| 工具 | 能力 | 说明 |
|------|------|------|
| `list_mcp_resources` | `read_only` | 列出 MCP resources |
| `read_mcp_resource` | `read_only` | 按 URI 读取 resource |

## 错误语义

- MCP 工具调用抛出 `MCPError` 时返回 `ToolResult.error(..., error_kind="mcp_error")`。
- resource 不存在时，`read_mcp_resource` 返回 `mcp_error`。
- 老 client 缺少 resource 方法时，按没有 resources 处理。

## 开发入口

- `src/autoverse/infra/mcp/manager.py`
- `src/autoverse/infra/mcp/session.py`
- `src/autoverse/infra/mcp/transport.py`
- `src/autoverse/app/extensions.py`

相关扩展边界见 [`extensions.md`](extensions.md)。
