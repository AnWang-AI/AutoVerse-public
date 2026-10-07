# 文档写作标准

> 当前事实来源：`docs/templates/`、`docs/doc-impact.yml`、`tests/contract/test_documentation_contracts.py`。
> 维护规则：模板、影响映射或治理流程变化时同步本页、模板和 contract tests。

AutoVerse 文档只服务三个目的：使用、排障、贡献代码。读者不应该为了确认当前行为而翻旧设计稿，也不应该在 docs 里维护第二套任务历史。

## 文档分层

| 层 | 读者 | 应该回答 | 不应该写 |
|----|------|----------|----------|
| `guides/` | 使用者 | 怎么安装、配置、启动、完成一个任务、排错 | 长篇架构推演 |
| `reference/` | 使用者和贡献者 | 命令、配置、工具、事件、角色、API 的当前行为和字段 | 历史争论、目标形态冒充现状 |
| `architecture/` | 贡献者 | 代码怎么分层、运行路径、导入边界、该改哪个模块 | 用户教程 |
| `design/` | 维护者 | 为什么这样设计，关键取舍和开放设计问题是什么 | 字段全集和重复 API 参考 |
| `adr/` | 维护者 | 已采纳、废弃或被取代的架构决策 | 临时待办、实现计划、审计过程稿 |

`guides/` 只写完成任务所需的最小步骤、可运行示例和关键失败信号。字段全集、状态枚举、协议变量、API endpoint、命令清单和 registry 镜像只放在 `reference/`；guide 需要时链接，不复制第二份表。

## 信息架构质量门槛

文档分层还需要满足可发现性和可维护性：

- 每篇非索引文档必须从 `docs/README.md`、所在目录索引或一篇直接相关文档可达，不能形成孤立页面。
- 新用户路径优先从 guide 进入；精确事实从 reference 进入；贡献者先读 architecture，再按需进入 design/ADR。
- 同一个可变状态必须在 architecture 中标明唯一权威所有者。前端缓存、trace、SSE block log 和 sidebar state 只能是派生视图，不能成为第二套控制真相。
- architecture 的图只画稳定组件、端口和数据流；图中出现的代码路径必须存在，时序必须与当前执行顺序一致。
- reference 页面超过 300 行、15 个二级标题、6 张表，或同时承担三个以上独立查询意图时，应人工评估是否按查询意图拆分。这是评审信号，不是机械拆页规则。
- 可枚举事实优先由 contract test 与代码 registry 对照；只有当人工维护成本持续偏高时，才改成确定性生成块。解释、边界、失败语义和选择建议始终由人维护。

设计文档的必需章节是“背景”“关键取舍”“边界与非目标”“关联参考”。“目标形态”和“开放设计问题”按主题需要使用；没有真实内容时直接省略，不写“暂无”、`TBD` 或空章节。开放设计问题描述尚未决定的语义、受影响边界和重新决策所需证据，不能写成实施 checklist。

## 模板优先

新增或规范化文档时，先从 [`templates/README.md`](templates/README.md) 选择模板。模板清单只维护在模板索引页，避免新增、删除或改名模板时出现双维护漂移。

模板是各类文档结构的唯一规范来源。如果文档形态要变化，修改对应模板和 contract tests；不要在本页维护第二份 guide/reference/design 字段清单。

标准文档头只保留两类元信息：

- `当前事实来源`：读者和 agent 应该去哪核对当前行为。
- `维护规则`：什么变更会要求同步本页。

不要写通用 `文档类型` 或 `状态` 横幅。文档类别由目录和模板表达；实现完成度不是文档元数据。ADR 例外：架构决策记录可以写 `决策状态`，因为它描述的是决策生命周期。

## 当前文档模型

docs 只保留仍有维护价值的内容：

- 当前事实：写入 `guides/`、`reference/` 或 `architecture/`。
- 设计取舍和目标形态：写入 `design/`，必要时用 ADR 固化已采纳决策。
- 语义待办：只在所属 `design/` 文档的“开放设计问题”中保留；解决后删除开放问题，必要事实并入当前文档。

已经落地、取消或过期的实施计划不保留正文清单。若仍有当前价值，改写成事实、取舍、非目标或 ADR；否则删除。历史追溯使用 git 或外部 issue，不在 docs 中复制审计过程稿、完成清单或历史副本。

禁止新增以 `done`、`todo`、`archive`、`plan`、`check` 或同义词命名的文档分类目录。不要把当前产品接口误判为文档治理分类：`todo_write`、plan mode、`PLAN_PROPOSED` 等用户可见 token 属于 reference 当前事实。

## 文档引用规则

- 当前行为引用 `docs/reference/` 或 `docs/architecture/`。
- 设计取舍引用 `docs/design/` 或 `docs/adr/`。
- 语义待办引用对应 `docs/design/` 文档里的“开放设计问题”。
- 带 `#anchor` 的本地 Markdown 链接必须指向目标文档里实际存在的 heading。
- 文档中写出的仓库内路径必须存在；通配路径必须至少匹配一个当前文件。
- 共享类型、端口和模型 registry 的字段全集以对应代码模块为准，文档只说明真相源位置和使用边界。
- 用户可见的命令、flag、模式、状态和 endpoint 必须使用当前代码暴露的实际 token；内部 presentation 名称不能冒充用户接口。

## 文档影响治理协议

每次代码改动都必须处理文档影响。处理不等于每次都改文档；处理意味着明确判断“哪些文档受影响，是否需要更新，为什么”。

权威映射文件是 [`doc-impact.yml`](doc-impact.yml)：

- `required_docs`：当前事实手册，相关行为/API/字段变化时必须同步。
- `conditional_docs`：用户路径、设计取舍或开放设计问题变化时按需同步。
- `tests`：该区域的文档或契约检查入口。

提交前必须运行：

```bash
python scripts/check_doc_impact.py --strict
```

如果工作区包含不属于本次提交的本地改动，先只暂存本次提交文件，再运行：

```bash
python scripts/check_doc_impact.py --strict --staged
```

如果脚本提示受影响文档，必须更新对应文档并重新运行检查；只有确认是内部重构、无用户可见行为/API/配置/命令/工具/schema/架构边界变化时，才允许带理由豁免：

```bash
python scripts/check_doc_impact.py --strict \
  --allow-none-reason "private helper extraction only; commands, schema, errors, runtime path, and public imports unchanged"
```

新增、删除、重命名或改变以下事实时不能使用 internal-only waiver：用户可见命令/flag、配置字段或默认值、HTTP route 或请求/响应形状、工具名称/schema/capability、事件/payload、模型 registry 或能力位、子代理角色、workflow definition、导入边界、运行时序、生命周期或权威状态所有者。这些变化必须同步当前文档，并更新负责把文档清单或边界与代码对照的 contract test。

Waiver 必须指出实际保持不变的边界，例如“只抽取 private helper；命令 token、配置 schema、错误行为、运行路径和 public import 均未变化”。“不需要文档”不是可审查的理由。

建议 commit message 或 PR 描述包含：

```text
Docs-Impact: updated
```

或：

```text
Docs-Impact: none
Docs-Impact-Reason: private helper extraction only; commands, schema, errors,
runtime path, and public imports unchanged
```

## 变更到文档的映射

| 代码变化 | 文档动作 |
|----------|----------|
| 命令、CLI flag、Web route、工具 schema、配置字段、模型 registry、事件、子代理角色 | 更新 `reference/` |
| 用户操作步骤、错误排查路径、默认启动方式 | 更新 `guides/` |
| 导入边界、端口归属、真相源位置、运行路径 | 更新 `architecture/` |
| 设计取舍、长期目标、非目标 | 更新 `design/` 或新增/更新 ADR |
| 新增、推进或收束语义待办 | 更新所属 `design/` 的“开放设计问题” |
| 审计或复核结论 | 只并入仍有当前价值的 guide/reference/architecture/design/ADR |

机器检查负责提醒候选文档、路径存在性、旧引用、可枚举事实覆盖代码 registry；人工判断负责语义质量、是否需要新 guide、开放设计问题是否还成立，以及变更是否真的只是内部重构。

## 责任与评审

文档责任不写入每篇页面的 owner 元数据。代码区域到文档的责任放在 `doc-impact.yml`，人员或团队评审责任放在仓库的评审规则中，避免人员变化造成正文漂移。

评审不同文档时分别确认：

- guide：步骤可运行，成功信号真实，失败路径可行动，没有复制 reference 清单。
- reference：用户可见 token 与代码一致，当前能力和保留/未接入边界明确，开发入口存在。
- architecture：依赖方向与 import-linter 一致，状态只有一个权威所有者，时序与代码一致。
- design/ADR：当前事实链接到 reference/architecture，开放问题不是任务清单，重大裁决包含可证伪边界。

## 提交前检查

新增或修改功能时同时确认：

1. 用户路径是否需要 guide 更新。
2. 命令、配置、工具、事件、API 或角色是否需要 reference 更新。
3. 架构边界、端口归属或运行路径是否需要 architecture 更新。
4. 设计取舍、目标形态或非目标是否需要 design/ADR 更新。
5. 语义待办是否需要新增、改写或删除“开放设计问题”。
6. 已落地或失效的过程性内容是否已经删除，并把仍有效事实并入当前文档。
7. 新文档是否已进入索引或相关主题导航，没有形成孤立页面。
8. 可变状态、缓存和 replay 数据是否明确区分权威真相与派生视图。
9. 新增可枚举清单是否有代码 registry 或 contract test 支撑。
