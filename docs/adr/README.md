# 架构决策记录（ADR）

> 当前事实来源：`docs/adr/*.md`、相关 architecture/design 文档。
> 维护规则：新增、废弃或取代 ADR 时同步索引；不要把 ADR 当作实施计划。

本目录沉淀**少数重大、有争议、未来可能被翻案**的架构决策。每个决策一个文件，命名 `NNNN-短标题.md`。

## 当前决策

- [ADR-0001：L3 会话工作区默认 SHARED，worktree 隔离 opt-in](0001-l3-default-shared.md)
- [ADR-0002：e2e-verifier 靠"隔离 + 事后审计"约束，而非事前剥夺写/执行能力](0002-e2e-verifier-isolate-and-audit.md)
- [ADR-0003：工作区快照独立于 Isolator，用影子 git 而非 per-file 内存存档](0003-snapshot-shadow-git-vs-isolator.md)
- [ADR-0006：密钥默认用机器绑定加密 vault，而非明文配置或仅环境变量](0006-secrets-machine-bound-vault.md)
- [ADR-0007：PreToolUse 钩子是中间件，可 deny 但默认不静默改写 tool_input](0007-hook-no-silent-rewrite.md)
- [ADR-0008：IM 网关无人值守审批默认 deny（fail-safe），而非 allow](0008-gateway-unattended-deny.md)
- [ADR-0009：json_recovery 做成仅 degradation_prone 后端启用的中间件，而非默认路径](0009-json-recovery-capability-gated.md)
- [ADR-0010：用"四维度"作为工作流的统一设计语言](0010-workflow-four-dimensions.md)

## 与设计文档的分工

- **设计和架构文档**（`docs/design/`、`docs/architecture/`）陈述"系统是怎样的"——稳定、随代码同寿。绝大多数决策理由**内化成一句话** inline 在对应抽象旁（结论 + 一个理由 + 一个可证伪边界），不写正/反/裁决的辩论体。
- **ADR**（本目录）只收**值得长期追溯论证过程**的少数决策。设计文档在对应处留一行链接（如 `（完整权衡见 ADR-0001）`），保持正文干净、论证可考。

判断某决策该内化还是抽 ADR：

| 内化进设计文档 | 抽成 ADR |
|---|---|
| 结论清晰、不会翻案 | 有真实争议、未来可能翻案 |
| 一句话能讲清理由 | 需要完整背景 + 多方权衡 |
| 局部、单模块 | 跨模块、影响面大 |

新增 ADR 时使用 [`../templates/adr.md`](../templates/adr.md)，不要在索引页维护第二份模板。
