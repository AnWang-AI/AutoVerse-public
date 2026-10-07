# 排障指南

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

本页收集使用 AutoVerse 时最常见的问题。先从症状定位，再跳到对应 reference 或架构页。

## 前置条件

- 能运行 `autoverse --help` 或 `autoverse-web --help`。
- 保留最近一次命令、错误输出、当前 cwd，以及使用的是 TUI、Line REPL、
  `plain`/`--print` one-shot 还是 WebUI。

## 安装问题

### `autoverse: command not found`

通常是虚拟环境未激活，或没有执行 editable install。

```bash
source .venv/bin/activate
pip install -e ".[dev,web]"
autoverse --help
```

如果仍然找不到，确认 `pip` 和 `python` 来自同一个虚拟环境：

```bash
which python
which pip
which autoverse
```

### WebUI 相关 import 错误

WebUI 依赖在 `web` extra 中。当前 `pyproject.toml` 的 `web` extra 覆盖
FastAPI、带 WebSocket transport 的 Uvicorn 和 `cryptography`：

```bash
pip install -e ".[web]"
```

开发环境推荐：

```bash
pip install -e ".[dev,web]"
```

如果 Apple App 的 Browser 分栏持续显示 `Browser host offline`，同时 server log 出现
`Unsupported upgrade request` 或 `No supported WebSocket library detected`，说明环境仍是
旧的基础 Uvicorn 安装；重新执行上面的 `web` extra 安装并重启 App。

## 终端显示问题

### TUI 乱码、布局错位或启动失败

AutoVerse 有多级终端前端：`tui`、`inline`、`line`、`plain`。遇到弱终端问题时，
交互模式先切到 `--ui line`，确认 agent 本身可用；`--ui plain` 是 one-shot only，
需要同时提供 prompt 或使用 `--print`。

常见原因：

- `TERM=dumb`。
- 终端过窄或过矮。
- SSH/tmux 环境能力检测不准。
- 本地终端不支持需要的 ANSI 特性。

终端前端说明见 [`using-cli-and-tui.md`](using-cli-and-tui.md)。

## 模型和密钥问题

### 401、403 或鉴权失败

检查：

- API key 环境变量是否存在。
- 配置中的 key alias 是否和环境变量一致。
- 当前 model 是否使用了对应 provider。
- provider 是否要求额外 header、account 或 endpoint。

相关文档：

- [`configuration.md`](configuration.md)
- [`../reference/models.md`](../reference/models.md)

### 模型不支持图片

vision 能力由模型能力位决定。非 vision 模型收到原始 `ImageBlock` 时，codec
层应该明确拒绝。WebUI 上传图片会先尝试用 `read_image` 做文字降级；浏览器截图
在 text-only 模型下也会在权限允许时自动委托 `read_image` 生成文字描述。

如果你希望图片直传，切换到声明支持 vision 的模型。若降级失败，检查
`read_image` 是否已注册、权限是否允许，以及 `OPENROUTER_API_KEY` 是否可用。

## 工具和权限问题

### 工具被拒绝

常见原因：

- 当前处于 plan 模式，写工具被拦截。
- 子代理角色能力上限不足。
- 工具需要网络，但当前角色或策略不允许网络。
- shell 命令被权限策略判定为高风险。

参考：

- [`../reference/tools.md`](../reference/tools.md)
- [`../reference/subagents.md`](../reference/subagents.md)

### 文件写入失败

检查：

- 相对路径是否逃逸当前 cwd；这类逃逸会被 `workspace_escape` 拒绝。
- 是否处于 worktree 隔离会话；隔离模式会阻止访问隔离目录之外的路径。
- 写入或编辑已有文件前，是否已经在当前 session 读取过该文件；否则会触发 read-before-write / stale-read 拦截。
- 是否有权限审批被拒绝。

### `browser_evaluate` 返回 `unsupported_result`

异步页面脚本应使用顶层 `return await ...` 函数体，例如：

```javascript
return await fetch(url).then(response => response.json())
```

不要直接返回 `fetch(...).then(...)`、async IIFE、function 或 DOM node；最终值必须是 JSON
兼容的有限数字、字符串、boolean、`null`、数组或对象。不要依赖“先写 `window`、再立即
读取”的两次调用碰运气，因为异步回调和第二次读取之间没有完成顺序保证。未决异步脚本会
返回 `browser_timeout`；之后仍可继续浏览器操作。

## WebUI 问题

### 页面能打开但没有流式输出

可能是 SSE 连接失败。检查浏览器开发者工具的 Network 面板，确认
`/api/sessions/{sid}/stream` 请求未被代理、浏览器策略或反向代理缓冲阻断。

### 会话历史不一致

WebUI 展示依赖后端 session、bridge 和 store。刷新后应通过 replay 恢复。若 replay 缺块，优先检查持久化和事件日志。

参考：

- [`using-webui.md`](using-webui.md)
- [`../reference/events-and-storage.md`](../reference/events-and-storage.md)

## 开发检查失败

### `import-linter` 失败

说明层次边界被打破。常见问题是：

- `core` import 了 `infra`、`frontends` 或 `app`。
- `ports` import 了 `core` 或具体实现。
- `types` import 了内部模块。

先读 [`../architecture/layers.md`](../architecture/layers.md)，再调整依赖方向。

### `mypy` 失败

项目使用 strict mypy。优先修类型定义，不要用宽泛 `Any` 绕过共享契约。

### 测试挂起

工具、shell、WebUI、子代理相关测试容易因为后台任务未收敛而挂起。检查 abort signal、timeout、job cleanup 和 async fixture。

## 仍然无法定位

收集这些信息再排查：

- 运行命令和完整错误。
- 当前模型 ID。
- 是否使用 WebUI、TUI、Line REPL，还是 `plain`/`--print` one-shot。
- 是否在 worktree 隔离会话中。
- 最近一次工具调用和权限裁决。
- `pytest`、`mypy`、`import-linter` 哪个失败。

## 相关文档

- [快速开始](getting-started.md)
- [配置与密钥](configuration.md)
- [CLI 参考](../reference/cli.md)
- [WebUI API 参考](../reference/webui-api.md)
- [测试与验收参考](../reference/testing.md)
