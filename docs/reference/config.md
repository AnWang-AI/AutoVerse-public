# 配置参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

配置最终会合并为 `AppContext.config` 上的不可变快照。core 只消费这个快照，不负责读取文件、环境变量或 vault。

## 快速索引

- 想确认覆盖顺序：见[配置来源和优先级](#配置来源和优先级)。
- 想查字段和默认值：见[配置字段](#配置字段)。
- 想配置模型密钥：见[密钥解析](#密钥解析)。
- 想理解项目记忆：见[项目记忆](#项目记忆)和 [`context-and-compaction.md`](context-and-compaction.md)。
- 想直接复制一个起点：见[配置示例](#配置示例)。

## 责任边界

| 主题 | 真相源 | 注入位置 |
|------|--------|----------|
| 配置 | Pydantic `Config` 树 | `AppContext.config` |
| 密钥 | 环境变量和 vault | `AppContext.secrets` |
| 项目记忆 | 当前目录的 `CLAUDE.md` / `AGENTS.md` | `PromptAssembler.memory` |

## 配置原则

- 所有阈值放在配置模型里，避免 `core` 散落魔法数字。
- 配置对象及固定结构内层容器不可就地修改；热替换通过构造新树实现。
- 配置文件不保存明文密钥，只保存密钥别名。
- 加载错误应包含字段路径、来源和原因。

## 配置来源和优先级

配置加载由 `infra.config.loader` 完成。层级从低到高如下，高层同名字段覆盖低层字段；合并后再由 Pydantic `Config` 树统一校验。

| 优先级 | layer | 来源 |
|--------|-------|------|
| 1 | `default` | 内置默认值 |
| 2 | `global` | `~/.autoverse/config.json` 或 `~/.autoverse/config.toml` |
| 3 | `project` | 从当前 `cwd` 向上找到的第一个 `.autoverse/config.json` 或 `.autoverse/config.toml` |
| 4 | `env` | `AUTOVERSE_` 环境变量；双下划线表示嵌套字段 |
| 5 | `cli` | CLI 显式覆盖 |

## 常见配置主题

| 主题 | 常见内容 |
|------|----------|
| model | 主模型 ID、max tokens、temperature、api key alias |
| model tiers | 子代理 `deep`、`balanced`、`fast` tier 到具体模型的映射 |
| compaction | 上下文窗口、压缩阈值、摘要预算 |
| recovery | 模型/tool-call 恢复重试上限 |
| permissions | 默认权限模式和审批策略 |
| memory | 启动时加载的项目记忆文件 |
| browser | 会话级浏览器自动化后端和视口设置 |

具体字段以当前 `src/autoverse/app/` 配置模型为准。新增字段时要同步本页。

## 配置字段

### 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | object | `{}` | 主模型运行参数 |
| `model_tiers` | object | `{}` | 子代理抽象 tier 到具体模型的映射 |
| `compaction` | object | `{}` | 上下文压缩阈值 |
| `recovery` | object | `{}` | 模型/tool-call 恢复配置 |
| `memory` | object | `{}` | 项目记忆加载配置 |
| `browser` | object | `{}` | 浏览器自动化配置 |
| `default_permission_mode` | `"allow_all" \| "deny_all" \| "ask"` | `"ask"` | 默认权限模式 |
| `session_dir` | string | `"~/.autoverse/sessions"` | 会话存储目录 |
| `max_turns` | integer | `100` | 一轮任务最大 turn 数 |
| `system_prompt_append` | string | `""` | 附加到 system prompt 的文本 |
| `trace_enabled` | boolean | `true` | 是否启用 trace 记录 |
| `snapshot_enabled` | boolean | `false` | 是否启用工作区 snapshot |
| `log_enabled` | boolean | `true` | 是否启用进程级诊断日志 |
| `log_level` | `"DEBUG" \| "INFO" \| "WARNING" \| "ERROR" \| "CRITICAL"` | `"INFO"` | 诊断日志等级 |
| `log_dir` | string | `""` | 诊断日志目录；空值表示使用默认数据目录下的 logs |
| `log_json` | boolean | `true` | 是否使用 JSON lines 日志格式 |
| `log_max_bytes` | integer | `5000000` | 单个日志文件轮转大小 |
| `log_backup_count` | integer | `3` | 保留的轮转日志数量 |
| `mcp_servers` | object | `{}` | MCP server 配置表 |
| `plugins` | object | `{}` | 插件扫描和禁用配置 |
| `hooks` | object/null | `null` | hook 配置，解析在 app/infra 层完成 |

`session_dir` 仍只定义权威 transcript JSONL/blob 的位置。默认本地装配在其父 data directory
创建 `session-catalog.sqlite3`，用于 pinned、last-viewed 和可重建 rail projection；它不是
新增配置字段，也不能替代或删除 `session_dir` 中的消息。

### `model`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_id` | string | `""` | 主模型 ID；空值表示回退到 CLI 选择或 `echo` |
| `max_tokens` | integer | `8192` | 单次模型输出 token 上限 |
| `temperature` | number | `1.0` | 采样温度 |
| `api_key_alias` | string/null | `null` | 密钥别名；不要写明文 key |

### `model_tiers`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `deep` | string | `""` | 深度子代理模型；空值表示回落到主模型 |
| `balanced` | string | `""` | 平衡档子代理模型 |
| `fast` | string | `""` | 快速档子代理模型 |

### `compaction`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `context_window` | integer | `0` | `0` 表示按当前模型自动推导；大于 0 表示显式覆盖 |
| `compact_threshold` | number | `0.72` | 触发轻量压缩的上下文比例 |
| `summary_threshold` | number | `0.82` | 触发摘要压缩的上下文比例 |
| `emergency_threshold` | number | `0.90` | 触发紧急截断/压缩的上下文比例 |
| `summary_output_budget` | integer | `4096` | 摘要输出预算 |
| `cooldown_effective` | integer | `1` | 有效压缩后的冷却 turn 数 |
| `cooldown_low` | integer | `2` | 低收益压缩后的冷却 turn 数 |
| `cooldown_stuck` | integer | `5` | 卡住时的冷却 turn 数 |
| `low_reduction_ratio` | number | `0.30` | 低收益判定比例 |
| `ema_alpha` | number | `0.20` | EMA 平滑参数 |

约束：`compact_threshold <= summary_threshold <= emergency_threshold`。

阈值应用于完整请求占用：provider token estimate、由当前 `max_tokens` 和窗口比例共同
限定的输出预留，以及内部安全余量。`context_window=0` 时随活动模型切换；usage
返回后估算会在会话内校准。

### `recovery`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_retries` | integer | `3` | 恢复策略最大重试次数 |

### `memory`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否加载项目记忆 |
| `files` | array/string tuple | `("CLAUDE.md", "AGENTS.md")` | 当前目录下要加载的记忆文件名 |
| `max_file_chars` | integer | `25000` | 单个记忆文件最大注入字符数 |
| `max_total_chars` | integer | `60000` | 所有记忆合计最大注入字符数 |
| `scan_max_depth` | integer | `15` | 已弃用：当前为 cwd-only 扫描 |
| `retrieval_enabled` | boolean | `false` | 可选 FTS5 retrieval helper 的显式开关；默认 builder 仍走 cwd-only full memory 注入 |
| `reconcile_on_search` | boolean | `true` | retrieval helper 搜索前是否延迟重建索引；默认 prompt 注入路径尚未消费 |

### `browser`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `backend` | `"auto" \| "playwright" \| "none"` | `"auto"` | `auto` 在 Playwright 可用时启用浏览器后端；`none` 禁用浏览器工具后端 |
| `headless` | boolean | `true` | 是否以 headless 模式启动浏览器 |
| `viewport_width` | integer | `1280` | 浏览器视口宽度，最小 `320` |
| `viewport_height` | integer | `900` | 浏览器视口高度，最小 `240` |
| `timeout_ms` | number | `10000` | 浏览器操作默认超时，单位毫秒 |

浏览器后端是会话级资源，由 `EngineBuilder` 注入到 `AppContext.browser`。没有可用 Playwright 或配置为 `none` 时，`browser_*` 工具会返回 `no_backend`。Apple App 的 WebKit host 是 frontend 显式注入项；host 在 engine 激活前已连接时优先于这些 Playwright 字段，`headless`、viewport 和 Playwright timeout 不用于原生 WKWebView。

浏览器截图结果会走 150 KB 护栏后放入 metadata，并在 WebUI 中显示为可点击放大的缩略图。vision 模型可以直接看到截图；text-only 模型会在权限允许时自动委托 `read_image` 生成文字描述。详见 [`tools.md`](tools.md) 的浏览器工具章节。

### `mcp_servers.<name>`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `command` | string/null | `null` | stdio MCP server 命令 |
| `args` | array | `[]` | stdio 命令参数 |
| `env` | object | `{}` | 配置层保留；当前 runtime transport 尚未消费 |
| `cwd` | string/null | `null` | 配置层保留；当前 runtime transport 尚未消费 |
| `url` | string/null | `null` | HTTP MCP server URL，当前保留；默认 stdio client 尚未消费 |
| `headers` | object | `{}` | HTTP headers，当前保留 |
| `capability` | string/null | `null` | 限制 server 工具能力等级 |
| `needs_network` | boolean | `true` | server 工具是否需要网络 |
| `rpc_timeout` | number | `30.0` | RPC 超时秒数 |

约束：`command` 和 `url` 至少设置一个。

### `plugins`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `extra_paths` | array | `[]` | 额外扫描的扩展目录 |
| `disabled` | array | `[]` | 禁用的扩展/插件名称 |

## 配置示例

```json
{
  "model": {
    "model_id": "openrouter/deepseek-v4-pro",
    "max_tokens": 8192,
    "temperature": 1.0,
    "api_key_alias": "OPENROUTER_API_KEY"
  },
  "model_tiers": {
    "deep": "openrouter/deepseek-v4-pro",
    "balanced": "deepseek/deepseek-v4-flash",
    "fast": ""
  },
  "default_permission_mode": "ask",
  "browser": {
    "backend": "auto",
    "headless": true,
    "viewport_width": 1280,
    "viewport_height": 900,
    "timeout_ms": 10000
  },
  "trace_enabled": true,
  "snapshot_enabled": false,
  "memory": {
    "enabled": true,
    "files": ["CLAUDE.md", "AGENTS.md"]
  }
}
```

## 密钥解析

密钥由 `SecretProvider` 提供，只暴露 `get` / `require` 这类窄接口。常见优先级：

1. 环境变量。
2. 加密 vault。
3. 缺失时报错。

WebUI 会在单个 session 内提供临时 secret 层；有效优先级是
`env -> session -> file(vault)`，且不把 session secret 持久化到配置文件。
具体 endpoint 和状态形状见 [`webui-api.md`](webui-api.md)。

示例：

```bash
export OPENROUTER_API_KEY="..."
export DEEPSEEK_API_KEY="..."
```

模型配置里应引用 `OPENROUTER_API_KEY` 这类 alias，而不是写入明文 key。

## 项目记忆

当前项目记忆是启动时装配的 prompt 片段，不是长期数据库。默认路径只读取当前 `cwd` 下 `memory.files` 列出的文件，不向上遍历父目录；读取结果进入主 agent 的 `PromptAssembler.memory`。可选 FTS5 retrieval 后端和判断函数已存在，但默认 `EngineBuilder` 还没有把 retrieval 接入 prompt 注入路径。

生产子代理使用独立的静态角色 prompt 加 task/context，不继承主 agent 的 `PromptAssembler.memory`。如果需要把项目规则传给子代理，应通过派发任务的 context 显式提供。

使用建议见 [`../guides/project-memory.md`](../guides/project-memory.md)。

## 常见错误

### 配置字段不生效

检查字段是否真的存在于当前配置模型。不在当前模型里的字段不会自动生效。

### 密钥缺失

确认配置中的 alias 和环境变量名称完全一致。不同 provider 可能使用不同 key。

### 配置里写了明文 key

不要这样做。把明文迁移到环境变量或 vault，配置只保留 alias。

### 修改阈值后 core 行为没变

确认阈值是在 `EngineBuilder` 装配前加载的。运行中的不可变配置快照不会被就地修改。

## 开发入口

- `src/autoverse/app/context.py`
- `src/autoverse/app/options.py`
- `src/autoverse/infra/secrets/vault.py`
- `src/autoverse/app/builder.py`
