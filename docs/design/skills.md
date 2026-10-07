# Skill 与 Plugin 生态设计

> 当前事实来源：`docs/reference/skills.md`、`docs/reference/extensions.md`、`docs/architecture/layers.md`、`src/autoverse/infra/skills/`、`src/autoverse/infra/plugins/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

本文只记录 skill/plugin 生态的长期设计边界和关键取舍。命令、字段、endpoint、状态值和当前实现能力以 reference 与代码为准；尚未落地但仍会影响边界的设计问题在本文集中维护。

## 背景

AutoVerse 需要同时支持四类扩展：

- 项目级 instruction 和 memory，用来约束当前仓库。
- Skill，用来复用一个任务工作流。
- Plugin，用来安装、分发和管理一组 capability。
- MCP、hook 和 tool，用来接入外部系统或执行动作。

如果把这些能力混成一层，core 会被安装来源、UI 状态、权限审查和 prompt 投影细节污染。设计目标是让每类扩展有清晰职责，并让 app/infra 完成装配，core 只消费注册后的工具、事件、prompt fragment 和 port。

## 目标形态

Skill 是可发现、可诊断、可按需加载的工作流单元。它的最小表达是一个目录和 `SKILL.md`：

```text
review-diff/
├── SKILL.md
├── references/
└── scripts/
```

`SKILL.md` 的 frontmatter 只承担轻量索引职责；正文才是 agent-readable workflow。较长资料放在 `references/`，确定性辅助逻辑放在 `scripts/`，但读取或执行都必须走普通工具和权限路径。

Plugin 是 capability 的安装和生命周期管理单元。一个 plugin 可以贡献 skills、tools、hooks、MCP servers、commands 或 assets，但它不拥有这些 capability 的运行时语义。每类 capability 仍由原本 registry 或 manager 消费。

```text
Plugin package
  -> PluginManager validates and distributes
     -> SkillRegistry
     -> ToolRegistry
     -> HookEngine
     -> MCPManager
     -> CommandRegistry
```

## 关键取舍

### Skill 和 Plugin 分层

Skill 适合“一个可复用工作流”。Plugin 适合“一个可安装、可启停、可审计的能力包”。独立 skill 不需要伪装成 plugin；只有当包需要同时分发多类 capability、共享资产、依赖或管理元数据时，才升级为 plugin。

这个边界避免把简单工作流强行装进复杂包管理，也避免 plugin 变成万能 prompt 注入器。

### 兼容通用 `SKILL.md`

AutoVerse 不发明专属 skill 文件格式。`name` 和 `description` 是稳定索引字段，body 是工作流说明。其他扩展字段不能成为运行时真相源；需要工具边界、动态上下文或权限策略时，应通过普通工具、hook、子代理角色或权限配置表达。

这样做的代价是 skill 包不能靠私有 frontmatter 获得特殊权限；收益是 skill 可以跨生态复用，loader 逻辑也保持小。

### Progressive Disclosure

Skill 不应把所有正文常驻 system prompt。初始 prompt 只投影轻量 index，例如名称、来源和 description；模型显式或隐式选择后，再通过 `Skill` / resource 读取完整正文或引用资料。

这条规则服务三个目标：

- 控制上下文体积。
- 让激活行为可观测、可追踪。
- 避免一个大型 skill 在无关任务中污染模型判断。

### 隐式调用可推荐，不应偷偷执行

隐式匹配只产生候选和提示，最终激活仍应该通过可观测路径发生。显式调用优先级最高；隐式调用需要记录候选、匹配理由、激活来源和读取资源。

这能保留模型判断空间，同时让 `/skills doctor`、trace 和复盘能解释“为什么这个 skill 被用到”。

### Plugin 只分发，不越权执行

Plugin manager 的职责是发现、校验、审查、启用、禁用和分发 capability bundle。它不直接执行 tool、hook、MCP 或 skill，也不直接写 prompt。

运行时顺序应保持为：

1. app/infra 发现 plugin 和 skill。
2. plugin 产出 capability bundle。
3. 各 registry 接收自己负责的 capability。
4. prompt、tool dispatch、hook 和 MCP 生命周期由对应组件处理。

这个顺序让 plugin 能组合能力，但不会绕过既有权限、事件和架构边界。

### 管理状态必须共享

TUI、Line REPL 和 WebUI 不能各自扫描目录、各自维护 enable/disable/trust 状态。目标形态应有 frontend-agnostic 的 admin service，前端只负责展示和发起动作。

当前行为细节、已暴露命令和 session-local 限制见 [`../reference/extensions.md`](../reference/extensions.md)。持久化状态、重新装配和安装/卸载闭环仍是开放设计问题。

## 运行路径

Skill 运行路径应保持可解释：

1. discovery 扫描 repo/user/admin/system/plugin skill roots。
2. loader 解析 `SKILL.md`，只生成 spec、diagnostics 和 resource manifest。
3. registry 处理冲突、namespace、启用状态和轻量 prompt index。
4. prompt assembler 投影轻量 skill index。
5. 用户显式调用或模型通过 `Skill` tool 激活 skill。
6. 激活以普通 tool event 进入事件流和 trace；是否需要把 resource 读取、匹配理由或 compaction 摘要标准化，仍属于开放设计问题。

Plugin 运行路径应保持可回滚：

1. discovery 找到 plugin package 或 entry point。
2. loader 校验 manifest 与声明的 capability roots。
3. trust/dependency 检查决定是否可激活。
4. manager 把 capability bundle 分发给对应 registry。
5. enable/disable/reload 在安全点重建 graph，而不是半途改写正在运行的 turn。

## 边界与非目标

- Skill 不是第二套 tool API。
- Skill 不替代 `AGENTS.md`、project memory 或配置文件。
- Skill body 不声明工具 allow/deny 权限；权限仍归 dispatcher、permission port、hook 和子代理角色。
- Plugin 不是安全特权层；plugin 贡献的所有 capability 仍走原本运行边界。
- Plugin 不直接写 prompt；prompt fragment 由 capability owner 投影。
- Frontend 不直接反射 infra 目录来拼管理状态；共享服务输出纯数据。
- Core 不导入 `infra.skills` 或 `infra.plugins`；core 只消费注册后的工具、prompt fragment、事件和 port。

## 安全原则

- Skill 是 prompt 和资源清单，不是可信代码。
- `references/` 读取受路径和大小限制。
- `scripts/` 执行必须经过普通工具、permission、hook 和审计。
- Repo-local skill 和 plugin 默认需要 workspace trust 或等价审查。
- Plugin manifest 是声明，不是授权。
- Plugin 禁用必须完整移除它贡献的 runtime capability。
- Plugin tool 名称冲突应 fail closed。
- Hook 不能静默改写工具输入，除非 hook framework 明确记录改写结果。
- Secrets 只能声明依赖；读取仍通过 `SecretProvider`。

## 开放设计问题

这些问题不是当前行为说明，也不是实施 checklist；用户可见现状仍以 reference 为准。保留它们只因为它们会影响 skill/plugin 生态的边界：

- Extension 管理状态是否应持久化到配置或 state store；这个决定会影响 enable/disable/trust 是否跨 session 生效，以及是否需要在安全点重建 extension graph。
- Standalone skill package 安装是否应进入默认管理面；如果进入，需要同时确定 install record、hash-bound trust、卸载和 reload 的单一真相源。
- Skill resource/reference 的按需读取是否需要标准化为可追踪 resource manifest；这个决定会影响匹配理由、trace、request snapshot 和 compaction 摘要，而不改变当前“轻量 index + `Skill` tool 读取正文”的已落地边界。

## 关联参考

- 当前 skill 行为：[`../reference/skills.md`](../reference/skills.md)
- 当前 extension/plugin 管理行为：[`../reference/extensions.md`](../reference/extensions.md)
- 当前命令行为：[`../reference/commands.md`](../reference/commands.md)
- WebUI extension endpoints：[`../reference/webui-api.md`](../reference/webui-api.md#extensions)
- 分层边界：[`../architecture/layers.md`](../architecture/layers.md)
