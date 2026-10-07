# Architecture 模板

> 当前事实来源：`docs/DOCUMENTATION.md`、`pyproject.toml` 和 import-linter contract tests。
> 维护规则：修改模板时同步文档规范和 contract tests。

## 模板正文

```md
# <架构标题>

> 当前事实来源：`src/autoverse/`、`pyproject.toml`、import-linter 和 contract tests。
> 维护规则：分层、端口、入口、导入边界或真相源位置变化时必须同步。

## 背景

## 当前结构

## 边界规则

## 状态所有权 / 运行路径

## 相关代码
```

按主题选择“状态所有权”或“运行路径”，不要求同时出现。涉及可变状态时必须说明权威所有者、生命周期、持久化边界和允许的派生视图。
