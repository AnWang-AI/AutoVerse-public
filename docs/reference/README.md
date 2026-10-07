# 参考手册

> 当前事实来源：`docs/reference/*.md` 和 `docs/DOCUMENTATION.md`。
> 维护规则：新增、拆分或废弃参考页时同步；不要复制具体字段/API 细节。

本目录放“查资料”用的稳定说明。它不记录设计推演过程，只回答当前仓库里各能力怎么命名、怎么接线、开发时该看哪里。

- [`commands.md`](commands.md)：斜杠命令、提示词命令和交互命令。
- [`cli.md`](cli.md)：`autoverse` 和 `autoverse-web` 的 flags、示例和退出码。
- [`config.md`](config.md)：配置、密钥和项目记忆。
- [`context-and-compaction.md`](context-and-compaction.md)：对话上下文、压缩、清空和 rewind 的当前边界。
- [`models.md`](models.md)：模型注册、协议、transport 和鉴权。
- [`tools.md`](tools.md)：工具协议、工具结果、权限和内置工具。
- [`subagents.md`](subagents.md)：子代理角色、能力分级、隔离和后台任务。
- [`workflows.md`](workflows.md)：工作流、StopGate 和 pipeline。
- [`events-and-storage.md`](events-and-storage.md)：事件、追踪、会话存储和回放。
- [`extensions.md`](extensions.md)：跨扩展边界、session-local 管理语义、Gateway adapter 边界。
- [`mcp.md`](mcp.md)：MCP server 配置、工具包装、资源工具和重连。
- [`skills.md`](skills.md)：`SKILL.md` v2、发现、匹配、prompt/工具投影和终端 skill slash 命令边界。
- [`hooks.md`](hooks.md)：hook 事件、shell 协议、超时和 fail-closed。
- [`webui-api.md`](webui-api.md)：WebUI REST + SSE endpoints。
- [`testing.md`](testing.md)：测试分层和常用验证命令。
