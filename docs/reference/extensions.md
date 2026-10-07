# 扩展边界参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

本页只记录跨扩展能力共享的边界。具体字段、命令、endpoint 和协议表分别维护在
[`skills.md`](skills.md)、[`mcp.md`](mcp.md)、[`hooks.md`](hooks.md)、
[`commands.md`](commands.md#skills-和-plugins) 和 [`webui-api.md`](webui-api.md#extensions)。

## 扩展类型

- Skill：可发现、可诊断、可投影到 prompt 和工具的工作流说明；终端前端还会把已加载 skill 注册为 prompt-template slash command。
- Plugin：可发现、可启停的能力包；当前 runtime manager 消费 tools、commands、hooks 和 skills。manifest parser 能识别 MCP servers/assets 声明，但默认 manager 还不会把它们装配成运行时能力。
- MCP：把外部 server 暴露成普通 tool 和 resource tool。
- Hook：在工具调用或生命周期事件前后观察或拦截。
- Gateway：把 IM、平台消息或其他外部入口转换成 engine 输入。

所有扩展都在 app/infra/frontends 层装配。core 只消费注册后的 tool、port、prompt
fragment 或 event，不导入扩展实现。

## Skill 与 plugin

Skill 是一个可复用工作流。Plugin 是分发和生命周期单元；其中的 skills 仍进入
`SkillRegistry`，由 skill 机制负责 prompt、工具和终端命令投影。独立 skill
不需要伪装成 plugin。

## 当前管理入口

TUI、Line REPL 和 WebUI 的内建 extension 管理命令共用 slash command
dispatch。`/skills` 和 `/plugin` 的完整语法只维护在
[`commands.md`](commands.md#skills-和-plugins)，本页只说明 extension 状态和生命周期边界。
WebUI extension endpoints 只维护在 [`webui-api.md`](webui-api.md#extensions)。
动态 skill prompt-template slash commands 当前只在 TUI 和 Line REPL 注册，WebUI
仍通过 `/skills`、extension endpoints 和已注册工具暴露 skill 能力。

当前 `enable` / `disable` / `trust` 是 `ExtensionAdminService` 持有的 session-local 状态：会改变 `/skills`、`/plugin` 和 WebUI extension 列表里的展示状态，但不持久化，不写配置或 trust store，也不会重新装配已经注册的 tool、skill、hook、MCP 或 plugin capability。

后续是否持久化管理状态、是否在安全点重建 extension graph，属于
[`../design/skills.md#开放设计问题`](../design/skills.md#开放设计问题)。

## TUI/WebUI 可见性

所有已加载 skills 和 plugins 都应出现在右侧栏的 `Extensions` 面板中。该面板是只读状态镜像，显示：

- `Skill` / `Plugin` 类型。
- display name。
- enabled/disabled/invalid 等状态。
- diagnostics count，例如 `!1`。

它不替代管理命令：当前启用、禁用、trust 和 doctor 仍走 `ExtensionAdminService`，并由 `/skills`、`/plugin` 或 WebUI endpoints 触发。这样 TUI、Line REPL、WebUI 不会各自维护一套 session-local extension 状态。

## Gateway 边界

Gateway 是 frontend adapter：`PlatformAdapter` 做平台翻译，`GatewayRunner` 编排
engine 调用、outbound 渲染、审批和重试。它只面向 `EngineHandle`、`EventSink`
和 `PermissionPort` 等公开接口，不直接调用 core 内部对象。

无人值守场景的权限默认应 fail-safe。公开 bot 的默认安全取舍见
[`../adr/0008-gateway-unattended-deny.md`](../adr/0008-gateway-unattended-deny.md)。

## 设计边界

- 扩展发现属于 infra/app/frontends 层。
- core 只见注册后的 tool、port、prompt fragment 或 event。
- frontends 只能使用 frontend-agnostic data service（例如 `ExtensionAdminService`），不能直接扫描 skill/plugin 目录来拼 UI 状态。
- Gateway 不重新实现 loop、权限或工具调度。

## 开发入口

- `src/autoverse/app/extension_admin.py`
- `src/autoverse/app/extensions.py`
- `src/autoverse/infra/skills/`
- `src/autoverse/infra/plugins/`
- `src/autoverse/frontends/gateway/`
- `src/autoverse/frontends/repl/builtin_cmds.py`
- `src/autoverse/frontends/repl/sidebar.py`
- `src/autoverse/frontends/web/session.py`
- `src/autoverse/frontends/web/server.py`
