# 测试与验收参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

本页记录贡献者最常用的验证命令和测试分层。

完整的代码贡献路径见 [`../guides/contributing.md`](../guides/contributing.md)。

## 常用命令

代码质量基础检查：

```bash
pytest
mypy src/autoverse
import-linter lint
git diff --check
```

文档契约与文档影响检查属于提交治理流程，命令和 waiver 规则只维护在
[`../DOCUMENTATION.md#文档影响治理协议`](../DOCUMENTATION.md#文档影响治理协议)。

## 测试类型

- `tests/contract/`：文档、架构和共享契约。
- `tests/unit/`：模块级行为。
- `tests/acceptance/`：端到端能力和用户可见行为。
- 架构边界：由 `import-linter` 契约检查。

## 写测试建议

- 新工具优先写参数校验、权限拒绝、成功结果和失败反馈。
- 新 provider 优先测 codec 的输入输出，不依赖真实网络。
- 新前端能力优先测事件到展示模型的映射。
- 新 workflow 或 StopGate 要测继续、停止和 gate 错误三类分支。

## 开发入口

- `tests/`
- `pyproject.toml`
