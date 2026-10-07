# Hooks 参考

> 当前事实来源：`src/autoverse/`、`tests/` 和本页列出的开发入口。
> 维护规则：命令、配置、API、工具、事件、角色或字段变化时必须同步。

Hooks 由 app/infra 层解析和运行。core 不直接读取 hook 配置。

## 事件

| 事件 | 工具上下文 | 阻塞默认 | 当前行为 |
|------|------------|----------|----------|
| `SessionStart` | 否 | 否 | `HookEngine` 可合并 `additional_context`；默认装配未触发 |
| `PreToolUse` | 是 | 是 | 第一个 `deny` 阻止工具调用 |
| `PostToolUse` | 是 | 否 | 工具执行后旁路观察 |
| `Stop` | 否 | 是 | `HookEngine` 可 fail-closed；默认装配未触发 |

## 配置形状

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "command": "python scripts/deny_env_write.py",
        "matcher": "write_file",
        "timeout": 5.0,
        "blocking": true,
        "description": "Block writes to dotenv secret files"
      }
    ]
  }
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `command` | string | 必填 | shell command |
| `matcher` | string | `"*"` | `fnmatch` 工具名匹配 |
| `timeout` | number | `5.0` | 单个 hook 超时秒数 |
| `blocking` | boolean | `true` | 阻塞事件失败时是否拒绝 |
| `description` | string | `""` | prompt fragment 和管理面显示 |

未知事件名、非列表 entries、无 command entry、非法 timeout 或非 boolean
blocking entry 会被跳过，避免一条坏配置中断默认扩展装配。

## Shell 协议

环境变量：

| 变量 | 说明 |
|------|------|
| `AUTOVERSE_HOOK_EVENT` | 当前事件名 |
| `AUTOVERSE_HOOK_TRIGGER` | 触发说明，可能为空 |
| `AUTOVERSE_TOOL_NAME` | 工具事件时的工具名 |

stdin JSON：

```json
{
  "event": "PreToolUse",
  "trigger": "",
  "tool_name": "edit_file",
  "tool_input": {}
}
```

stdout JSON：

```json
{
  "decision": "deny",
  "reason": "explanation for the user",
  "additionalContext": "optional prompt context"
}
```

`decision` 只有大小写不敏感的 `deny` 会拒绝；其他值按 proceed 处理。
非 JSON stdout 会被当作 `additional_context`，不会拒绝动作。
Shell adapter 只把 stdout 当作控制协议；stderr 只用于人工诊断，不参与字段解析。
子进程非零退出会被视为 hook failure，由 `HookEngine` 按事件阻塞语义处理。

## 失败语义

- 阻塞事件上，超时、异常或 shell hook 非零退出，且 `blocking=true`，会返回 `deny`。
- 非阻塞事件上，超时、异常或 shell hook 非零退出会降级为未运行。
- `PreToolUse` 被拒绝时，dispatcher 返回 `ToolResult.error(..., error_kind="hook_blocked")`，不会进入权限检查或工具执行。

## Prompt 投影

`HookEngine.prompt_fragment()` 只把阻塞的 `PreToolUse` hooks 投影给模型：

```text
## Active Tool Hooks
- matcher `write_*`: Check write tool calls
```

被动审计类 `PostToolUse` 不投影，避免干扰模型。

## 当前装配边界

默认 `EngineBuilder` 通过 `HookEngine.middleware()` 只包裹工具 dispatcher：

- 工具调用前运行 `PreToolUse`。
- 工具调用后运行 `PostToolUse`。
- `SessionStart` 和 `Stop` 只有接入明确生命周期调用点后才会在默认运行路径触发。

## 开发入口

- `src/autoverse/infra/hooks/engine.py`
- `src/autoverse/infra/hooks/shell.py`
- `src/autoverse/app/extensions.py`
- `src/autoverse/app/builder.py`

相关扩展边界见 [`extensions.md`](extensions.md)。
