# CLI 参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

本文按当前 `[project.scripts]`、`src/autoverse/frontends/cli/main.py`
与 `src/autoverse/frontends/web/server.py` 暴露的入口和参数编写。

## 入口点

| 命令 | pyproject target | 用途 |
|------|------------------|------|
| `autoverse` | `autoverse.frontends.cli.main:main` | 终端 CLI/TUI/Line REPL 入口 |
| `autoverse-web` | `autoverse.frontends.web.server:main` | WebUI server 入口 |

## `autoverse`

```bash
autoverse [prompt] [flags]
```

不传 `prompt` 时进入交互模式。传 `prompt` 时进入一次性执行；`--print` 是显式 one-shot 标记，常用于脚本。

### 示例

交互式启动：

```bash
autoverse
```

在指定目录启动：

```bash
autoverse --cwd /path/to/repo
```

一次性执行并退出：

```bash
autoverse --print "summarize this repository"
```

指定模型：

```bash
autoverse --model openrouter/deepseek-v4-pro
```

强制 line REPL：

```bash
autoverse --ui line
```

### 参数和 flags

| 参数 | 说明 | 示例 |
|------|------|------|
| `prompt` | 可选的一次性 prompt；不传则进入交互模式 | `autoverse "explain src"` |
| `--model <id>` | 启动模型。优先级高于配置；省略时使用配置模型，再回退到 `echo` | `--model deepseek/deepseek-v4-flash` |
| `--cwd <path>` | 工作目录 | `--cwd ../my-project` |
| `--resume <session-id>` | 恢复已有会话 ID | `--resume abc123` |
| `--print` | 一次性执行：打印事件渲染结果并退出 | `autoverse --print "fix lint"` |
| `--no-tui` | 兼容 flag：交互模式等价于 `--ui line`；一次性执行时禁用 ANSI 彩色输出 | `--no-tui` |
| `--ui <mode>` | 选择交互前端：`auto`、`tui`、`inline`、`line`、`plain` | `--ui inline` |
| `--debug-ui` | 把终端能力探测和选档结果打印到 stderr | `--debug-ui` |
| `--theme <name-or-path>` | 主题名称或 JSON 路径 | `--theme ./theme.json` |

交互 CLI/TUI 与 WebUI 共享同一个 `SessionCatalogPort` 装配：会话栏的 pinned、已查看时间和
projection 使用 data directory 下的 SQLite WAL catalog；`--resume` 的消息仍从配置的
`session_dir` Store 恢复，catalog 不是对话恢复来源。

### UI 模式

| 模式 | 说明 |
|------|------|
| `auto` | 自动探测终端能力，从推荐档开始并按 `tui` 全屏 → `inline` → `line` 的阶梯向下回退 |
| `tui` | Textual 全屏 TUI；不可用时视为错误 |
| `inline` | Textual 内联模式，不进入 alternate screen |
| `line` | append-only 行式 REPL |
| `plain` | one-shot only；交互模式下会提示需要提供 prompt 或使用 `--print` |

### 退出码

一次性执行时，`END_TURN`、`MAX_TURNS` 和 `PLAN_PROPOSED` 会返回 `0`；其他停止原因返回 `1`。

## `autoverse-web`

```bash
autoverse-web [flags]
```

启动 FastAPI WebUI server。普通界面走 HTTP + SSE；Apple App 的可见浏览器 host
使用同一 server 的 WebSocket route。

### 示例

默认启动：

```bash
autoverse-web
```

指定 host/port：

```bash
autoverse-web --host 0.0.0.0 --port 8170
```

指定工作区和模型：

```bash
autoverse-web --cwd /path/to/repo --model openrouter/deepseek-v4-pro
```

### flags

| 参数 | 说明 | 默认 |
|------|------|------|
| `--model <id>` | WebUI 会话默认模型；省略时使用配置模型，再回退到 `echo` | configured / `echo` |
| `--cwd <path>` | WebUI 默认工作目录 | 当前目录 |
| `--theme <name-or-path>` | 主题名称或 JSON 路径 | 默认主题 |
| `--host <host>` | 监听地址 | `127.0.0.1` |
| `--port <port>` | 监听端口 | `8170` |
| `--parent-pid <pid>` | sidecar 模式：父进程消失时退出 | unset |

## 常见问题

### `--model` 和配置模型谁优先？

命令行 `--model` 优先。没有传 `--model` 时读取配置里的 `model.model_id`；配置也没有时回退到 `echo`。

### `--print` 和交互模式有什么区别？

传入 `prompt` 就会跑一轮后退出；`--print` 是脚本友好的显式 one-shot 标记。交互模式会启动 TUI/Line REPL，并保留会话状态。

### `--no-tui` 和 `--ui line` 有什么区别？

`--ui line` 明确选择 line REPL；`--no-tui` 是禁用 TUI 的兼容 flag。新文档和脚本优先使用 `--ui`。

## 相关文档

- 终端使用：[`../guides/using-cli-and-tui.md`](../guides/using-cli-and-tui.md)
- WebUI 使用：[`../guides/using-webui.md`](../guides/using-webui.md)
- 配置参考：[`config.md`](config.md)

## 开发入口

- `src/autoverse/frontends/cli/main.py`
- `src/autoverse/frontends/web/server.py`
