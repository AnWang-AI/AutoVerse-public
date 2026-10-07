# 配置 MCP Server

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

本页面面向想把外部 MCP server 暴露给 AutoVerse 的用户。字段全集见 [`../reference/mcp.md`](../reference/mcp.md)。

## 前置条件

- 已安装 AutoVerse。
- MCP server 可以通过本机命令启动。
- 当前权限模式允许 `process` 或会在调用时向你确认。

## 1. 添加配置

在配置文件的 `mcp_servers` 下添加一个最小 stdio server。字段全集、默认值和保留字段说明见
[`../reference/mcp.md`](../reference/mcp.md) 与
[`../reference/config.md`](../reference/config.md#mcpserversname)。

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": "uvx",
      "args": ["mcp-server-filesystem", "/path/to/project"],
      "needs_network": false,
      "rpc_timeout": 30.0
    }
  }
}
```

当前生产路径使用 stdio client；今天要让 server 成功连接，请配置可启动的
`command` 和必要 `args`。只配置 HTTP URL 的边界以 reference 为准。

## 2. 启动 AutoVerse

```bash
autoverse
```

或启动 WebUI：

```bash
autoverse-web --cwd /path/to/project
```

启动时 `EngineBuilder` 会通过 `wire_extensions` 创建 `MCPManager`，连接配置里的 server，并把 server 暴露的工具注册为普通工具。

## 3. 查看 MCP 工具

在交互界面里使用：

```text
/mcp
```

这个命令会显示 MCP server 状态以及当前已注册的 MCP 工具名；成功连接后，MCP
工具会进入同一套工具注册表，并像内置工具一样经过 schema 校验、权限检查和调度。
工具命名规则和状态值见 [`../reference/mcp.md`](../reference/mcp.md)。

## 4. 读取 MCP 资源

当至少一个 MCP server 已连接时，你可以直接让 agent 查看相关 resources：

```text
列出当前连接的 MCP resources，并读取和这个任务最相关的资源。
```

## 权限建议

不要把外部 server 默认当成本地只读能力。未知 server 应按保守权限处理；如果
server 只做本地只读查询，再在配置中显式降低能力等级。

示例：

```json
{
  "mcp_servers": {
    "docs": {
      "command": "python",
      "args": ["-m", "my_docs_mcp"],
      "capability": "read_only",
      "needs_network": false
    }
  }
}
```

## 常见失败

### `/mcp` 看不到工具

检查 server 命令是否能独立启动，配置名是否唯一，`/mcp` 中的 server 状态是否为
`connected`，以及启动日志里是否有初始化失败。连接失败的 server 会进入重连状态，
不会阻止其他 server 工作。

### 工具调用被权限拦截

这是预期安全行为。MCP server 默认是外部进程能力，除非你明确声明更低能力等级。

### 工具 schema 不符合预期

AutoVerse 从 MCP server 的 JSON Schema 构建宽松 Pydantic 参数模型。若字段缺失或 required 不正确，先修 MCP server 暴露的 schema。


## 相关文档

- [MCP 参考](../reference/mcp.md)
- [配置参考](../reference/config.md)
- [工具参考](../reference/tools.md)
- [扩展边界参考](../reference/extensions.md)
