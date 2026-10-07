# 常见工作流

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

本页面向“我想完成一件事”的用户，而不是查字段的用户。

## 前置条件

- 能运行 `autoverse --help`。
- 在要检查或修改的项目目录启动，让 cwd、memory 和工具路径对准目标仓库。

## 完成第一次任务

1. 进入目标仓库：

```bash
cd /path/to/project
```

2. 启动 AutoVerse：

```bash
autoverse
```

3. 让模型先读项目，而不是直接改代码：

```text
先阅读这个仓库的 README、测试入口和主要代码结构，告诉我应该从哪里开始。
```

4. 给出明确任务：

```text
修复当前 failing tests。先定位失败原因，再做最小修改，最后运行相关测试。
```

5. 用命令查看状态：

```text
/diff
/usage
/context
```

6. 需要提交时使用工作流命令：

```text
/commit
```

预期结果：AutoVerse 会围绕当前 cwd 运行，工具输出进入事件流，最终可用 `/diff` 查看本轮改动。

## 修复一个测试失败

1. 先让模型收集证据：

```text
运行最小相关测试，解释失败断言和调用路径，不要先改代码。
```

2. 如果输出太长，查看 trace 或让模型聚焦：

```text
/expand
```

3. 要求最小修复：

```text
只修改导致这个测试失败的最小代码路径。不要重构无关模块。
```

4. 验证：

```text
重新运行失败测试，然后运行相邻测试文件。
```

常用命令：

```bash
pytest tests/path/to/test_file.py -q
pytest tests/path/to/test_file.py::test_name -q
```

## 写一个自定义命令

适合把团队反复使用的 prompt 固化为 Markdown 模板。

1. 创建命令 Markdown 文件。当前搜索路径和优先级见 [命令参考](../reference/commands.md#加载路径和优先级)。

2. 写 frontmatter 和模板正文：

```md
---
description: Review a path for correctness issues
---

Review this path and report correctness issues only:

$1

Current diff:

!`git diff -- $1`
```

3. 重启 AutoVerse，运行：

```text
/help
```

4. 调用命令：

```text
/review-path src/autoverse/core
```

注意：

- 普通展示命令不会进入 LLM 上下文。
- 模板命令展开后的 prompt 会进入上下文。
- 当前模板命令只实际支持 `model` 单轮覆盖；`agent` / `subtask` frontmatter 会被解析，但还不会自动派发子代理或隔离子任务。
- `!` shell 注入适合只读命令，例如 `git diff`，不要放有副作用的命令。

## 新增一个内建工具

适合需要稳定权限、结构化结果、可测试行为的功能。一次性 prompt 能解决的事情，不要先做成工具。

1. 先在 [`../reference/tools.md`](../reference/tools.md) 查能力等级、网络标记、
   dispatcher 边界和当前内置工具表，避免凭记忆写错安全语义。

2. 在工具注册位置添加工具定义和 handler。

3. handler 返回结构化 `ToolResult`，不要只返回裸字符串。

4. 加单元测试：

```bash
pytest tests/unit/core/test_tool_dispatcher.py -q
```

5. 更新参考文档：

- `docs/reference/tools.md`
- 如涉及权限，更新 `docs/design/tools-and-permissions.md`

验收标准：

- 权限等级清楚。
- 错误结果能被模型理解。
- 测试覆盖成功、失败和权限边界。

## 相关文档

- CLI flags：[`../reference/cli.md`](../reference/cli.md)
- 命令参考：[`../reference/commands.md`](../reference/commands.md)
- 工具参考：[`../reference/tools.md`](../reference/tools.md)
- WebUI 使用：[`using-webui.md`](using-webui.md)
- MCP 配置：[`mcp.md`](mcp.md)
- Skills：[`skills.md`](skills.md)
- Hooks：[`hooks.md`](hooks.md)
- WebUI API：[`../reference/webui-api.md`](../reference/webui-api.md)
