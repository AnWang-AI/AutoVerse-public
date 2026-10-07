# 项目记忆

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

项目记忆是启动会话时加载到 system prompt 的仓库上下文。它帮助 agent 理解本仓库的约定、架构边界和常用流程。

## 前置条件

- 当前 `cwd` 就是希望注入项目记忆的目录。
- 准备维护短小、稳定的 `AGENTS.md` 或 `CLAUDE.md`。

## 当前行为

当前实现读取 `memory.files` 配置列出的文件。默认只读取当前工作目录下的：

- `CLAUDE.md`
- `AGENTS.md`

这些内容会在 `EngineBuilder` 组装阶段进入 `PromptAssembler.memory`，随后每轮由 system prompt 消费。

当前默认路径不向上遍历父目录，也不会从 home 或父项目自动合并记忆。`scan_max_depth` 字段仍保留在配置模型里，但当前是兼容字段，不参与扫描。

可选 FTS5 retrieval 后端和 `retrieval_enabled` 判断函数已经存在，但默认 `EngineBuilder` 仍使用 cwd-only full memory 注入，还没有自动把 retrieval search 接进 prompt。

子代理生产路径使用独立的静态角色 prompt，只接收派发时提供的 task/context，不继承主 agent 的 `PromptAssembler.memory`。需要给子代理的项目规则应写进派发 context。

## 适合写什么

适合放稳定、可复用的信息：

- 架构边界和导入规则。
- 常用测试命令。
- 目录职责。
- 命名约定。
- 项目特殊安全要求。

不适合放：

- 临时任务计划。
- 密钥、token、账号。
- 已经过期的历史争论。
- 大段和当前仓库无关的通用教程。

## 与 docs 的区别

`docs/` 是给人读的文档；项目记忆是给 agent 在执行任务时读的上下文。两者可以互相引用，但不要把长篇设计文档整段塞进项目记忆。

推荐做法：

- `README.md` 和 `docs/` 解释给人。
- `AGENTS.md` 给 agent 简短、强约束、可执行的规则。

## 维护建议

- 保持短小，优先写约束和命令。
- 删除已经不适用的历史说明。
- 如果某条规则只适用于一个子目录，把它放在该子目录自己的 `AGENTS.md`，并从该子目录作为 `cwd` 启动或显式传给子代理 context。

## 相关文档

- [配置参考](../reference/config.md)
- [模型与协议参考](../reference/models.md)
- [运行路径](../architecture/runtime-flow.md)
- [文档写作标准](../DOCUMENTATION.md)
