# 快速开始

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

本页面面向第一次使用 AutoVerse 的人。目标是在 10 分钟内完成安装、启动终端前端、启动 WebUI，并知道下一步该读什么。

## 前置条件

- Python 3.11 或更高版本。
- 一个可以运行虚拟环境的 shell。
- 可选：模型 provider 的 API key，例如 OpenRouter 或 DeepSeek。
- 可选：浏览器，用于 WebUI。

如果你只是想验证仓库能跑，可以先不配置真实模型，使用帮助命令和测试命令确认环境。

## 1. 安装

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,web]"
```

这会安装运行时依赖、开发工具和 WebUI 依赖。只需要终端模式时，可以先装：

```bash
pip install -e ".[dev]"
```

安装成功后，下面的命令应该能打印帮助：

```bash
autoverse --help
autoverse-web --help
```

## 2. 启动

查看命令：

```bash
autoverse --help
```

进入默认交互式终端：

```bash
autoverse
```

你应该看到交互式输入界面。终端能力足够时会进入 Textual TUI；弱终端会降级到
line REPL。`plain` 是脚本友好的 one-shot 输出档，不作为交互降级目标。

启动 WebUI：

```bash
autoverse-web
```

WebUI 默认由 FastAPI + SSE 提供服务，前端静态资源随包发布。`web` extra 也会安装
Apple 原生浏览器 host 所需的 WebSocket transport。

如果 WebUI 缺依赖，确认安装时包含了 `web` extra：

```bash
pip install -e ".[web]"
```

## 3. 配置模型和密钥

AutoVerse 的模型配置和密钥是分开的：

- 配置里写模型 ID、阈值和工具、MCP、浏览器等运行选项。
- 密钥通过环境变量或 vault 读取，不写进配置文件。

最小方式是先设置环境变量，例如：

```bash
export OPENROUTER_API_KEY="..."
```

然后在配置中选择对应模型，或使用默认模型配置。更多配置见 [`configuration.md`](configuration.md) 和 [`../reference/models.md`](../reference/models.md)。

密钥不要写入仓库。优先用环境变量；需要持久保存时使用 AutoVerse 的 secret vault。

## 4. 验证开发环境

```bash
pytest
mypy src/autoverse
import-linter lint
```

这三条分别覆盖行为、类型和层次边界。修改 `core`、`ports`、`types` 或共享值类型时，三条都应该跑。

## 常见问题

### `autoverse` 命令不存在

确认已经激活虚拟环境，并且在仓库根目录执行过 editable install：

```bash
source .venv/bin/activate
pip install -e ".[dev,web]"
```

### TUI 显示异常

先切到 `--ui line`，确认不是模型或配置问题。终端前端说明见
[`using-cli-and-tui.md`](using-cli-and-tui.md)。

### 模型请求失败

常见原因是 API key 缺失、模型 ID 不存在、provider endpoint 不可达。先查 [`configuration.md`](configuration.md) 和 [`../reference/models.md`](../reference/models.md)。

### WebUI 启动失败

确认安装了 `web` extra，并检查端口是否被占用。WebUI 说明见 [`using-webui.md`](using-webui.md)。

更多排障见 [`troubleshooting.md`](troubleshooting.md)。

## 相关文档

- [配置与密钥](configuration.md)
- [使用 CLI、TUI 和 Line REPL](using-cli-and-tui.md)
- [使用 WebUI](using-webui.md)
- [排障指南](troubleshooting.md)
- [命令参考](../reference/commands.md)
- [分层与导入边界](../architecture/layers.md)
- [运行路径](../architecture/runtime-flow.md)
