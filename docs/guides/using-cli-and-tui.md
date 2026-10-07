# 使用 CLI、TUI 和 Line REPL

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

AutoVerse 的终端前端有三种形态：一次性 CLI、全屏/内联 TUI、行式 REPL。它们消费同一个 `EngineBuilder` 组装出的引擎，只是显示能力不同。

## 前置条件

- 能运行 `autoverse --help`。
- 在目标项目目录启动，或显式传 `--cwd`。

## 启动方式

交互模式：

```bash
autoverse
```

一次性执行：

```bash
autoverse --print "解释这个仓库的结构"
```

查看参数：

```bash
autoverse --help
```

## 前端档位

AutoVerse 会根据终端能力选择最合适的交互形态：

- `tui`：Textual 全屏 TUI，支持可重绘区域、侧栏、折叠卡和快捷键。
- `inline`：Textual 内联形态，保留 scrollback，适合不想进入 alternate buffer 的终端。
- `line`：纯行式 REPL，适合弱终端、CI 或不支持 Textual 的场景。
- `plain`：one-shot only；交互模式下会提示需要提供 prompt 或使用 `--print`。

默认自动选择。需要强制指定时，用命令行参数或配置项，以 `autoverse --help` 为准。

## 常见操作

- 输入普通文本会提交一轮对话。
- 输入 `/` 开头的内容会走斜杠命令系统。
- 工具调用、diff、todo 和子代理状态会以终端卡片展示。
- 权限审批、计划审批和多选问答由前端 adapter 接回 core。

斜杠命令参考见 [`../reference/commands.md`](../reference/commands.md)。

## Skills、plugins 和右侧栏

全屏 TUI 在宽终端中会显示右侧栏。右侧栏是当前 session 的只读状态镜像，包含
Context、Directory、Memory、Todo、Changed、Agents 等面板；当有 extensions 被
加载时，还会显示 `Extensions` 面板。

切换到已有 session 时，TUI 会从新格式的 assistant usage metadata 和 tool result metadata
重建 Context、费用/cache、Todo 与 Changed，而不只重画正文；恢复后的 trace meter 也包含
重启前的累计 usage。模型没有 registry 价格时费用显示 `cost unavailable`，不会把未知价格
写成 `$0.0000`。

该面板只用于确认当前会话加载了哪些 skills/plugins。管理动作仍通过 `/skills`
和 `/plugin` 命令完成；完整语法见 [`../reference/commands.md`](../reference/commands.md#skills-和-plugins)，
状态边界见 [`../reference/extensions.md`](../reference/extensions.md)。

## TUI 与 Line REPL 的区别

TUI 能原地更新流式文本、折叠工具卡、显示侧栏并支持更丰富的交互。Line REPL 是 append-only 输出，内容尽量复用同一套渲染器，但不会提供原地展开和常驻面板。

Line REPL 没有常驻右侧栏，但 `/skills`、`/plugin` 等命令语义与 TUI 相同；
两者都走同一个 command registry。

如果你在远程终端、窄窗口或 `TERM=dumb` 环境里遇到显示问题，交互模式优先切到
`line`；脚本或一次性执行才使用 `plain`。

## 相关文档

- [CLI 参考](../reference/cli.md)
- [命令参考](../reference/commands.md)
- [前端 Adapter 架构](../architecture/frontend-adapters.md)
- [前端设计](../design/frontend.md)
