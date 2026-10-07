# 前端设计

> 当前事实来源：`docs/reference/`、`docs/architecture/`、`src/autoverse/`。
> 维护规则：设计取舍、非目标、目标形态或开放设计问题变化时同步。

AutoVerse 的前端都是消费侧 adapter。它们把用户输入送进 engine，把 `StreamEvent` 渲染成人类可读界面，并承接权限、计划和问答交互。

## 背景

CLI、TUI、Line REPL、WebUI 和 Gateway 的交互能力差异很大，但它们面对的是同一个 agent runtime。如果前端各自实现命令、工具状态或完成判定，用户可见语义会随入口漂移，因此领域行为必须留在 engine、port 和共享 registry 中。

## 关键取舍

### 终端前端

终端前端分四档：

- `tui`：Textual 全屏。
- `inline`：Textual 内联。
- `line`：行式 REPL。
- `plain`：脚本友好输出。

它们尽量共享命令注册表、主题角色、工具渲染和事件消费逻辑。区别主要是布局、重绘能力和交互丰富度。

### WebUI

WebUI 把只读 view session 与 engine runtime 暴露为 REST + SSE：

- `SessionManager` 先管理 catalog/history-backed view session，运行时动作再 lazy activate engine。
- `SessionBridge` 维护可 replay 的块日志和 SSE 队列。
- `SessionHistoryPort` 按完整 turn 提供有界 tail/before 窗口。
- `WebLog` 作为 `TuiEventSink` 的鸭子表面，把渲染写调用转成 Web 块。
- 浏览器端负责 DOM、布局、文件预览、会话栏和移动适配。

文件预览把“可点击性”和“读取授权”分开：浏览器把对话文件引用与 Changed/Memory 项统一
映射到右栏动作；后端仍以 cwd 为默认边界，只对当前 view session 已明确展示的 cwd 外文件
派生逐文件只读 capability。不能为了方便把预览 GET 退化为任意本机绝对路径读取，也不能
让临时 capability 获得目录列举、重命名或删除能力。

### 移动和 Apple 壳

移动端优先复用 WebUI 的响应式布局。Apple 壳作为 SwiftUI WebView 外壳，负责本地窗口、
深链、系统 WebKit 浏览器和平台集成，不复制 agent 逻辑。

macOS 窗口栏可以消费 WebUI 派生的当前会话标题和 dashboard 栏宽，用于让原生窗口 chrome
与浏览器三栏连续对齐。工具 Browser 打开期间，Swift 壳持有本次展示的临时宽度并通过单向
预览让底层 WebUI 轨道对齐；关闭后 WebUI 的持久 dashboard 宽度重新生效。该桥只承载展示
状态和 UI 动作，不承载消息、运行状态或工具状态；会话真相仍由 Conversation/Store 持有。

工具浏览器与聊天 WebView 是两个隔离 surface：聊天 WebView 只渲染 WebUI；工具浏览器
持有独立、持久的 named WebKit profile，并通过 server 的窄 `BrowserPort` command bridge
执行动作。页面、cookie 和控制权留在 Apple 进程，Python 不复制浏览器状态。原生壳负责
有序标签、稳定 page id、活动页和新窗口语义；隐藏分栏是展示状态，不等价于关闭页面。共享页面
对话中的 HTTP(S) Markdown 链接、裸 URL 与完整 inline-code URL 保持普通 Web 的
`_blank` 语义；Apple 壳可用
单用途进程内消息把点击升级为工具 Browser 新页面，并在原生 URL policy 校验后激活。该 UI
入口不写回消息、不复用 agent command channel，也不允许非 Web scheme 绕过安全边界。
默认由 agent 控制，用户可显式接管；下一条有效聊天消息通过进程内 UI 事件自动恢复 agent
控制。这项 UI 状态不改变 permission policy，也不复制消息或运行状态，只阻止两方同时
操作页面。Apple 壳从当前 command 派生短生命周期 activity 展示：agent 使用琥珀色，用户
接管使用绿色；快速命令只把具体动作展示补足到 800ms，用户接管仍立即清除。它不进入
Browser 协议、页面状态或持久化。host 为
每页维护单调 navigation revision，并在同一 revision 内生成语义快照、
最终页面状态和可选截图；Python 只接收该不可变观察值，不建立第二份 DOM 或页面真相。

### 显示一致性

应共享：

- 事件语义。
- 工具结果文本。
- diff 内容。
- 主题角色。
- 子代理和 usage 摘要。

允许差异：

- 键位。
- 折叠和滚动方式。
- modal 样式。
- 移动端抽屉和布局。

macOS Browser workspace 的应用级弹窗不属于这些几何差异：WebUI 以统一的
conversation-overlay 标记可见浮层，Apple 壳据此把完整聊天 WebView 临时提升到 Browser
上方并禁用 Browser 命中。提升期间 WebUI 的 Browser 预留轨道透明，仍挂载的 Browser 画面
保留在浮层遮罩之下。这个布尔信号仅控制本地视图层级和透明展示；弹窗内容仍由 WebUI 持有，
Browser 页面与会话状态均不被复制或重建。

### 展示状态不是运行真相

Frontend 可以缓存滚动位置、折叠状态、modal、选中项和从事件派生的卡片，但不能把这些缓存作为会话消息、工具状态、权限或 workflow 的权威来源。Web `SessionBridge` 的 block snapshot 用于重建显示；会话真相仍由 Conversation/Store 持有。

冷会话 dashboard 也遵守这条边界：Context/cache/cost 从 assistant usage metadata 与 trace
派生，Todo/Changed 从 tool result metadata 派生。缺失信息必须显示 unavailable 或明确的
whole-turn legacy 标签，不能用前端 sink 的构造默认值零填充历史事实。

### 命令展示与模型上下文分离

普通命令的展示结果只给用户看，不进入下一轮 LLM 上下文；提示词命令展开出的 prompt 才作为用户消息提交。这个边界避免 `/help`、`/cost` 或 session 列表污染模型输入。

## 边界与非目标

- Frontend 不拥有对话循环、工具调度、权限规则或停止判定。
- Frontend 不反射 engine 私有字段；需要新状态时先增加公开 query/admin service。
- WebUI REST/SSE 当前服务 bundled UI，不承诺第三方长期兼容；稳定公开协议需要独立版本和兼容策略后再建立。
- 移动和 Apple 壳不复制 agent runtime，只承接平台窗口、深链和本地集成。
- 跨前端一致性约束用户可见语义，不要求布局、键位或滚动方式完全相同。

## 关联参考

- 终端指南：[`../guides/using-cli-and-tui.md`](../guides/using-cli-and-tui.md)
- WebUI 指南：[`../guides/using-webui.md`](../guides/using-webui.md)
- 前端交互响应与运动：[`frontend-interaction-responsiveness.md`](frontend-interaction-responsiveness.md)
- 前端架构：[`../architecture/frontend-adapters.md`](../architecture/frontend-adapters.md)
- 状态所有权：[`../architecture/state-ownership.md`](../architecture/state-ownership.md)
- Web API 当前行为：[`../reference/webui-api.md`](../reference/webui-api.md)
