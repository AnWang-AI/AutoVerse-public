# 贡献代码

> 当前事实来源：`AGENTS.md`、`pyproject.toml`、`src/autoverse/`、`tests/` 和 `docs/DOCUMENTATION.md`。
> 维护规则：分层、验证命令、文档影响流程或贡献入口变化时必须同步。

本页给出从定位责任层到完成验证的最短贡献路径。具体命令、字段和架构事实仍以链接的 reference/architecture 为准。

## 目标

完成一个边界清楚、测试充分、文档影响已处理的代码改动，同时不破坏 AutoVerse 的依赖方向和单一真相源。

## 前置条件

- 已按 [`getting-started.md`](getting-started.md) 安装开发依赖。
- 已阅读仓库根目录 `AGENTS.md`。
- 能运行 `pytest`、`mypy` 和 `import-linter`。

## 1. 确定责任层

| 变化 | 首选位置 | 判断依据 |
|------|----------|----------|
| 跨层共享值类型、事件 payload、枚举 | `src/autoverse/types/` | 不包含 IO 和实现依赖 |
| core 需要的外部能力协议 | `src/autoverse/ports/` | 窄、可注入、只依赖 types |
| 对话循环、上下文、工具调度、子代理、gate | `src/autoverse/core/` | 稳定行为骨架，不知道具体 adapter |
| provider、存储、密钥、hook、MCP、隔离实现 | `src/autoverse/infra/` | 第三方和系统资源 adapter |
| 配置快照和运行时装配 | `src/autoverse/app/` | 可以同时知道 core 和 infra |
| CLI、TUI、WebUI、Gateway 交互 | `src/autoverse/frontends/` | 只采集输入、消费事件和处理交互 |

拿不准时先读 [`../architecture/component-map.md`](../architecture/component-map.md) 和 [`../architecture/state-ownership.md`](../architecture/state-ownership.md)。不要通过在 core 中导入 infra 来缩短装配路径。

## 2. 找到单一真相源

修改可枚举行为前先定位 registry 或模型：

- 配置：`src/autoverse/app/config.py`；
- 模型：`src/autoverse/infra/llm/registry.py`；
- 命令：`src/autoverse/frontends/repl/builtin_cmds.py`；
- 工具：`src/autoverse/core/tools/registry.py`；
- 事件：`src/autoverse/types/events.py`；
- 子代理：`src/autoverse/core/subagent/definition.py`；
- 工作流：`src/autoverse/app/workflow/pipelines.py`；
- Web routes：`src/autoverse/frontends/web/server.py`。

不要先改文档表格，再让代码追随文档里的第二份清单。代码是可枚举事实的权威来源，reference 解释用户可见语义，contract tests 负责对照。

## 3. 实现并测试

优先运行最窄测试，再扩大范围：

```bash
pytest tests/unit/path/to/test_file.py -q
pytest tests/contract -q
pytest
```

测试选择原则：

- 共享契约、导入边界、文档事实进入 `tests/contract/`；
- 单组件成功、失败和边界分支进入 `tests/unit/`；
- 完整用户场景进入 `tests/acceptance/`；
- provider、MCP、hook 和 store 测试使用本地 double，不依赖真实网络。

## 4. 判断文档影响

先查看 [`../doc-impact.yml`](../doc-impact.yml)，再按变化语义判断：

| 问题 | 是时更新 |
|------|----------|
| 用户完成任务的步骤、默认行为或排障路径变化？ | `guides/` |
| 命令、字段、状态、API、工具、模型、事件或角色变化？ | `reference/` |
| 依赖方向、装配、状态所有权或运行时序变化？ | `architecture/` |
| 关键取舍、长期边界、非目标或开放问题变化？ | `design/`，必要时 ADR |

只有纯内部重构、且用户行为、公开 schema、架构边界和真相源位置都没有变化时，才使用具体 waiver。不要使用“无需文档”这类无法审查的理由。

## 5. 完整验证

提交前运行：

```bash
pytest
mypy src/autoverse
import-linter lint
python scripts/check_doc_impact.py --strict
pytest tests/contract/test_documentation_contracts.py
git diff --check
```

工作区包含其他人的改动时，只暂存本次文件，再运行：

```bash
python scripts/check_doc_impact.py --strict --staged
```

## 成功信号

- 相关最窄测试和全量测试通过。
- mypy strict 与 import-linter 通过。
- 文档契约和 doc-impact 检查通过，或给出可审查的 internal-only waiver。
- `git diff --check` 无空白错误。
- diff 只包含本次任务范围内的代码、测试和文档。

## 常见失败

### import-linter 报 core 依赖 infra

把 core 真正需要的行为缩成 port，由 app 注入 infra adapter。不要用延迟 import 掩盖运行时依赖。

### doc-impact 要求检查很多文档

映射给出候选责任，不代表每篇都必须改。逐项确认用户行为、当前事实、架构边界和设计取舍；确属内部重构时写出具体不变项。

### reference 表格和代码不一致

先修改权威 registry/模型，再同步 reference，并扩展对应 contract test。不要删除断言来迁就漂移。

## 相关文档

- [文档写作标准](../DOCUMENTATION.md)
- [分层与导入边界](../architecture/layers.md)
- [运行时组件地图](../architecture/component-map.md)
- [状态所有权](../architecture/state-ownership.md)
- [测试与验收参考](../reference/testing.md)
- [常见工作流](common-workflows.md)
