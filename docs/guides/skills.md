# 使用 Skills

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

Skill 是可复用的提示词包。它适合沉淀“什么时候用、怎么做、需要哪些参考资料”的操作规程，而不是替代普通代码或工具。

## 前置条件

- 能启动一个新的 AutoVerse session。
- 准备把 skill 放入项目或用户扫描目录；修改后通过重启或新建 session 生效。

## Skill 放在哪里

推荐路径：

- 团队共享：当前仓库里的 `.autoverse/skills/<name>/SKILL.md`。
- 个人使用：`~/.autoverse/skills/<name>/SKILL.md`。

完整扫描规则和冲突处理只维护在 [`../reference/skills.md`](../reference/skills.md)。

## 创建一个 Skill

创建目录和 `SKILL.md`：

```text
.autoverse/skills/review-python/SKILL.md
```

示例：

```md
---
name: review-python
description: Review Python changes for correctness, typing, and architecture risks.
---

Review Python changes for correctness bugs, missing tests, typing regressions,
and AutoVerse layer-boundary violations.

When the user provides a path, diff, or scope, focus the review there.
Report findings first, ordered by severity.
```

重启 AutoVerse 后，用户可调用的 skill 会被注册为普通 slash 命令：

```text
/review-python src/autoverse/core
```

模型也会在 system prompt 的 `Available Skills` 段看到启用 skill 的名称和描述，并被提示在适用时优先调用 `Skill` 工具。

## Frontmatter 字段

Skill 只需要 `name`、`description` 和正文。`description` 应写清触发场景，
不要只写“helper”或“tooling”这类无法匹配用户意图的泛化词。

其他 frontmatter 字段会被忽略，不参与匹配、展示或执行。需要 references、
scripts 或 assets 时，把相对路径和使用方式写进正文，保持和 Codex/Claude
skill 包通用。

字段全集和精确命名规则只维护在 [`../reference/skills.md`](../reference/skills.md)。

## 匹配行为

AutoVerse 用 `description` 做轻量匹配：

因此推荐把触发场景写进 `description`：

```yaml
description: Create release notes or changelog entries when the user asks for release writing, version summaries, or change highlights.
```

精确匹配算法见 [`../reference/skills.md`](../reference/skills.md)。

## 常见失败

### Skill 没出现在 `/help`

检查文件名是否是 `SKILL.md`，`name` 是否符合命名规则，并确认 `description` 存在。

### 模型没有自动使用 skill

检查 `description` 是否清楚写出触发场景。

### 同名 skill 冲突

注册表采用 first-wins，裸名冲突会被拒绝。插件来源可以使用 `namespace:name` 别名，普通仓库 skill 应避免同名。

### 怎样安装独立 skill 包？

当前运行时已有本地 skill package copy/install helper，但默认 CLI、REPL 和 WebUI 还没有 `install`、`uninstall` 或 `reload` 管理入口。今天可直接生效的路径仍是把 `SKILL.md` 放进上述扫描目录，然后重启或新建 session。


## 相关文档

- [Skills 参考](../reference/skills.md)
- [扩展边界参考](../reference/extensions.md)
- [命令参考](../reference/commands.md)
- [Skill 与 Plugin 生态设计](../design/skills.md)
