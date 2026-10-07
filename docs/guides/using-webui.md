# 使用 WebUI

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

WebUI 是 AutoVerse 的浏览器前端。它通过 FastAPI 提供 HTTP 路由和 SSE 流，后端复用终端前端的渲染和事件消费逻辑，浏览器负责布局与交互。

## 前置条件

- 已安装 `web` extra，或开发环境安装 `dev,web`。
- 浏览器能访问本机 server，并已确定默认 cwd 和模型配置。

## 安装依赖

```bash
pip install -e ".[web]"
```

开发时通常安装完整依赖：

```bash
pip install -e ".[dev,web]"
```

## 启动

```bash
autoverse-web
```

默认入口由 `src/autoverse/frontends/web/server.py` 提供。静态页面位于 `src/autoverse/frontends/web/static/`。

## 外观

首次打开且没有已保存偏好时，WebUI 使用浅色外观。顶部工具栏的外观按钮可在浅色和
深色之间切换，选择保存在当前浏览器或 WebView 的本地存储中。macOS App 会同步 WebUI
外观到原生标题栏，使窗口标题和原生控件在两种背景下保持足够对比度。
子代理卡片的角色色也会随外观切换：标题和树线保留角色辨识，工具数与耗时继续使用
次级文本色，不会在浅色背景上沿用深色主题的低对比度前景色。

macOS App 的顶部栏与正文三栏对齐：左侧按钮折叠或展开会话栏，中间显示当前会话标题并
可打开 WebUI 设置，右侧提供连接设置和刷新。会话改名或切换后，中间标题会自动同步。
产品名位于左侧栏；普通会话正文不会重复显示品牌横幅。

Apple App 右侧标题栏的 Browser 按钮打开原生浏览器分栏。该分栏使用系统 WebKit，
并按当前系统 Safari/WebKit 版本补充兼容身份，避免 Google 等站点把普通 WKWebView
降级到旧版页面；UA 仍保留 AutoVerse 标识，不伪装为 Chrome。登录状态保存在独立的
AutoVerse browser profile 中。地址栏输入域名或明确的 HTTP(S) URL 时直接打开，输入
普通关键词时默认使用 Google 搜索。原生浏览器工具栏会跟随 AutoVerse 的浅色或深色外观，
地址栏采用接近桌面 Chrome 的较高圆角比例、较宽内边距和清晰字号，聚焦时显示主题强调色；
地址栏右侧的宽度按钮可在最大宽度和之前的 Browser 宽度间切换。栏宽较窄时接管操作会收拢
为图标，地址输入通过最小宽度和布局优先级保持可用，不会被两侧按钮挤扁。
地址栏下方的标签条支持新建、切换和逐页关闭。各标签使用铺满栏高、彼此零间距的连续分段样式，
以细分隔线区分相邻标签，选中项显示轻微强调色底纹和底部强调线；满高的新建按钮保持固定。
标签较多时活动项会自动滚动到可见区域，标签栏与原生工具栏共用同一主题和动效。切换不会重建
WKWebView，因此页面历史、表单内容和脚本状态都会保留；网页脚本修改标题后，标签文字也会
同步。网页以 `target="_blank"` 打开的内容会成为新标签。聊天回答里的显式 Markdown
HTTP(S) 链接、直接写出的裸网址以及代码样式中的完整网址都可点击；Apple App 会显示
Browser、创建一个新标签并
在其中打开该网址，不会覆盖当前标签。普通浏览器访问 WebUI 时仍使用标准新窗口行为。
长网址会在聊天区的可用宽度内连续换行；位于列表中时，续行保持正文的悬挂缩进，不会回到
项目圆点下方。不安全或非 HTTP(S) 地址不会通过这条入口打开。agent 通过稳定页面 id 操作指定
标签，没有指定时使用当前活动页。隐藏 Browser 不会关闭标签，之后 agent 的实际页面操作
会自动重新显示；只查询标签列表不会打开展示栏。关闭最后一个标签会收起 Browser，下次
打开时从新的空白页开始。
agent 调用浏览器工具时，导航、
点击、输入、滚动和截图会在这里可见。需要手动登录、验证码或检查页面时选择
**Take Over**；完成后在聊天区发送下一条消息，控制权会自动交还给 agent。手动控制期间
agent 浏览器调用会明确暂停并返回 `user_control`，不会与用户争抢页面；浏览器工具栏不再
常驻单独的交还按钮。直接点击或滚动网页也会自动接管，并且
第一次输入仍会送达页面；若 agent 命令正在等待或导航，会立即取消。Browser 状态栏显示
连接、最近动作和当前控制方。琥珀色细描边表示 agent 持有控制权；执行导航、点击、输入、
滚动或截图时，地址栏或页面边缘、活动标签圆点和具体动作文字会轻微呼吸。用户接管后提示
立即转为绿色。即使命令很快完成，动作提示也至少保留 800ms；工具栏固定宽度的 Take Over
区域会显示 `Clicking`、`Typing` 等动作，不会挤压地址栏。开启“减少动态效果”时只保留静态
颜色、描边与文字。Web server 短暂重启后
host 会自动重连，无需重启 App。
接管期间可以自由切换标签或在同一标签内打开其它地址：发送下一条消息时，agent 会收到
页面开关、活动 id、revision/URL/title/loading 变化的可信状态提示，先重新列出页面，再用明确的页面
id 继续目标操作，不会沿用切换或导航前的隐式假设。这个提示只进入模型运行上下文，不会
出现在聊天正文或改写你的消息。普通浏览器动作会直接返回同一页面 revision 的可访问控件、
可见文字和最终 URL/title/loading 状态。对 DSV4 Flash 等 text-only 模型，自动反馈不截图、
不等待额外视觉 API；视觉模型仅在图表、视频或图像密集页面自动收到截图。只有明确要求截图
分析时才无条件采集，并由 text-only 模型调用视觉读取。无需为了确认同一次点击再调用标签列表。
取图会先短暂等待 DOM、字体、首屏图片和两个绘制帧，总等待上限约 1.25 秒；持续网络连接
不会阻止截图。页面仍在切换时会重试一次，而不会展示不同导航版本拼出的截图。
新会话首次看到已有 Browser 页面时也会收到同步提示，不需要先发生一次标签切换。
由点击、回车或选择触发的页面跳转也会先等待一个有界的新 DOM 确认；慢页面会明确显示为
loading，而不会在页面已经出现时把整个交互报告成失败。
原生工具执行异步页面 JavaScript 时要求脚本使用顶层 `return await ...`，只返回 JSON 兼容
最终值，并以 30 秒为上限；不支持的 Promise/DOM 返回值会给 agent 稳定的修正提示，超时后
Browser host 仍能继续处理后续操作。
同一 server 只接受一个可见 browser host；后打开的 App 接管 host 后，原 App 会显示
“Browser host active in another app”并停止自动重连，避免两个窗口反复争抢页面。
在 macOS 上，Browser 原位占用 WebUI 右侧栏位置，拖拽和开合动画沿用同一套交互规则，
打开前后的 Sessions、窗口顶部、聊天正文和输入框保持原有位置与样式，不会触发移动端或
另一套紧凑布局。Browser 打开时记录并覆盖 dashboard，默认宽度是窗口的 50%，理想范围为
`600px`–`900px`；实际宽度仍会为聊天区让出必要空间。关闭后恢复 dashboard 打开前的可见性
和宽度，Browser 的拖动值不会改写 dashboard 的记忆宽度。拖动 Browser 左侧分隔条时，
浏览器正文和标题栏会在原生层同步跟手；底层布局只接收不保存的同步预览，因此向右缩窄时不会露出或
误选 dashboard。可见分隔线与标题栏边界连续为一条细线；跨在边界两侧、贯穿标题栏与正文
的 40px 原生拖拽热区即使覆盖 `WKWebView` 也会持续捕获指针，不会形成双线或夹缝；中央短握柄
常驻显示，悬停时会使用强调色反馈，边界两侧都可以直接按下并开始拖动。
macOS 原生标题栏已经提供 Sessions 开关和设置入口，因此壳内不会再显示 WebUI 折叠后的
重复边缘按钮；独立 WebUI 仍保留这些恢复入口。Browser 最小宽度为 `320px`，没有固定最大宽度：向左
继续拖大、使中间聊天区即将小于 `520px` 时会自动收起 Sessions；随后仍可继续拖大 Browser，
但聊天区始终至少保留 `520px`，避免正文和输入栏被挤坏。地址栏右侧的放大按钮可一步到达这个动态最大值，再次点击恢复放大前的
Browser 宽度；最大化时调整窗口会持续贴合新上限，手动拖动会退出最大化。窄窗口打开
Browser 时也会直接收起 Sessions。向右缩小 Browser 不会触发自动收起；由 Browser 自动
收起且用户未再次改变的 Sessions 会在 Browser 关闭时恢复。如果手动重新展开 Sessions，
Browser 会相应缩小，继续为聊天区保留 `520px`。
Browser 打开期间，主对话区拥有的设置、新建会话、图片预览、退出 worktree 确认，以及
文件重命名、删除确认和失败提示统一使用整窗内容浮层：弹窗打开时聊天 WebView 会覆盖 Browser，
浏览器画面仍在半透明遮罩下可见，但 Browser 保持挂载且不可交互；弹窗关闭后原标签、页面和
控制权状态原样恢复。这样弹窗不会被
剩余聊天宽度挤窄，也不会有关闭按钮藏到 Browser 后面。系统文件选择器和原生连接设置仍由
Apple 壳按系统界面展示。

原生 host 允许打开 `localhost`、`127.0.0.1` 和 `::1`，适合测试本地 Web 应用；
其它内网、link-local、metadata 地址以及 `file://` 仍被阻止。若工具提示
`browser_unavailable`，先在连接设置中重连 Apple App；不使用 Apple App 的 WebUI
会按 `browser.backend` 配置回退到 Playwright。

macOS 标题栏和 Web 面板共享 press/state/panel motion token，Browser 命令反馈使用较慢的
activity token。一次栏切换携带 action id；
WebUI 一次提交目标布局后，标题栏轨道同步到相同几何，标题栏面板表面与下方栏使用同一方向、
时长和曲线移动，不会再额外播放一次宽度动画。连续点击时只接受最新动作的 settled 几何，
过期回调不会让标题栏闪回。开启“减少动态效果”时取消位移/缩放，但选中和 pending 反馈仍
立即出现。

各滚动区域默认隐藏滚动条滑块；滚动、悬停滑块或拖拽时滑块显示，停止滚动后自动淡出。
滚动槽宽度保持不变，因此显隐不会引起会话正文、会话列表或右侧栏横向跳动。

## WebUI 能做什么

- 发送普通文本和带图片的消息。
- 已发送的用户指令提供“修改”和“撤销”：两者都会停止当前 turn，把工作区文件和模型上下文恢复到该指令发送前，并清除该指令及其后的回复；“修改”还会把原文字与图片放回输入框。
- 显示 assistant 流式回答、工具卡、diff、todo、子代理状态和用量信息。
- 处理权限审批、计划审批和 AskUserQuestion。
- 管理会话列表、会话状态和历史回放。
- 在 Apple App 中显示 agent 与用户共享、可随时接管的原生 WebKit 浏览器。
- 点击对话中的 HTTP(S) Markdown 链接、裸网址或代码样式网址，在原生 Browser 的新标签中打开。
- 浏览工作区文件并预览常见 artifact：UTF-8 文本、Markdown、CSV/TSV、图片、PDF，以及其它文件的 raw 打开/下载。文件树获得焦点后，`↑` / `↓` 在所有可见文件与文件夹间移动，`→` 展开文件夹或进入首个子节点，`←` 折叠文件夹或返回父级，`Enter` 打开文件或切换文件夹，`Home` / `End` 跳到首尾，`Escape` 关闭预览。选中文件会同步预览；选中文件夹保留原预览。预览面板可复制绝对路径，图片可全屏查看。对话中以 Markdown 链接或 inline code 明确展示的本地文件也可点击，在同一个右栏预览；Changed/Memory 条目使用相同入口。文件可以位于项目外，但只开放当前会话明确展示的那个文件，不开放其父目录或任意绝对路径。如果原生 Browser 正在显示，点击文件会收起 Browser 并保留其标签状态，让右栏文件预览立即可见。
- 在右侧栏显示已加载 skills/plugins，并通过共享 command/API 管理它们。

切换会话时，列表选中态会立即确认点击，旧会话内容会保留到目标 session 打开成功；
打开失败则回滚选中态并显示错误。打开阶段只建立 view session，不等待模型、工具或权限
engine；默认先读取最近两个完整 turn，首个 snapshot 最多 200 blocks/128 KiB。图片通过
session resource URL 懒加载。超大 turn 的问题和最终回答仍正常显示，中间工具执行过程
点击“执行过程”时才读取；滚动到正文顶部才读取更早记录，因此长 session 不需要先把
全部 JSONL 或 block 塞进 WebView。

会话分组的展开/折叠使用已经取得的 catalog summary，不会额外刷新 inventory；大列表和
已折叠旧 turn 会虚拟化 DOM。会话行与分组头是原生 button 语义，pressed、focus、pending
和 `aria-busy` 走同一状态机。每个 session 的滚动位置和已展开 turn 保存在有界本地视图
缓存中，切回时恢复；该缓存不保存 Conversation 真相。

设置面板先显示外壳，再并行读取模型、tier 和密钥状态；其中一个请求变慢时仍可关闭面板。

## 与 TUI 的关系

WebUI 不是另一套 agent。它只是同一个 core 的浏览器 adapter：

- 对话循环仍在 `core/loop`。
- 工具调度仍在 `core/tools`。
- 模型调用仍走 `infra/llm`。
- 会话、审批、渲染块由 Web adapter 转成 SSE/REST 交互。

差异主要在显示和交互：WebUI 有浏览器布局、文件预览和会话栏；TUI 有本地终端键位、Textual 组件和终端内联体验。

## Skills、plugins 和右侧栏

WebUI 右侧栏会随 session state 更新。除 Context、Directory、Memory、Todo、
Changed、Agents 外，当当前 session 加载了 skills 或 plugins 时，会显示
`Extensions` 面板。

重新打开历史会话时，Context、费用、cache、Todo 和 Changed 会从持久消息/trace 恢复。
`tokens` 表示最后一次模型 request 的精确 Context；非常旧的会话若只有整 turn trace，界面
会明确写 `tokens in last turn` 且不显示 window 百分比。若 usage 从未被持久化或 trace 已
禁用/删除，界面显示 `historical usage unavailable`，这表示数据不可恢复，不代表实际花费为零。
模型不在价格 registry 时费用也显示 unavailable，不会把未知价格写成 `$0.0000`。`tok/s`
只属于当前实时生成，冷打开后不显示是正常的。

该面板是只读状态镜像；WebUI 不维护第二份 skill/plugin 管理语义。管理动作走
与 TUI 相同的 `/skills`、`/plugin` 命令，或走 Web extension endpoints。完整命令
语法见 [`../reference/commands.md`](../reference/commands.md#skills-和-plugins)，
endpoint 表见 [`../reference/webui-api.md`](../reference/webui-api.md#extensions)。

## 图片输入

WebUI 可以采集文件选择、拖拽和剪贴板图片。支持能力取决于当前模型：

- vision 模型：图片作为 `ImageBlock` 直传 provider。
- 非 vision 模型：前端 adapter 可先经 `read_image` 工具生成文字描述，再把文字喂给主模型。

codec 层仍然 fail-fast：非 vision provider 收到原始图片块时必须拒绝。

## 相关文档

- [WebUI API 参考](../reference/webui-api.md)
- [事件、追踪与持久化参考](../reference/events-and-storage.md)
- [前端 Adapter 架构](../architecture/frontend-adapters.md)
- [前端设计](../design/frontend.md)
