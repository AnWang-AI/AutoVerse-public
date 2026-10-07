# 编写 Hooks

> 当前事实来源：`docs/reference/`、实际 CLI/WebUI 行为和相关测试。
> 维护规则：用户步骤、命令示例、默认行为或排障路径变化时必须同步。

Hook 用来在生命周期事件上观察或拦截行为。当前生产路径最常见、也最可靠的用途是阻止危险工具调用，或在工具调用后做旁路审计。

## 前置条件

- Hook command 能在当前 shell 手动执行。
- 初次接入时使用窄 `matcher` 验证，避免误拦截所有工具调用。

## 配置一个 PreToolUse hook

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "write_file",
        "command": "python scripts/deny_env_write.py",
        "timeout": 3.0,
        "blocking": true,
        "description": "Block writes to secret files"
      }
    ]
  }
}
```

`matcher` 使用 `fnmatch` 匹配工具名。`HookEngine` 把 `PreToolUse` 和 `Stop` 定义为阻塞事件：如果 hook 超时或异常，并且 `blocking=true`，会 fail-closed。当前默认装配会触发工具 dispatcher 两侧的 `PreToolUse` 和 `PostToolUse`，其中 `PreToolUse` 是实际阻塞控制点。

Shell hook 从环境变量和 stdin JSON 读取上下文，并通过 stdout JSON 返回控制结果。
协议字段全集只维护在 [`../reference/hooks.md`](../reference/hooks.md)。最常见的
拒绝输出是：

```json
{"decision": "deny", "reason": "writing .env is forbidden"}
```

## 示例：禁止写入 `.env`

仓库内可直接参考 [`../../scripts/deny_env_write.py`](../../scripts/deny_env_write.py)，核心逻辑如下：

```python
from __future__ import annotations

import json
import sys

payload = json.load(sys.stdin)
tool_input = payload.get("tool_input", {})
path = str(tool_input.get("file_path") or tool_input.get("path") or "")

if path.endswith(".env") or "/.env" in path:
    print(json.dumps({
        "decision": "deny",
        "reason": "secret files must not be edited by tools",
    }))
else:
    print(json.dumps({"decision": "proceed"}))
```

## 事件选择

当前生产路径优先使用 `PreToolUse` 阻止危险工具，使用 `PostToolUse` 做旁路审计。
`SessionStart` 和 `Stop` 已在 `HookEngine` 中定义，但默认 builder 尚未调用；
精确事件语义见 [`../reference/hooks.md`](../reference/hooks.md)。

## 常见失败

### Hook 阻止了所有工具

检查 `matcher` 是否过宽，例如 `*`。同时确认脚本在所有输入上都会输出 proceed 或空输出。

### Hook 超时导致拒绝

阻塞事件默认 fail-closed。缩小脚本工作量，或只在确实可降级的事件上设置 `blocking=false`。

### Hook 输出没有生效

确认 stdout 是合法 JSON object。stderr 不参与控制结果；脚本非零退出会被当成 hook failure，阻塞事件会 fail-closed。字段全集见 [`../reference/hooks.md`](../reference/hooks.md)。

### SessionStart 或 Stop 没有触发

这是当前实现边界。`HookEngine` 定义了这两个事件，但默认装配只把 `PreToolUse` / `PostToolUse` 包到工具 dispatcher 上。


## 相关文档

- [Hooks 参考](../reference/hooks.md)
- [工具参考](../reference/tools.md)
- [工具与权限设计](../design/tools-and-permissions.md)
- [ADR-0007：Hook 不静默改写工具输入](../adr/0007-hook-no-silent-rewrite.md)
