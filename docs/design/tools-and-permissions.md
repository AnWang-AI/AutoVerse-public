# 工具与权限设计

> 当前事实来源：`docs/reference/`、`docs/architecture/`、`src/autoverse/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

工具系统的设计重点是把“模型想做的事”变成受控、本地可审计的动作。权限、能力分级、路径解析、读后写和取消都属于工具执行边界。

## 背景

工具同时连接模型意图、本地文件、进程、网络和用户审批，是风险最集中的执行边界。如果每个工具自己处理权限、取消和事件，安全语义会随工具数量增长而漂移；如果 dispatcher 吞下所有文件语义，它又会变成无法维护的万能层。

## 关键取舍

### 工具安全边界

工具不应该各自手写权限、取消、事件和错误包装。`ToolDispatcher` 是统一外层中间件链，集中处理：

- schema 校验。
- 能力和权限。
- abort 和 timeout。
- 追踪和错误包装。

路径解析和隔离映射由 `ToolContext` 提供；read-before-write 和 stale-read 由文件类工具通过 `ToolContext.tracker` 执行。这样 dispatcher 保持薄外壳，文件语义仍在最了解文件操作的工具里。

### 能力分级优于黑白名单

角色和模式不应维护大量工具名 blacklist。更稳定的做法是按能力等级过滤工具，再对少数特殊工具加声明式属性。

文件系统风险和网络能力是两根轴：一个工具可以只读但需要网络，也可以写文件但不需要网络。

### 权限不属于 core loop

权限引擎是 adapter/policy，不应该写进 TurnLoop。core 在工具执行前请求 permission port，具体 UI 如何审批由终端或 Web adapter 决定。

### 文件追踪

写入类工具需要知道文件是否已读、路径是否在允许范围内、操作是否处于隔离 worktree。这样能降低模型“盲写”和跨工作区污染的风险。

### 错误反馈

工具失败应该返回可行动信息：

- 哪个参数错了。
- 哪个路径不可访问。
- 哪条权限规则拒绝。
- 是否被中断或超时。
- 失败后模型可以尝试的下一步。

### 中间件顺序必须可观测

生产装配的普通 per-call 执行链按 PreToolUse hook、lookup/schema、plan-mode gate、permission、`TOOL_START`、tool execution、`TOOL_END`、PostToolUse 前进。hook 是 app 层对 `dispatch_one` 的外层包装，因此 hook deny 会在 route/schema/permission 前短路。被 hook 拒绝与被 permission 拒绝必须保留不同错误类型，便于模型、用户和 trace 判断阻断来源。

### 本地风险与网络风险正交

`CapabilityTier` 表达文件系统和进程风险，`needs_network` 单独表达网络出站。浏览器工具虽然通常不写本地文件，却可能触发远端副作用，因此仍需要 plan gate 和权限策略，而不是因为 `READ_ONLY` 就无条件执行。

### 可见浏览器的共享控制

Apple 原生浏览器把浏览器 profile 与网页状态留在 WKWebView 所在进程，工具侧只拿
`BrowserPort`。用户接管是独立于 permission port 的并发控制：接管后 host 必须拒绝
agent 命令，交还后才继续。页面集合、稳定 page id 和活动页同样由 host 单点持有，broker
不建立可漂移的镜像；显式页面目标用于可复现控制，省略目标才跟随当前活动页。URL guard
仍是工具安全边界；原生 host 只因页面对用户可见而
增加 literal loopback 例外，不放开 private、link-local、reserved 或 metadata 地址。

普通页面动作的反馈应在同一个工具结果中包含同一 navigation revision 的最终页面状态和
受限语义 DOM 快照。所有模型都先得到这份可操作的结构化文字；视觉模型在 `auto` 策略下
仅对 canvas/video 或图像密集、文字稀少的页面附带截图，纯文本模型的 `auto` 不截图。
不能因为 frontend 能够采集截图，就在动作返回前隐式追加一次视觉模型请求；这会让
用户已经看到的新页面与 agent 收到工具结果之间产生不必要的模型级延迟。显式截图工具
仍可按请求调用视觉读取。语义快照不采集输入框当前值，避免把密码或表单数据误作控件名称；
DOM 文本和页面元数据始终是不可信的外部内容，预览必须限长并明确标记。观察前后必须核对
revision；反馈提取采用 best-effort，提取失败不能把已经由 host 确认成功的页面动作改写成失败。
截图就绪同样采用 bounded best-effort：等待 DOM、字体、有限数量的首屏图片和两个绘制帧，
但不以全局网络空闲作为成功条件。这样可减少半绘制截图，又不会被长连接或第三方资源拖住；
等待失败仍可截图，revision 变化则重试或明确拒绝不一致的图像。

页面 JavaScript 的执行结果也必须在 adapter 边界收敛：工具 schema 明确同步表达式、显式
异步函数体和 JSON 最终值约束；具体 backend 负责等待与超时，并把 native engine 的类型
错误映射为稳定 `BrowserError`。core 只负责把已经满足 port 契约的值展示为严格 JSON，不能
依赖某个 WebKit/Playwright 私有对象，也不通过匹配本地化错误字符串推断恢复动作。

## 边界与非目标

- Prompt 说明不是权限边界；真实限制由 dispatcher、permission port、hook、角色过滤和 isolation 执行。
- Dispatcher 不实现每种工具的路径语义；文件工具通过 `ToolContext.tracker` 处理 read-before-write 和 stale-read。
- Hook 默认不能静默改写 tool input；调整参数应让模型看到并重试。
- Shell 无法只靠静态 tier 完整分类，仍需具体命令策略、审批和隔离。
- ToolResult 面向模型和 frontend 共享，不能只为某个 UI 返回不可解释的私有对象。

## 关联参考

- 工具、能力和执行路径：[`../reference/tools.md`](../reference/tools.md)
- Hook 当前协议：[`../reference/hooks.md`](../reference/hooks.md)
- 子代理角色边界：[`../reference/subagents.md`](../reference/subagents.md)
- Hook 输入决策：[`../adr/0007-hook-no-silent-rewrite.md`](../adr/0007-hook-no-silent-rewrite.md)
