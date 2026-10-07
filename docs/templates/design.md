# Design 模板

> 当前事实来源：`docs/DOCUMENTATION.md` 与对应 reference/architecture 文档。
> 维护规则：修改模板时同步文档规范和 contract tests。

## 模板正文

```md
# <设计标题>

> 当前事实来源：`docs/reference/`、`docs/architecture/`、`src/autoverse/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

## 背景

## 关键取舍

## 边界与非目标

## 关联参考
```

“背景”“关键取舍”“边界与非目标”“关联参考”是必需章节。只有主题确实需要描述未来边界或尚未决定的语义时，才增加“目标形态”或“开放设计问题”；不要保留空章节、`TBD` 或实施 checklist。
