# 工具参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

工具系统负责把模型发出的 tool call 变成可审计、可取消、可授权的本地动作。core 通过统一的工具协议和 dispatcher 工作，不直接知道具体工具的实现细节。

## 快速索引

- 理解 dispatcher：见[工具执行路径](#工具执行路径)。
- 查能力和权限：见[能力分级](#能力分级)与[权限与安全](#权限与安全)。
- 理解 rewind 文件安全网：见[工作区快照与 turn 回退](#工作区快照与-turn-回退)。
- 查内置工具：见[ToolRegistry.build 内置工具](#toolregistrybuild-内置工具)。
- 查浏览器工具：见[浏览器工具](#浏览器工具)。
- 贡献新工具：见[新增工具流程](#新增工具流程)。

## 核心概念

| 概念 | 说明 |
|------|------|
| Tool | 一个可调用能力，声明名称、schema、能力等级和执行函数 |
| ToolContext | 工具执行时需要的横切依赖，如 cwd、权限、追踪、abort signal |
| ToolDispatcher | 路由、参数校验、plan-mode gate、权限、事件、取消和错误包装中间件链 |
| ToolResult | 工具返回给模型和前端的结构化结果 |

## 工具执行路径

一次工具调用大致经过：

```text
模型请求 tool call
  -> ToolDispatcher 查找工具
  -> 用工具 Args schema 校验参数
  -> 检查 plan mode、权限和能力策略
  -> 调用 tool.run(args, ctx)
  -> 包装 ToolResult
  -> 发出 TOOL_START / TOOL_END 事件
  -> 结果回到模型上下文和前端
```

这条路径的目标是把横切关注点集中起来。单个工具应该尽量只写自己的业务逻辑，不要各自实现权限、取消、事件发送和错误格式。
文件路径边界和 read-before-write / stale-read 约束由文件类工具通过 `ToolContext.tracker`
执行；dispatcher 只提供统一上下文和外层安全链。

## 能力分级

工具按文件系统风险分级：

- `READ_ONLY`：只读。
- `CONTROLLED_WRITE`：受控写，如精确 edit。
- `ARBITRARY_WRITE`：任意写。
- `PROCESS`：可启动进程或更高风险动作。

网络能力是独立布尔属性，不混入文件系统线性等级。

| 工具类型 | 推荐等级 | 说明 |
|----------|----------|------|
| 读文件、glob、grep | `READ_ONLY` | 不修改工作区 |
| 精确 edit | `CONTROLLED_WRITE` | 写入受 old/new、读后写和路径规则约束 |
| write/delete | `ARBITRARY_WRITE` | 可创建、覆盖或删除文件 |
| shell | `PROCESS` | 可执行任意进程，风险最高 |
| web search/fetch | `READ_ONLY` + `needs_network=True` | 不写文件，但有网络出站 |
| browser | `READ_ONLY` + `needs_network=True` | 不写本地文件；交互类操作可能触发远端副作用，由 plan-mode gate 和权限策略约束 |

## 权限与安全

工具执行前应经过权限和能力检查：

- plan 模式会拦截写入类工具。
- 子代理按角色 tier 过滤工具。
- 写文件应配合读后写追踪。
- shell 需要区分只读命令和有副作用命令。
- 浏览器交互类工具虽然不改本地文件，但可能新建或导航页面、点击按钮、提交表单或执行页面 JS；plan 模式会拦截 `browser_new_page`、`browser_navigate`、`browser_click`、`browser_type`、`browser_select`、`browser_keyboard` 和 `browser_evaluate`。
- 浏览器截图在 text-only 模型下如需自动委托 `read_image`，会先通过 permission port 检查；策略拒绝时不会把截图交给视觉模型。

权限不是工具本体的职责。工具通过 `ToolContext` 拿到 port 和运行时依赖；具体审批由终端或 Web 前端实现。

## 工作区快照与 turn 回退

`GitShadowSnapshot` 使用独立 `GIT_DIR` 捕获工作区树，不修改用户仓库历史。
WebUI 在用户 turn 开始前强制捕获精确边界；即使连续 turn 的文件树相同，也可
以不同 `rewind_id` 指向同一不可变 tree。恢复使用 shadow index 同步工作区，
包括删除该 turn 后新增的文件。文件恢复失败时，Conversation 不会被截断。

全局 `snapshot_enabled` 仍控制通用 engine/CLI 的可选 `/rewind` 装配；WebUI
为支持已发送输入的修改与撤销，会为自己的 session 单独保证 snapshot adapter
存在。

## ToolResult

工具结果应同时服务模型和前端：

- 成功时说明做了什么，必要时附结构化 metadata。
- 失败时说明为什么失败、已经尝试了什么、用户或模型下一步能做什么。
- 不要把长 traceback 原样灌给模型。
- mutation 工具应尽量提供 diff 或变更摘要。
- 图片类结果把 base64 放在 metadata 中，不把大图塞进 `output` 文本。

失败反馈不只是用户体验问题，它直接影响模型下一步能否自我修正。

### Web 检索输出

`web_search` 每条摘要会折叠空白并限制长度；模型可见文本保留标题、URL、可用的发布时间
和短摘要，不再回灌搜索 provider 的任意长内容。

`web_fetch` 对 HTML 响应会在工具边界移除 script/style/SVG 等页面代码并提取可读正文，
再应用 `max_length` 和 TTL/LRU 缓存；JSON、Markdown 与普通文本保持原始正文。若 HTML
只有客户端渲染壳、没有足够可见文本，结果会明确提示改用 browser 渲染，避免把原始框架
代码当成一手证据。

## 内置工具类别

- 文件类：read、write、edit、glob、grep。
- shell：命令执行、取消和后台化。
- web：搜索和抓取。
- browser：会话级浏览器自动化。
- planning：think、todo、plan 相关工具。
- runtime：agent、job、read_image、generate_image 等扩展能力。

## ToolRegistry.build 内置工具

下表只列 `ToolRegistry.build()` 直接实例化的内置工具；生产 `EngineBuilder`
会再注册 `agent`、`JobOutput`、`JobList`、`JobGet`、`JobStop` 和 `read_image`，
MCP/skill/plugin 等扩展也可以在运行时注册额外工具。

| 工具 | capability | network | 说明 |
|------|------------|---------|------|
| `AskUserQuestion` | `READ_ONLY` | no | Ask the user one or more structured multiple-choice questions. Use only when blocked on a decision that is genuinely the user's to make. |
| `EnterPlanMode` | `READ_ONLY` | no | Enter planning mode before implementation. |
| `EnterWorktreeMode` | `READ_ONLY` | no | Switch the session into isolated worktree mode. |
| `ExitPlanMode` | `READ_ONLY` | no | Exit planning mode with the proposed plan. |
| `ExitWorktreeMode` | `READ_ONLY` | no | Leave isolated worktree mode and finalize the changes. |
| `SendMessage` | `CONTROLLED_WRITE` | no | Compatibility alias for `SessionTell`. |
| `SessionAbort` | `CONTROLLED_WRITE` | no | Abort a managed worker session. |
| `SessionInbox` | `READ_ONLY` | no | Read messages from managed worker sessions. |
| `SessionList` | `READ_ONLY` | no | List known session ids from the active Store. |
| `SessionSpawn` | `CONTROLLED_WRITE` | no | Spawn a managed worker session. |
| `SessionTaskUpdate` | `CONTROLLED_WRITE` | no | Update manager-visible task status for a session. |
| `SessionTell` | `CONTROLLED_WRITE` | no | Send a message to a managed worker session. |
| `bash` | `PROCESS` | no | Execute a bash command in a shell session. |
| `browser_click` | `READ_ONLY` | yes | Click an element by CSS selector or coordinates. |
| `browser_close` | `READ_ONLY` | yes | Close the browser session and release resources. |
| `browser_close_page` | `READ_ONLY` | yes | Close one browser page by id, or the active page. |
| `browser_evaluate` | `READ_ONLY` | yes | Execute JavaScript in the page and return a JSON result; use top-level `return await ...` for asynchronous work. |
| `browser_get_content` | `READ_ONLY` | yes | Get text/HTML/markdown content of the current page. |
| `browser_keyboard` | `READ_ONLY` | yes | Press keyboard keys/shortcuts in the browser. |
| `browser_list_pages` | `READ_ONLY` | yes | List open browser pages and the active page. |
| `browser_navigate` | `READ_ONLY` | yes | Open a URL in the browser. |
| `browser_new_page` | `READ_ONLY` | yes | Create and activate a browser page. |
| `browser_screenshot` | `READ_ONLY` | yes | Capture a screenshot of the current page. |
| `browser_scroll` | `READ_ONLY` | yes | Scroll the page or a specific element. |
| `browser_select` | `READ_ONLY` | yes | Select option(s) from a select dropdown. |
| `browser_switch_page` | `READ_ONLY` | yes | Activate an open browser page by id. |
| `browser_type` | `READ_ONLY` | yes | Type text into an element identified by a CSS selector. |
| `browser_wait` | `READ_ONLY` | yes | Wait for an element/navigation/network condition. |
| `delete_file` | `ARBITRARY_WRITE` | no | Delete a file or directory. |
| `edit_file` | `CONTROLLED_WRITE` | no | Replace a string in a file. |
| `generate_image` | `PROCESS` | yes | Generate an image from a text description. |
| `generate_uuid` | `READ_ONLY` | no | Generate one or more UUIDs. |
| `glob` | `READ_ONLY` | no | Find files matching a glob pattern. |
| `grep` | `READ_ONLY` | no | Search file contents with a regex. |
| `hash_text` | `READ_ONLY` | no | Compute a cryptographic hash of a text string. |
| `memory_read` | `READ_ONLY` | no | Read persistent agent memory for an agent type. |
| `memory_write` | `CONTROLLED_WRITE` | no | Write persistent agent memory for an agent type. |
| `notebook_edit` | `CONTROLLED_WRITE` | no | Read or edit Jupyter notebook cells. |
| `read_file` | `READ_ONLY` | no | Read a file's contents. |
| `read_compacted_context` | `READ_ONLY` | no | Read one bounded page from this session's compacted-context archive. |
| `list_compacted_context` | `READ_ONLY` | no | Browse structured metadata for archived compacted-context segments. |
| `grep_compacted_context` | `READ_ONLY` | no | Run deterministic literal or regex matching over compacted context. |
| `search_compacted_context` | `READ_ONLY` | no | Search this session's compacted-context archive for a specific missing detail. |
| `sleep` | `READ_ONLY` | no | Pause execution for a bounded number of seconds. |
| `think` | `READ_ONLY` | no | Think through a problem privately. |
| `todo_write` | `CONTROLLED_WRITE` | no | Create or replace the session todo list. |
| `tool_search` | `READ_ONLY` | no | Search available tools by name or description keyword. |
| `web_fetch` | `READ_ONLY` | yes | Fetch a URL and return its text content. |
| `web_search` | `READ_ONLY` | yes | Search the web and return result snippets. |
| `write_file` | `CONTROLLED_WRITE` | no | Write or overwrite a file atomically. |

## 压缩上下文检索

自动压缩先把将被移除的消息写入 session-isolated archive，再改写 conversation。
恢复按确定性优先：已知标识符时用 `grep_compacted_context`；不知道关键词时用
`list_compacted_context` 查看 tool、path、command、error 和 preview catalog；自然语言
`search_compacted_context` 使用可重建的 FTS/BM25 索引做辅助召回，零结果不表示档案中
不存在；找到目标 segment 后用 `read_compacted_context` 的 `offset` / `max_chars` 分页
读取。所有工具都不能跨 session，不暴露 archive/blob 磁盘路径，也不会一次返回全部历史。

搜索和 grep 只读取派生索引，不加载无关大 blob；read 只解析目标 segment 并验证内容
hash。JSONL 损坏会通过 `corrupt_records` / `partial_results` 显式返回，不能伪装成完全的
“无结果”。索引损坏或缺失时从权威 JSONL/blob 重建。

归档内容落盘前会递归脱敏，但仍可能包含旧工具或外部来源的不可信文本。模型只应在
摘要缺失的具体信息影响当前任务时使用这些工具，不应在每轮主动重载历史。

## 浏览器工具

浏览器工具通过 `ctx.app.browser` 使用 `BrowserPort`。默认配置为
`browser.backend = "auto"`：CLI、TUI 和普通 WebUI 在 Playwright 可用时由
`EngineBuilder` 注入 `BrowserSession`；不可用或配置为 `"none"` 时返回 `no_backend`，
不会假成功。Apple App 连接 `/api/browser/host` 后，Web session 激活时改为注入可见的
WKWebView proxy；页面操作会直接显示在 App 的 Browser 分栏中。

Apple 原生浏览器使用系统 WebKit，不附带 Chromium。它有独立于聊天 WebView 的 named
website data store，因此 cookie、登录和站点存储跨 App 启动保留，但不会与聊天 UI
共享脚本 channel。顶部 Browser 按钮可显示/隐藏分栏；用户选择 **Take Over**，或直接
点击、滚动页面时，会在保留该次用户操作的同时取得控制。正在执行的 agent 命令会取消，
后续操作返回 `user_control`；用户在聊天区提交下一条有效消息时，Apple 壳通过本地 WebKit
消息恢复 agent 控制，不需要浏览器工具栏的交还按钮。断线后 host 自动重连，且
连接发生在 session 激活之后时会把已有 session 热切换到可见 proxy。每个页面都有稳定的
`page_id`；没有显式 `page_id` 的页面操作使用当前活动页，显式目标会同时激活该页。
Apple 分栏显示标签条，网页的 `target="_blank"` 也创建并激活受同一状态管理的新标签。
分栏隐藏后页面、导航历史和活动页继续存在，`browser_list_pages` 不会把它重新打开；其它
页面操作会按需显示分栏。`browser_close` 对原生 host 只隐藏分栏但不删除页面或持久 profile，
Playwright backend 则释放全部 page/context/process。`browser_close_page` 关闭最后一个原生
页面时会隐藏分栏；下一次 launch 或用户打开 Browser 时创建新的空白页。

当前工具集：

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `browser_list_pages` | 列出页面、活动页、revision 和 loading 状态 | 无 |
| `browser_new_page` | 新建并激活页面 | `url`、`wait_until`、`screenshot` |
| `browser_switch_page` | 按稳定 id 激活页面 | `page_id`、`screenshot` |
| `browser_close_page` | 关闭指定页或活动页 | `page_id` |
| `browser_navigate` | 打开 URL | `page_id`、`url`、`wait_until`、`screenshot` |
| `browser_screenshot` | 截取当前页面或元素 | `page_id`、`full_page`、`selector` |
| `browser_click` | 点击元素或坐标 | `page_id`、`selector`、`x`、`y`、`button`、`double`、`screenshot` |
| `browser_type` | 输入文本 | `page_id`、`selector`、`text`、`clear`、`press_key`、`screenshot` |
| `browser_get_content` | 提取页面内容 | `page_id`、`selector`、`mode` (`text`/`html`/`markdown`) |
| `browser_scroll` | 滚动页面或元素 | `page_id`、`direction`、`amount`、`selector`、`screenshot` |
| `browser_wait` | 等待页面或元素条件 | `page_id`、`condition`、`selector`、`timeout` |
| `browser_select` | 操作 `<select>` | `page_id`、`selector`、`label`、`value`、`index`、`screenshot` |
| `browser_keyboard` | 按键或快捷键 | `page_id`、`key`、`selector`、`screenshot` |
| `browser_evaluate` | 执行页面 JavaScript | `page_id`、`script` |
| `browser_close` | 关闭会话浏览器 | 无 |

参数在 tool schema 边界严格校验：`wait_until`、点击按键、内容模式、滚动方向和 wait
condition 只接受列出的 token；element wait 必须有 selector；`browser_select` 必须且只能
提供 `label`、`value`、`index` 之一。模型或 provider 序列化出的空白 `label` / `value`
按未提供处理，避免一个有效选择值被空可选字段误判为歧义；两个非空选择方式仍会拒绝。
带 `screenshot` 的页面动作只接受 `auto`、`always`、`never`，默认 `auto`；旧调用中的
布尔值仍在 schema 校验前兼容为 `true=always`、`false=never`。这个参数只控制是否附带视觉
反馈，语义快照和最终页面状态始终返回。原生与 Playwright backend 使用相同约束和错误语义。

`browser_evaluate.script` 接受同步 JavaScript 表达式，或带显式顶层 `return` 的函数体。
异步读取必须写成 `return await ...`，例如
`return await fetch(url).then(response => response.json())`；不要把 `fetch(...).then(...)`、
async IIFE 或其它 Promise 直接作为顶层表达式返回。adapter 会等待显式异步函数体，但最终值
必须是 JSON 可序列化的 `null`、boolean、有限 number、string、array 或 object；Promise、
function、DOM node、循环对象、`NaN`/Infinity 等不属于工具契约；原生 WebKit 会将此类
返回值归一化为 `unsupported_result`，core 也会拒绝 backend 泄漏的非 JSON 值。成功结果的
模型可见文本是严格 JSON，不使用 Python repr。Playwright 以 `browser.timeout_ms`
限制 evaluate；原生 WebKit 以 30 秒限制异步函数体。未决 Promise 返回稳定的
`browser_timeout`，且 timeout 后仍可继续执行下一条浏览器命令。

截图会先经过护栏压缩：长边限制在 2000px 内，并用 JPEG 质量阶梯压到 150 KB 以内。WebUI 会把截图渲染为可点击放大的缩略图；成功时 metadata 包含：

- `images`: base64 字符串列表。
- `image_media_type`: 通常为 `image/jpeg`。
- `full_page` / `screenshot_selector` / 压缩信息。

自动观察截图和显式 `browser_screenshot` 在取图前都执行有上限的视觉就绪等待：先等
`DOMContentLoaded`（若尚未发生），并行 best-effort 等待字体和最多 12 个首屏未完成图片，
资产等待最多 1100ms，最后再跨两个 `requestAnimationFrame` 让布局与绘制落地。它不等待
`networkidle`；绘制帧本身也有 150ms 兜底，因此总等待不超过约 1.25 秒，长连接、广告或
持续埋点不会无限拖住工具。就绪检测失败时仍尝试截图。
自动观察在等待后会重读语义快照，使模型文字与随附截图尽量来自同一绘制状态。截图前后的
page revision 必须一致，变化时重试一次，持续变化返回
`browser_state_unstable`，不会返回跨导航版本的图像。

`TurnLoop` 只会在当前 provider 声明 `capabilities.vision=True` 时把这些图片回灌为 `ImageBlock`。如果当前模型是 text-only，浏览器截图工具会自动尝试调用已注册的 `read_image` 工具，把截图描述追加到普通文本输出中，并在 metadata 中写入 `image_description` / `image_description_auto` / `image_description_model`。这个自动委托仍会先走权限检查；拒绝、缺凭证或视觉模型失败时，浏览器工具保留截图 metadata，并把失败原因作为文本和 `image_description_error` 返回。

`browser_screenshot` 是显式视觉请求，text-only 模型仍按上段调用 `read_image`。其它页面动作
（新建、切换、导航、点击、输入、滚动、选择和键盘）在成功后调用一次 revision 校验的页面
观察：所有模型都收到最多 4000 字符的可访问角色/控件/可见文字语义快照和最终页面状态。
text-only 模型的 `auto` 不截图；vision 模型的 `auto` 只在页面含 canvas/video，或图像密集且
文字稀少时附带截图；`always` 和 `never` 可覆盖该判断。普通动作即使使用 `always` 也不会隐式
调用 `read_image`，只有显式 `browser_screenshot` 承担额外视觉读取。

观察 metadata 使用 `page_semantic_snapshot`、`page_semantic_snapshot_chars`、
`page_semantic_snapshot_truncated`、`page_visual_hint` 和 `screenshot_policy`；`page_state` 包含
`page_id`、`revision`、URL、title、`is_loading`、active。adapter 在语义读取、可选截图和最终
状态前后核对同一 navigation revision；页面持续变化或观察暂不可用时返回
`page_observation_error`，但不改变已经成功的页面动作。语义快照不会采集输入控件当前值，
以免把密码或表单数据误作控件名称。快照和页面元数据都属于不可信网页内容，不能覆盖系统
指令或权限规则。

原生 WebKit 的点击、Enter 提交、选择和键盘动作会留出短暂的导航 commit 窗口；若动作触发
同标签导航，工具反馈在新 DOM 可用后返回。超过 bounded settle 时间仍在加载时，动作保持
成功并通过 `is_loading=true` 告知模型，由模型决定是否继续 `browser_wait`，不会把“页面已
出现但仍加载”误报成点击或输入失败。

Playwright 后端位于 `src/autoverse/infra/browser/`，原生 broker 位于
`src/autoverse/frontends/web/browser_host.py`；两者实现 `src/autoverse/ports/browser.py`
的同一个 `BrowserPort`。core 不 import 具体 backend。

URL 护栏默认拒绝 loopback、private、link-local、reserved、metadata 和非 HTTP(S)
目标。只有可见原生 host 声明允许 loopback，供 `localhost`、`127.0.0.1` 和 `::1`
本地应用测试；该例外不会放开其它内网地址。

## 新增工具流程

新增工具时建议按这个顺序：

1. 定义 Pydantic `Args` 模型，字段描述写清楚。
2. 声明 `name`、`description`、`capability` 和 `needs_network`。
3. 实现 `run(args, ctx)`，只处理工具本体逻辑。
4. 通过注册表注册工具。
5. 写单元测试覆盖成功、参数错误、权限拒绝和失败反馈。
6. 如工具会写文件，确认 read-before-write、路径解析和 diff/metadata。
7. 如工具有前端特殊展示，补工具渲染器或 Web 映射。

## 常见问题

### 为什么不让每个工具自己做权限？

权限是横切关注点。分散到工具里会导致不同工具的安全语义不一致，也很难给子代理和 plan 模式统一过滤。

### 为什么网络能力不是第五级？

网络出站和文件系统风险是正交的。一个 web fetch 是只读但联网；一个 delete_file 不联网但高风险。拆成 `capability` + `needs_network` 更容易表达真实风险。

### shell 为什么是最高风险？

shell 可以读、写、删、启动进程、访问网络，也可能绕过工具系统内建的路径和 diff 约束。因此它必须按 `PROCESS` 处理，再由权限策略进一步分类命令本身。

## 开发入口

- `src/autoverse/core/tools/registry.py`
- `src/autoverse/core/tools/dispatcher.py`
- `src/autoverse/core/tools/builtin/`
- `src/autoverse/types/capability.py`

设计背景见 [`../design/tools-and-permissions.md`](../design/tools-and-permissions.md)。
