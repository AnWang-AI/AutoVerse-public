# Skills 参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

Skill 是 on-disk adapter 输入，运行时变成 `SkillRegistry` 中的 `Skill` 对象。registry
负责 prompt fragment、模型可调用 `Skill` 工具，以及终端前端的 prompt-template
slash command；core 只看到注册后的工具和 prompt fragment。当前已有本地
standalone skill package copy/install helper；它只返回 install records 和
diagnostics，不写持久 state/trust store，也没有默认 CLI/REPL/WebUI install、
uninstall 或 reload 入口。

## 发现路径

默认扫描：

```text
<cwd>/.autoverse/skills
<parent>/.autoverse/skills
...
<git-root>/.autoverse/skills
~/.autoverse/skills
```

如果当前目录没有 `.git` 祖先，repo-local 扫描会继续到 filesystem root，再追加 user root。

扫描结果包含 skills、diagnostics 和 roots。发现只返回数据，不直接修改 prompt。

## `SKILL.md`

最小示例：

```md
---
name: summarize-diff
description: Summarize a git diff for review.
---

Summarize this diff in concise reviewer language:

1. Identify the main files or areas changed.
2. Group the changes by behavior, tests, and maintenance impact.
3. Call out risks or follow-up questions.
```

## Frontmatter 字段

AutoVerse 只把 Codex/Claude 通用字段作为 skill 契约：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | string | 目录名 | `^[a-z][a-z0-9_-]{1,63}$` |
| `description` | string | 必填 | 摘要 |

其他 frontmatter 字段会被忽略，不参与匹配、展示或执行。

## 渲染

Skill body 是普通 Markdown 指令。通过 slash 命令显式调用并带参数时，AutoVerse 会把参数作为 `User arguments:` 附加到正文末尾，不要求 skill 使用私有模板语法。

## 匹配

`Skill.matches_request()`：

1. 先匹配 `description` 中单引号或双引号包住的短语。
2. 再抽取长度至少 4 的关键词，过滤停用词后与用户请求求交集。

## 注册规则

- 裸名 first-wins。
- 重复裸名默认报错，除非显式 `overwrite=True`。
- 插件 skill 会注册为 `namespace:name`，并在裸名空闲时同时占用裸名。
- `unique_enabled_skills()` 会折叠 namespaced alias。

## Prompt 和工具

`SkillRegistry.prompt_fragment()` 渲染 `Available Skills` 段。只要存在启用的 skill，registry 会贡献一个模型可调用工具：

```text
Skill
```

TUI 和 Line REPL 在 engine build 后会把已加载 skill 注册为普通 prompt-template
slash command，使终端 `/help`、补全和命令分发走统一路径。WebUI 当前不注册这些
动态 skill template commands；它暴露 `/skills` 管理命令、extension endpoints
和 registry 贡献的 `Skill` 工具。

## 当前运行时消费字段

当前 `SkillTool` 消费：

- `skill` 名称。
- `args` 显式调用参数，作为附加上下文追加到正文。

当前终端 skill slash 命令消费 `name`、`description` 和正文。

## Diagnostics

加载、发现、安装和运行诊断使用 `SkillDiagnostic`：

| 字段 | 说明 |
|------|------|
| `code` | 机器可读错误码 |
| `message` | 人类可读说明 |
| `path` | 相关路径 |
| `skill` | 相关 skill 名 |
| `severity` | `info/warning/error` |

## 开发入口

- `src/autoverse/infra/skills/spec.py`
- `src/autoverse/infra/skills/loader.py`
- `src/autoverse/infra/skills/discovery.py`
- `src/autoverse/infra/skills/installer.py`
- `src/autoverse/infra/skills/registry.py`
- `src/autoverse/infra/skills/tool.py`
- `src/autoverse/app/extension_admin.py`

相关设计背景见 [`../design/skills.md`](../design/skills.md)，跨扩展边界见 [`extensions.md`](extensions.md)。
