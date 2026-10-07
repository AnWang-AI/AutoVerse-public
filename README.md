# AutoVerse

AutoVerse 是一个 Python 实现的 AI agent harness。它把对话循环、工具调用、子代理、工作流、持久化和多个前端拆成清晰的层，让使用者可以从命令行、TUI 或 WebUI 驱动同一个 agent 引擎。

这个仓库还在快速演进中。文档现在按读者拆分：先跑起来，再查配置和命令；需要贡献代码时再读架构；长篇设计记录放在最后。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,web]"
autoverse --help
```

启动交互式终端：

```bash
autoverse
```

启动 WebUI：

```bash
autoverse-web
```

`web` extra 同时安装 HTTP/SSE 与 WebSocket transport；Apple App 的可见浏览器依赖
后者连接原生 WKWebView host。

运行测试和架构检查：

```bash
pytest
mypy src/autoverse
import-linter lint
```

## 文档入口

- 新用户：从 [`docs/guides/getting-started.md`](docs/guides/getting-started.md) 开始。
- 遇到问题：查 [`docs/guides/troubleshooting.md`](docs/guides/troubleshooting.md)。
- 贡献代码：按 [`docs/guides/contributing.md`](docs/guides/contributing.md) 完成分层、测试和文档影响检查。
- 完整导航：看 [`docs/README.md`](docs/README.md)。
- 文档维护规则：看 [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md)。

## 仓库结构

```text
src/autoverse/
  types/       共享值类型
  ports/       core 依赖的协议接口
  core/        agent 循环、工具调度、上下文、子代理、工作流
  app/         配置、依赖注入和 EngineBuilder
  infra/       LLM、持久化、密钥、隔离等具体适配器
  frontends/   CLI、TUI/line REPL、WebUI、Gateway
tests/         单元、验收和架构边界测试
docs/          使用文档、参考手册、架构说明和设计记录
```

## 开发约束

AutoVerse 的核心边界由 `import-linter` 守住：

- `types` 不依赖任何内部层。
- `ports` 只依赖 `types`。
- `core` 不依赖 `infra`、`frontends` 或 `app`。

新增模块时优先沿用已有层次，不要让 UI、LLM 后端或存储实现泄漏进 `core`。
