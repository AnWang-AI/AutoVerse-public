# WebUI API 参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

以下端点来自当前 `src/autoverse/frontends/web/server.py`，服务 bundled WebUI；
第三方稳定兼容还不是正式承诺。默认响应为 JSON，错误使用 FastAPI
`HTTPException` 的 `{ "detail": ... }` 形状。

## 快速索引

- 启动和 curl 验证：见[快速调试流程](#快速调试流程)。
- 会话与 turn：见[Session](#session)和[Turn 和命令](#turn-和命令)。
- 文件预览与下载边界：见[文件浏览](#文件浏览)。
- 权限、计划和问答：见[审批、计划和提问](#审批计划和提问)。
- Skill/plugin 管理：见[Extensions](#extensions)。
- SSE replay：见[SSE](#sse)。

## 快速调试流程

启动 server：

```bash
autoverse-web --cwd /path/to/project --host 127.0.0.1 --port 8170
```

创建或打开 session：

```bash
curl -sS -X POST http://127.0.0.1:8170/api/sessions \
  -H 'content-type: application/json' \
  -d '{"cwd": "/path/to/project"}'
```

订阅 SSE：

```bash
curl -N http://127.0.0.1:8170/api/sessions/<sid>/stream
```

发送消息：

```bash
curl -sS -X POST http://127.0.0.1:8170/api/sessions/<sid>/message \
  -H 'content-type: application/json' \
  -d '{"text": "总结这个仓库的结构"}'
```

`message` 端点只提交 turn；输出从同一个 session 的 `/stream` 到达。审批、计划和问答端点通常由 WebUI 卡片按钮调用，外部脚本需要先从 SSE block 取得对应 id。

## Static 和主题

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | WebUI 单页应用 |
| `GET` | `/theme.css` | 当前主题 CSS |
| `GET` | `/theme.json` | 当前主题 token JSON |

## 原生浏览器 host

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/browser/status` | 返回 `{connected}`，表示是否已有 Apple WebKit host |
| `WEBSOCKET` | `/api/browser/host` | Apple App 的可见 WKWebView command channel |

server 向 host 发送：

```json
{"type":"command","id":"...","session_id":"...","action":"navigate","params":{"url":"https://example.com"}}
```

host 成功时返回 `{"type":"result","id":"...","ok":true,"result":...}`；失败时
返回 `ok:false` 和 `{kind,message}` error。当前 action 与工具 inventory 一一映射：
`launch`、`close`、`list_pages`、`new_page`、`switch_page`、`close_page`、`navigate`、
`observe`、`screenshot`、`evaluate`、`click`、`type`、`content`、`scroll`、`wait`、`select`、
`keyboard`。页面操作可带稳定 `page_id`；省略时使用活动页。`list_pages` 只读状态且
不会显示已隐藏分栏，实际页面操作会显示分栏。这是 bundled Apple frontend 的内部协议，
不作为第三方稳定 API；只允许一个活跃 host，新连接会替换旧连接并使旧 pending command 失败。
页面状态结果还包含单调 `revision` 和当前 `is_loading`；WebKit 的 cancelled navigation 表示被更新导航替代，
不会覆盖一个已经可见且仍在正常加载的页面为失败。

`evaluate` 的 `script` 可以是同步表达式，或使用顶层 `return await ...` 的异步函数体；host
等待后者完成并只回传 JSON 兼容的最终值。直接返回 Promise、function、DOM node 等 WebKit
host object 时返回稳定的 `unsupported_result` 和可执行提示，而不是透传本地化 WebKit
错误字符串。异步函数体最多等待 30 秒，超时返回 `browser_timeout`；host 在超时后清理
内部 timer 并继续接收后续 command。所有成功 command 在发送前还会经过 JSON 合法性检查，
避免不可编码结果被静默丢弃并在 Python 侧表现为传输超时。

`observe` 的 `screenshot` 参数是 `never`、`always` 或 `when_visual`。host 在同一 page revision
内返回页面状态、受限 `semantic_snapshot`、截断标记、`visual_hint` 和可选 PNG；若观察期间
导航变化则重试一次，持续变化返回 `browser_state_unstable`，不会拼接不同页面版本。该 action
由 `BrowserPort.observe_page` 使用，不是额外的用户工具。

连接建立后 host 先发送 `{"type":"hello","protocol":1,"engine":"webkit",...}`；
只有版本与 engine 都匹配时，server 才把 socket 发布为 connected 并完成已有
active session 的 browser proxy 热切换后返回
`{"type":"hello_ack","protocol":1,"engine":"webkit"}`。Apple App 只有收到 ack 才显示
connected；不兼容连接收到 `hello_reject`，不能替换当前 host。普通断线后按有上限的
backoff 自动重连；被另一个 App host 替换时先收到 `host_replaced`，停止自动争抢并在
状态栏提示。命令按 session
串行，连接替换、断线、超时或用户接管都会结束 pending command，并返回稳定的 browser
error kind。未在 5 秒内完成 hello 的 socket 会以 1008 关闭；broker 的命令传输 deadline
会给 native 导航和显式 wait timeout 留出完成与回传余量。
Apple host 会从这条既有串行命令生命周期派生仅供本地显示的 activity 状态，用于控制权描边、
局部动作反馈和状态栏动作文字；`list_pages` 不触发特效，快速命令完成、失败或取消后只为可见性
保留到 800ms 展示下限，用户接管和断线立即清除。
该展示不新增 command/result 字段，也不写入页面、Conversation 或持久存储。

Apple App 内的聊天 WebView 另有一个本地 `autoverseAgentResume` message handler。有效的
composer 提交只向原生壳发送 `message_submit` 意图，用于在用户接管后恢复 agent 浏览器
控制；消息正文仍只经上述 Session HTTP route 提交，该 handler 不扩展公开 WebUI API。
聊天 Markdown 中显式、裸露或完整 inline-code 形式的 HTTP(S) 链接使用另一个本地
`autoverseBrowserLink` handler；Apple 壳校验 URL 后在工具 Browser 中创建并激活新页面。
该动作不新增 REST/SSE/WebSocket 字段，非 Apple WebUI 仍使用标准 `_blank` 链接行为。
恢复后的 turn 开始前，server 会以最多 2 秒的只读 `list_pages` 与 host 对账；页面集合、
活动 id 或任一页面的 revision/URL/title/loading 与最近工具结果不同时，变化类别和相关 page id 会
作为仅供 LLM request 使用的消息 metadata 注入，提示模型先调用 `browser_list_pages` 并在
后续目标操作中显式传 `page_id`。该 metadata 不改变 HTTP request/response shape，也不
显示成用户输入；新 session 的首次非空页面快照同样注入，避免 agent 不知道已有页面。
对账失败不会阻塞或破坏 turn。
macOS Browser 分栏的宽度只存在于进程内展示桥，不新增 HTTP/SSE/WebSocket 字段，也不写入
WebUI dashboard 的持久宽度。打开时 bridge 保存 dashboard 的可见性和实际宽度，Browser
默认取窗口宽度的 50%，再限制在 `600px`–`900px` 与当前可用空间内；关闭时恢复该快照。
Browser 最小为 `320px`，中心会话栏以 `520px` 为舒适阈值和功能下限。
继续扩大 Browser 并即将越过舒适阈值时，本地展示桥以幂等动作收起 Sessions；收起后最大
Browser 宽度为当前窗口宽度减去 `520px`。工具栏最大化使用这个动态上限，再次点击恢复
最大化前的 Browser 宽度；最大化期间窗口变化会重新计算上限，手动拖动则退出最大化。
窄窗口打开 Browser 时立即应用同一舒适阈值；由 Browser 自动收起的 Sessions 在关闭时恢复，
但用户在 Browser 打开期间的显式展开/收起优先。用户主动展开 Sessions 时，目标 grid 会重新
收紧 Browser，以继续守住 `520px` 下限。
设置、新建会话、图片预览、退出 worktree 和文件操作交互层仍属于 WebUI 展示状态。所有
应用级固定浮层注册为 conversation overlay；macOS 本地展示桥只上报可见布尔值，并在可见时
把完整聊天 WebView 提升到 Browser pane 上方、禁用 Browser 与分隔条命中。弹窗因而沿用普通
WebUI 的整窗内容尺寸，不受 Browser 或 Sessions 宽度挤压；聊天页同时把 Browser 预留轨道
切为透明，让仍然挂载的浏览器画面保留在半透明遮罩下。关闭后 Browser 页面、标签、宽度
和控制权状态保持不变。该信号不进入 Web API、Conversation 或持久字段。系统文件选择器和
原生连接设置不属于 conversation overlay，继续由 Apple 壳按系统界面呈现。
分隔条使用贯穿标题栏与正文的 40px 原生命中区，在 `WKWebView` 上方保持拖拽捕获；可见部分
是居中的单像素边界与短握柄，悬停时以强调色反馈。macOS 壳以原生标题栏承载 Sessions 与
设置入口，并隐藏 WebUI 的
折叠态重复按钮；普通 WebUI 的恢复入口不变。这些都是本地展示行为，不新增协议字段。

## 全局查询

| 方法 | 路径 | Body / Query | 说明 |
|------|------|--------------|------|
| `GET` | `/api/commands` | - | 命令补全项 |
| `GET` | `/api/models` | - | 模型选项和当前模型 |
| `GET` | `/api/workspace` | - | server 默认 cwd |
| `GET` | `/api/secrets` | query `session_id?` | 密钥状态 |
| `POST` | `/api/secrets/session` | body `{session_id, key, value}` | 写入 session-scoped secret |
| `DELETE` | `/api/secrets/session/{key}` | query `session_id` | 删除 session-scoped secret |
| `POST` | `/api/secrets/vault` | body `{key, value, session_id?}` | 写入 encrypted vault secret |
| `DELETE` | `/api/secrets/vault/{key}` | query `session_id?` | 删除 encrypted vault secret |
| `GET` | `/api/providers/account` | query `key`, `session_id?` | provider 账户状态 |
| `POST` | `/api/restart` | - | 请求重启当前 WebUI 进程 |

`GET /api/secrets` 返回当前模型、vault 可用性、每个 secret alias 的
`configured`、`effective_source`、`sources` 和 `used_by`。有效优先级由响应里的
`priority` 给出，目前为 `["env", "session", "file"]`：环境变量最高，
session-scoped secret 只影响当前 session，`file` 表示 encrypted vault。

## Session

| 方法 | 路径 | Body / Query | 说明 |
|------|------|--------------|------|
| `GET` | `/api/sessions` | - | 会话列表 |
| `POST` | `/api/sessions` | `{session_id? cwd?}` | 打开已有或创建新会话 |
| `DELETE` | `/api/sessions/{sid}` | - | 删除会话 |
| `POST` | `/api/sessions/{sid}/rename` | `{title}` | 重命名 |
| `POST` | `/api/sessions/{sid}/pin` | `{pinned}` | 置顶/取消置顶 |
| `GET` | `/api/sessions/{sid}/history` | query `before` | 读取初始 snapshot 之前的有界历史页 |
| `GET` | `/api/sessions/{sid}/resources/{block_id}/{index}` | - | 读取 replay block 的懒加载图片资源 |
| `GET` | `/api/sessions/{sid}/blocks/{block_id}` | - | 显式展开一个超预算 block |
| `GET` | `/api/sessions/{sid}/ranges/{start}/{end}` | query `page?` | 显式展开超预算 turn range；句柄内部使用 |
| `GET` | `/api/sessions/{sid}/stream` | - | SSE snapshot + deltas |

创建新 session 时，`cwd` 必须是已存在目录。打开已有 session 时不会改变 cwd。
`POST /api/sessions` 只创建/恢复 view session，不激活 engine。它从 catalog 恢复 cwd，
由 `SessionHistoryPort` 反向读取最近两个完整 turn；首次消息、命令或 mode/runtime 操作才
lazy activate engine。已有 session 的 model 与 context window 从持久 SessionMeta/model
registry 恢复，不拿当前全局默认覆盖；模型切换同时更新该 session 的 model metadata 和
后续新 session 的进程内默认。

view session 在发布右侧栏 state 前会从完整 Store 消息重建 Todo、Changed 和逐 assistant
request usage，并用 trace 补齐 compaction/retry 等累计 cache/cost。新格式可以精确显示最后
一次 request 的 Context；只有旧 trace turn 聚合可用时，state 使用 `usage_kind=turn_total`，
客户端显示 “tokens in last turn” 且不计算 context-window 百分比；无可靠来源时
`usage_available=false` 并显示 unavailable，不返回具有误导性的历史零值。`tps` 仍只表示
当前进程中的实时生成速度，冷打开时隐藏是正确行为。usage 存在但模型没有 registry 价格
时，`cost_available=false` 且费用显示 unavailable，不把未知价格显示成 `$0.0000`。

`GET /api/sessions` 的 `Server-Timing` 包含 `catalog-query`、`serialization`；
`POST /api/sessions` 包含 `session-open`、`history-load`、`serialization`；首次
`message` 响应包含 `engine-activation`；history page 包含 `history-page` 和
`serialization`。这些值用于区分浏览器反馈、索引、历史读取、激活和编码时间。

`history` 返回完整 turn 对齐的 `blocks`、`has_more` 和 opaque `before_cursor`。客户端把
当前 cursor 作为下一次 `before`；没有更早记录时 cursor 为 `null`，不得解析 cursor 的
JSONL/bridge 内部表示。图片 block 在 replay/SSE 中使用 `resource_url`，不携带 base64
正文；资源响应为对应 `image/*` 字节并使用 `private, no-store`。单个超预算 block 使用
`oversize` 提供预览和同 session 的 `detail_url`。当完整 turn 超过首屏预算时，用户消息、
最终回答和 turn footer 仍直接返回，只有两者之间的执行过程使用 `deferred_execution`
句柄；用户展开“执行过程”时才读取对应 range。前端不展示 byte、block count 或内部 kind。

## Turn 和命令

| 方法 | 路径 | Body | 说明 |
|------|------|------|------|
| `POST` | `/api/sessions/{sid}/message` | `{text, attachments?}` | 提交消息；`/` 开头走命令 |
| `POST` | `/api/sessions/{sid}/command` | `{command}` | 直接运行命令 |
| `POST` | `/api/sessions/{sid}/abort` | - | 中止当前 turn |
| `POST` | `/api/sessions/{sid}/turn/rewind` | `{rewind_id}` | 停止活动 turn，恢复该用户指令前的工作区和上下文，并返回原输入 |
| `POST` | `/api/sessions/{sid}/queue/cancel` | `{id}` | 取消排队消息 |
| `POST` | `/api/sessions/{sid}/compact` | - | 触发压缩 |
| `POST` | `/api/sessions/{sid}/clear` | - | 清空会话显示/上下文 |
| `POST` | `/api/sessions/{sid}/model` | `{model}` | 切换主模型并持久化为全局默认 |
| `POST` | `/api/sessions/{sid}/mode` | `{target?, direction?, exit_action?}` | 切换 normal/plan/worktree |
| `GET` | `/api/sessions/{sid}/tiers` | - | 查询模型 tier |
| `POST` | `/api/sessions/{sid}/tiers` | `{deep, balanced, fast}` | 设置模型 tier |

图片附件限制：

| 限制 | 值 |
|------|----|
| 类型 | `image/png`、`image/jpeg`、`image/webp`、`image/gif` |
| 数量 | 最多 4 张 |
| 单张大小 | 8MB |
| 总大小 | 20MB |

WebUI 会在每个普通用户 turn 开始前创建 git-shadow 工作区快照，并把
`rewind_id` 与对应 snapshot hash 存在用户消息 metadata 中。`turn/rewind`
先恢复工作区，成功后才以 `edit_turn` 原因重写持久化历史；恢复失败时历史保持
不变。它还会恢复该 turn 前的 plan/goal checkpoint，取消仍在运行的后台子代理，
并根据保留的用户消息更新自动生成的 session 标题；手动重命名并锁定的标题不会
被覆盖。WebUI 的 turn 编辑能力因此不受全局 `snapshot_enabled=false` 默认值影响。

该接口不撤销已经发生的远端工具副作用，也不删除实际产生的 usage 或 trace
审计记录；其事务边界是持久化对话/工作流状态与 snapshot 管理的本地工作区。

## 文件浏览

| 方法 | 路径 | Body / Query | 说明 |
|------|------|--------------|------|
| `GET` | `/api/sessions/{sid}/files` | `path`, `show_hidden` | 列目录 |
| `GET` | `/api/sessions/{sid}/file` | `path` | 读文件预览元数据 |
| `GET` | `/api/sessions/{sid}/file/raw` | `path`, `download?` | 返回受限 raw 文件流 |
| `POST` | `/api/sessions/{sid}/file/rename` | `{path, new_name}` | 重命名 |
| `DELETE` | `/api/sessions/{sid}/file` | `path` | 删除 |

目录浏览、重命名和删除始终限制在 session cwd 内。只读 `/file` 与 `/file/raw`
还允许一个精确的外部文件集合：该文件必须由当前 session 已加载的对话 Markdown 以
文件链接或 inline-code 路径明确展示，或已出现在 Changed/Memory 侧栏状态中。授权按
resolve 后的完整文件路径逐项匹配，不授予父目录浏览权；任意猜测的 cwd 外路径仍返回
403。大小、UTF-8、MIME、SVG sandbox 与 `nosniff` 检查对外部文件同样生效。

### `/file` 预览元数据

`/file` 会返回 `kind` 区分预览类型。预览 JSON 的通用元数据包含：

| 字段 | 说明 |
|------|------|
| `kind` | 预览类型 |
| `path` | cwd 内文件使用相对路径；已授权外部文件使用绝对路径 |
| `root` | session cwd 的绝对路径，用于前端复制绝对路径 |
| `name` | 文件名 |
| `size` | 文件大小，单位 byte |
| `mime` | raw/image/pdf 等预览的 MIME 类型 |

| `kind` | 支持内容 | 响应形态 |
|--------|----------|----------|
| `markdown` | `.md`、`.markdown` UTF-8 文本，1 MiB 内 | JSON 内含 `content`，前端富 Markdown/Mermaid 渲染 |
| `table` | `.csv`、`.tsv` UTF-8 文本，1 MiB 内 | JSON 内含 `content`，前端表格预览 |
| `text` | 其它 UTF-8 文本，1 MiB 内 | JSON 内含 `content`，前端代码/文本预览 |
| `image` | `png`、`jpg/jpeg`、`webp`、`gif`、`svg`，25 MiB 内 | JSON 元数据；内容由 `/file/raw` inline 返回 |
| `pdf` | `.pdf`，25 MiB 内 | JSON 元数据；内容由 `/file/raw` inline 返回 |
| `raw` | 其它可访问文件，25 MiB 内 | JSON 元数据；可通过 `/file/raw` inline 打开或带 `download=true` 下载 |

文本超过 1 MiB 后不会塞进 JSON；如果在 raw 上限内，会降级为 `raw`。
超过 raw 上限返回 413。

## 审批、计划和提问

| 方法 | 路径 | Body | 说明 |
|------|------|------|------|
| `POST` | `/api/sessions/{sid}/approval` | `{call_id, action, scope}` | 解析权限审批 |
| `POST` | `/api/sessions/{sid}/plan` | `{action}` | `approve/revise/cancel` |
| `POST` | `/api/sessions/{sid}/ask_question` | `{question_id, selected}` | 回答 AskUserQuestion |

这些端点通常由 WebUI 卡片按钮调用；外部脚本需要先从 SSE block 中取得对应 id。

## 子代理查看

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/sessions/{sid}/agent/{job}` | 读取某个 sub-agent job 的上下文视图 |

## Extensions

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/extensions/skills?session_id=<sid>` | 列 skills |
| `GET` | `/api/extensions/skills/{skill_id}?session_id=<sid>` | skill 详情 |
| `POST` | `/api/extensions/skills/{skill_id}/doctor` | skill 诊断；body 需要 `session_id`，可选 `prompt` |
| `POST` | `/api/extensions/skills/{skill_id}/enable` | 启用 skill；body 需要 `session_id` |
| `POST` | `/api/extensions/skills/{skill_id}/disable` | 禁用 skill；body 需要 `session_id` |
| `POST` | `/api/extensions/skills/{skill_id}/trust` | 信任 skill；body 需要 `session_id` |
| `GET` | `/api/extensions/plugins?session_id=<sid>` | 列 plugins |
| `GET` | `/api/extensions/plugins/{plugin_id}?session_id=<sid>` | plugin 详情 |
| `POST` | `/api/extensions/plugins/{plugin_id}/doctor` | plugin 诊断；body 需要 `session_id` |
| `POST` | `/api/extensions/plugins/{plugin_id}/enable` | 启用 plugin；body 需要 `session_id` |
| `POST` | `/api/extensions/plugins/{plugin_id}/disable` | 禁用 plugin；body 需要 `session_id` |
| `POST` | `/api/extensions/plugins/{plugin_id}/trust` | 信任 plugin；body 需要 `session_id` |

当前 enable/disable/trust 是 `ExtensionAdminService` 的 session-local 管理状态：会改变 extension 列表展示，但不持久化，不写配置或 trust store，也不会重新装配已经注册的 tool、skill、hook、MCP 或 plugin capability。

## SSE

`GET /api/sessions/{sid}/stream` 返回：

```text
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
```

事件数据由 `SessionBridge` 发布，包含 snapshot、block 增量、status、meta 等前端块。
初始 snapshot 的 `blocks` 最多 200 个且使用 128 KiB JSON 预算；若仍有更早记录，
同时返回 `has_more=true` 和 `before_cursor`，客户端再走 `history` 分页。snapshot 总是
表示活动/最新 turn 和前一个完整 turn；单个超预算 block 或无法装入预算的超大 turn
不会挤满首帧。单个 block 使用 `oversize` 预览；超大 turn 使用
`deferred_execution` 延迟其中的执行过程，同时保留用户消息、最终回答和 footer。
块形状以 `web_log.py`、`session.py` 和 `bridge.py` 为准。
子代理块传递纯文本的 `head_text` / `foot_text` 与语义 `role`；浏览器根据
当前浅色或深色外观解析角色色，不重放服务端渲染时的前景 hex。
sidebar `state` 的 usage 字段还包含 `usage_available` 与 `usage_kind`；`context` 表示精确的
最后一次模型 request，`turn_total` 表示旧会话只能恢复最后一个完整 turn 的聚合。

## 开发入口

- `src/autoverse/frontends/web/server.py`
- `src/autoverse/frontends/web/session.py`
- `src/autoverse/frontends/web/bridge.py`
- `src/autoverse/frontends/web/files.py`
- `src/autoverse/frontends/web/static/app.js`
