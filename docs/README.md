# AutoVerse 文档

> 当前事实来源：被链接的目标文档和 `docs/DOCUMENTATION.md`。
> 维护规则：新增、移动或废弃文档时同步更新；不要复制字段/API 细节。

这套文档按“先使用，再参考，最后看设计”的顺序组织。大多数读者不需要先读设计记录。

文档维护标准见 [`DOCUMENTATION.md`](DOCUMENTATION.md)。

## 推荐阅读路径

第一次使用：

1. [`guides/getting-started.md`](guides/getting-started.md)
2. [`guides/configuration.md`](guides/configuration.md)
3. [`guides/using-cli-and-tui.md`](guides/using-cli-and-tui.md)
4. [`guides/using-webui.md`](guides/using-webui.md)
5. [`guides/common-workflows.md`](guides/common-workflows.md)
6. [`guides/troubleshooting.md`](guides/troubleshooting.md)

进阶使用：

- [`guides/mcp.md`](guides/mcp.md)：配置 MCP server。
- [`guides/skills.md`](guides/skills.md)：创建和使用 `SKILL.md`。
- [`guides/hooks.md`](guides/hooks.md)：编写 shell-command hooks。
- [`guides/project-memory.md`](guides/project-memory.md)：维护仓库级 agent 上下文。

日常查资料：

- [`reference/README.md`](reference/README.md)：当前行为、字段、命令、API 和开发入口索引。

贡献代码：

- [`guides/contributing.md`](guides/contributing.md)：从定位责任层到验证和文档影响判断的完整贡献路径。
- [`DOCUMENTATION.md`](DOCUMENTATION.md)：文档分层、模板和文档影响治理协议。
- [`templates/`](templates/)：新增或规范化文档时使用的模板。
- [`architecture/layers.md`](architecture/layers.md)：导入边界和各层职责。
- [`architecture/component-map.md`](architecture/component-map.md)：主要运行时组件、依赖方向和生命周期范围。
- [`architecture/state-ownership.md`](architecture/state-ownership.md)：会话、事件、任务、扩展和 Web replay 状态的唯一所有者。
- [`architecture/runtime-flow.md`](architecture/runtime-flow.md)：一次 turn 的运行路径。
- [`architecture/frontend-adapters.md`](architecture/frontend-adapters.md)：CLI、TUI、line REPL、WebUI、Apple 原生浏览器 host、Gateway 的关系。
- [`architecture/contracts.md`](architecture/contracts.md)：共享类型、端口和运行时装配的真相源。
- [`doc-impact.yml`](doc-impact.yml)：代码区域到文档责任的影响映射。

设计背景：

- [`design/README.md`](design/README.md)：设计记录索引。
- [`adr/README.md`](adr/README.md)：长期需要追溯论证过程的架构决策。
