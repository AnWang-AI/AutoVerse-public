# 设计记录

> 当前事实来源：`docs/design/*.md`、`docs/adr/` 和 `docs/DOCUMENTATION.md`。
> 维护规则：新增、拆分或废弃设计记录时同步；不要复制 reference 字段表。

这里保存维护者需要理解的设计背景。它不是新用户的默认入口；使用文档和参考手册优先。

## 主题

- [`llm-protocol.md`](llm-protocol.md)：模型注册、wire codec、transport、credential 和 fallback。
- [`tools-and-permissions.md`](tools-and-permissions.md)：工具协议、dispatcher、权限、能力分级、文件追踪。
- [`subagents-and-workflows.md`](subagents-and-workflows.md)：子代理、隔离、审计、StopGate 和 pipeline。
- [`frontend.md`](frontend.md)：CLI/TUI/Line REPL/WebUI/移动/Apple 壳的设计取舍。
- [`frontend-interaction-responsiveness.md`](frontend-interaction-responsiveness.md)：Web/Apple 壳的输入响应、会话导航、历史窗口、运动 token 和性能验证边界。
- [`persistence-observability.md`](persistence-observability.md)：store、trace、usage、会话栏和回放。
- [`skills.md`](skills.md)：skill/plugin 角色边界、渐进加载、安全和生命周期取舍。

## 维护边界

本页只维护设计记录导航。设计文档的写作边界、开放设计问题分工和过期资料处理规则见 [`../DOCUMENTATION.md`](../DOCUMENTATION.md)。
