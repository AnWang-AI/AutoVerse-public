# 前端 Adapter

> 当前事实来源：`src/autoverse/`、`pyproject.toml`、import-linter 和 contract tests。
> 维护规则：分层、端口、入口、导入边界或真相源位置变化时必须同步。

AutoVerse 有多个前端，但它们都应该只是 adapter：采集输入、消费事件、处理交互回路，不拥有领域逻辑。

## 前端类型

| 前端 | 适用场景 | 主要代码 |
|------|----------|----------|
| CLI | 一次性脚本、`--print` | `frontends/cli/` |
| TUI | 默认交互终端 | `frontends/repl/app.py` |
| Line REPL | 弱终端兜底 | `frontends/repl/line_repl.py` |
| WebUI | 浏览器会话、文件预览和原生 WebKit host bridge | `frontends/web/` |
| Gateway | IM、平台消息和无人值守入口 | `frontends/gateway/` |

## 共享内容

这些能力应尽量共享：

- 命令注册表。
- 事件类型和 payload。
- 工具渲染文本。
- 主题角色和基础配色。
- 权限/计划/问答的交互语义。

## 不共享内容

这些通常由每个前端自己实现：

- 布局。
- 键盘和鼠标交互。
- modal、toast、select 的具体呈现。
- 滚动、折叠和虚拟列表。
- Web 的 HTTP/SSE 传输。

共享渲染可以产生子代理的角色语义，但不应把终端深色主题的前景 hex 烧进
Web block。Web adapter 传递角色标识，浏览器再用当前浅色/深色 token 渲染；这些
token 是展示派生状态，不改变 sub-agent 运行语义。

## WebUI 的特殊点

WebUI 后端把 engine/session/approval/file preview 包装成 REST + SSE。浏览器只拿结构化块和状态更新，不直接调用 core。
对话 Markdown 中的本地文件链接、文件形态 inline code，以及 dashboard 的 Changed/Memory
条目共用一个 `data-open-file` 展示动作和右栏预览器。目录与写操作仍只在 session cwd；
cwd 外只读预览由 Web session 从已加载 block 与侧栏状态派生精确文件 capability，server
再次按 resolve 后的路径校验，不能用任意绝对路径扩大成本机文件浏览。该 capability 是
当前 view session 的派生授权，不写入 Conversation/Store，也不成为文件系统真相。

Apple 壳还建立独立的 `/api/browser/host` WebSocket，把聊天 WebView 之外的 WKWebView
注册为 `BrowserPort` host。聊天页与工具浏览器使用不同 `WKWebViewConfiguration` 和
website data store；网页不能取得 broker channel。server 只传递带 command id、session id、
action 和 JSON params 的窄命令，不镜像 DOM 或 cookie。最近连接的一个 host 拥有共享可见
页面集合；`BrowserHostController` 为每个 WKWebView 分配稳定 `page_id`，持有标签顺序和
活动页及单调导航 revision，并把 `target="_blank"` 纳入同一集合。`observe` 命令在 host
内生成受限语义快照并按策略附带截图，前后 revision 不一致时重试而不拼接跨页面状态；
Python 只接收共享 `BrowserPageObservation`。host 断开时 pending command 以
`browser_unavailable` 结束，Python broker 不复制页面状态。

`evaluate` command 在 host 内区分同步表达式与带顶层 `return await ...` 的异步函数体，后者
有 30 秒上限。host 负责把 WebKit unsupported-result 归一化为稳定错误种类，并在 WebSocket
发送前验证整个 result 可编码为 JSON；Python broker 只关联 command 和透传结构化错误，
不解析系统本地化的 WebKit message。这样 Promise、DOM node 等原生对象不会越过 frontend
adapter 边界，超时也不会永久占住串行 command loop。

macOS 打开工具浏览器时，Swift 壳向聊天 WebView 下发派生的
`native-browser-workspace` 展示状态。WebUI 仍以完整窗口尺寸参与布局，持久 dashboard 宽度
仍是普通右栏的真相；Swift 壳另持有本次 Browser 展示的临时宽度，并把 Browser pane 覆盖在
同一轨道上。bridge 在进入时快照 dashboard 可见性和宽度，退出时恢复；Browser 拖拽和
最大化只以进程内单向预览更新 WebUI CSS 轨道，不保存，也不作为 `NativeChromeState` 的
右栏宽度回传。这样缩窄 Browser 时不会露出可交互的 dashboard，也不会让测量回包污染
底层布局。
用户请求文件预览时，聊天 WebView 通过独立的本地展示消息让原生 Browser 收起，随后同一
dashboard 轨道显示文件；Browser 页面、标签和控制权状态仍保留，文件路径不经过该消息。
对话中的显式 Markdown HTTP(S) 链接、裸 URL 和完整 inline-code URL 由 Web adapter 统一
渲染为标准链接；Apple
聊天 WebView 点击后通过单用途本地消息交给 `BrowserHostController`。host 复用
`BrowserURLPolicy`，为合法地址新建并激活独立页面，同时切换为用户控制；普通 WebUI 没有
该 handler 时继续执行 `_blank`。URL 不进入 Conversation，也不扩展 BrowserPort 或 Web API。
Browser 宽度算法默认使用窗口的 50%，以 `600px`–`900px` 限制理想值，以 `320px` 保护
工具栏下限，并且不设固定最大值：中心会话栏低于 `520px` 前，扩大手势以幂等
`collapseSessions` 展示动作收起 Sessions；随后以 `520px` 中心功能下限计算右栏动态上限。
所有由主对话区拥有的固定浮层通过统一的 conversation-overlay 展示契约接入，不按弹窗 id
维护例外列表。macOS 聊天 WebView 以 MutationObserver 将“是否存在可见浮层”作为本地展示
信号交给 Swift 壳；Browser 激活时，壳将完整聊天 WebView 临时提升到 Browser pane 上方，
并关闭 Browser 和分隔条命中。设置、新建会话、图片预览、退出 worktree 和文件操作弹窗
因此使用与普通 WebUI 相同的整窗内容几何。聊天 WebView 的 under-page surface 为透明，浮层
激活时 WebUI 只隐藏自己的 Browser 预留 rail；半透明遮罩仍覆盖该区域，而底层 Browser 画面
继续可见。关闭后只恢复视图层级，Browser 页面集合和控制权
不变。该消息不携带会话数据、弹窗内容或 Browser 宽度，也不成为持久状态；系统文件选择器
和原生连接设置仍由 Apple 壳拥有系统级呈现。
Swift 拖动预览在发出收起请求后立即按“左栏已收起”计算。工具栏最大化保存当前 Browser
宽度作为还原点并使用动态上限；窗口缩放时最大化宽度继续跟随，拖动则清除最大化状态。
分隔线的命中所有者是覆盖标题栏与正文的 40px AppKit splitter；它显式接管完整矩形并在
嵌入式 `WKWebView` 之上保持原生鼠标捕获，SwiftUI 绘制居中的单像素边界，原生层补充
常驻短握柄和悬停反馈。macOS 壳隐藏 WebUI 折叠态的
重复边缘按钮，由原生标题栏提供 Sessions 和设置入口；独立 WebUI 的入口不变。
WebUI 自身的 dashboard 拖动仍使用相同两级中心阈值，但保持自己的持久宽度。Browser 初次
展示也检查舒适阈值；只恢复由 Browser 自动收起且未被用户再次改变的 Sessions。用户显式
重新展开 Sessions 时按目标左栏几何重新 clamp Browser，不突破中心功能下限。
Browser pane 常驻视图树，只用共享 `panel` motion 做合成层位移和透明度变化，因而打开时
不会重建聊天布局或重新触发响应式断点。该状态不改变会话、BrowserPort 或工具控制权的所有者。
Apple host 还从当前串行 command 派生一个不持久化的 Browser activity：忽略只读
`list_pages`，其余命令开始时设置动作；完成、失败或取消后按 token 保留到至少 800ms 的展示
下限再清除，后续命令会替换旧 token，用户接管或断线则立即清除。SwiftUI 只消费该派生值
绘制控制权描边、固定宽度动作胶囊、局部命令反馈和状态文字，不新增 WebSocket 字段或页面注入。

macOS SwiftUI 壳通过进程内 WebKit message handler 读取当前会话标题和可见栏几何，并把
侧栏切换、打开 WebUI 设置等窗口栏动作送回页面。标题栏桥是本地展示桥，不扩展 Web REST/SSE
协议，也不成为会话或运行状态的权威来源。用户接管工具浏览器后，聊天页提交下一条有效
消息会通过独立的进程内 handler 通知壳恢复 agent 控制；该瞬时用户意图不复制消息内容，
也不经过 Web REST/SSE 或 BrowserPort。聊天链接的本地 handler 同样只传递一次点击的
HTTP(S) 目标，由原生 URL policy 决定是否建页。侧栏布局一次提交最终 grid，Web 只在合成层
播放面板位移，并在同一脚本任务内同步发送 start 目标。Swift 标题栏轨道直接提交目标几何，
只有左右面板表面使用相同 transform/opacity token；原生按钮不预先猜测 Web 可见性，也不对
track width 做第二条隐式动画。ResizeObserver 回调合并为稳定态消息，不逐帧驱动 SwiftUI
布局。Web 与 SwiftUI 的 press/hover/state/panel/activity 时长由 `design-tokens/motion.json` 生成。
每次 panel 动作携带 action id、目标几何和 motion token；Swift 只接受最新 action 的 settled，
macOS 用 signpost 记录 native input、Web dispatch、start 和 settled。

恢复控制后，Web session 在开始 turn 前对原生 host 做一次有上限的只读页面列表刷新。
proxy 比较页面集合、活动 id 与每页 revision/URL/title/loading 的 compact 快照；标签开关、同 URL
reload、同标签导航、重定向和异步加载完成都会产生带相关 page id 的隐藏 request context。它不会改写
用户正文或新增 Web API 字段；模型收到通知后先重新列页，再对有目标要求的动作使用显式
`page_id`。因此可见页面变化属于可解释的 host 状态，而不是 agent 眼中的随机上下文漂移。

打开会话先创建不含 engine 的 view session：catalog 提供 cwd/rail summary，
`SessionHistoryPort` 只反向读取最近两个完整 turn。`SessionBridge` 的首次 SSE snapshot
再按 200 block/128 KiB 限制投影；旧 turn 由 history route 按 cursor 读取。发送给浏览器
的图片变成 session-scoped resource reference，单个超预算块变成可显式展开的预览；
超大 turn 保留用户消息和最终回答，只延迟加载中间执行过程。浏览器的选中/pending、
分页 cursor、滚动/折叠缓存和 keyed/virtualized DOM 都只是派生视图。

右侧栏不是只恢复可见 tail：Todo/Changed 从 Store 中持久化的 tool result metadata
重放，精确 Context 从 assistant message 的 per-request usage metadata 重放，累计 cache/cost
可由 trace 补齐。旧 trace 只有 whole-turn usage 时，adapter 必须把它标成 legacy turn
aggregate 并禁止计算 context-window 百分比；没有可靠恢复来源时显示 unavailable，不能把
新建 sink 的初始零值当作历史事实。历史 session 的 model/cwd 也来自 SessionMeta，进程级
默认只用于真正的新 session。

消息、命令、compact/clear/rewind 等需要 runtime 的动作才调用 lazy engine activation。
view 已加载快照用于初始 Conversation，普通 turn 在 append 前从 Store 同步；非 turn 的
历史修改先调用公开 `sync_history`。`SessionListQuery` 从 `SessionCatalogPort` 一次取得
summary，catalog 只解析 mtime 变化的 Store 文件，不为每一行重复读取 meta。

已发送用户块携带持久化的 `rewind_id`。浏览器的“修改”和“撤销”都调用 Web
session 的 turn rewind：session 负责停止并等待运行任务，engine 负责以同一个
turn 锚点恢复文件并重写 Conversation，随后 Web session 从持久化历史重建
`SessionBridge` 块。“修改”把服务端返回的原始文本和图片重新装入 composer；
“撤销”只保留回退后的空 composer。

停止阶段同时拒绝未决 approval、解除 `AskUserQuestion` 等待并清空 queued
follow-up，避免 turn 在 UI 输入上悬挂。重建后当前 session 会立即标记为已查看；
plan/goal checkpoint、自动标题和 rail 活动元数据由 engine 与 store 恢复，手动
锁定标题仍以用户命名为准。

## Gateway 的特殊点

Gateway 是外部平台前端。`GatewayRunner` 只面向 `EngineHandle`、`EventSink` 和 `PermissionPort` 等公开 port，把平台消息正规化为 engine 输入，再把事件渲染回平台消息。无人值守权限默认 fail-safe，具体能力和管理边界见 [`../reference/extensions.md`](../reference/extensions.md)。

## 设计约束

- 前端不通过 `getattr(engine, "_private")` 读取私有字段。
- 前端不重新实现工具调度和停止判定。
- 前端可以缓存展示所需派生状态，但真相仍在 core/store。
