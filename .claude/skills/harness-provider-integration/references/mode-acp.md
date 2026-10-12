# ACP 模式：CLI 作为 Agent Client Protocol agent 常驻

适用：CLI 能以 `<cli> acp` / `--acp` / `--experimental-acp` 之类方式在 stdio 上跑 ACP
（opencode、kimi-code、gemini-cli、qwen-code、goose 等）。ACP 是标准协议，所以模板里的
`rpc.py`、`mapping.py`、`harness.py` 基本**不需要厂商代码**——在 opencode 上零改动通过了全部 e2e。

## 模板做了什么

```
CliProcess (process.py)            启动 / 行读写 / stderr 丢弃 / 关闭→终止→kill
  └─ JsonRpcConnection (rpc.py)    单 reader；响应→future；通知按序 await；服务端请求独立 task
       └─ <Class>Harness            initialize → session/new | session/load → session/prompt ×N
            └─ <Class>TurnAccumulator (mapping.py)   session/update → 协议事件；stopReason → 终态
```

| 协议动作 | ACP 实现 |
|---|---|
| 可执行文件 | `cli_path` → `command[0]` 查 PATH → 包内自带 CLI 兜底（打包时可选，用到时其目录前置到子进程 PATH） |
| `start` | 启动进程 → `initialize`（`fs`/`terminal` 能力置 false）→ 校验 `protocolVersion==1` → `session/new`（或 `session/load`） |
| 一个 Turn | 一次 `session/prompt`；期间的 `session/update` 映射为事件；响应的 `stopReason` 决定终态 |
| `abort` | `session/cancel` 通知；agent 回 `stopReason: cancelled` |
| 工具审批 | `session/request_permission` → 宿主 `ToolApprovalRequest`（声明了 TOOL_APPROVAL 时），否则按 `auto_approve` 选 allow/reject 选项；中止中的 Turn 一律回 `cancelled` |
| checkpoint | `{"session_id", "resumed"}`；恢复走 `session/load`，回放的历史通知因无活动 Turn 被丢弃 |
| 系统提示词 | ACP 没有该字段；首轮 prompt 前置 `<system-instructions>` 围栏（恢复的会话不再发送） |
| MCP | `session/new.mcpServers`：stdio `{name, command, args, env:[{name,value}]}`；http 需 `mcpCapabilities.http` |
| 用量 | 读 prompt 响应里的 `usage`（非标准扩展，有就用） |
| 进程崩溃 | reader 遇 EOF → 挂起请求以 `JsonRpcConnectionClosed` 失败 → Turn FAILED（`<UPPER>_CLI_EXITED`）；之后的 Turn 立刻失败，由宿主决定重启 |

## 需要厂商化的点（SCAFFOLD 标记）

| 标记 | 怎么关 |
|---|---|
| `capabilities` | 跑 e2e；`initialize` 不报 `loadSession` 就删 `PERSISTENT_SESSION` / `CHECKPOINT` |
| `model` | 两条路都在真实 CLI 上试：启动 flag 填 `MODEL_ARGS = ("--model", "{model}")`；会话内切填 `MODEL_CONFIG_ID`（`session/new` 的 `configOptions` 里模型选择项的 `id`），适配器建会话后调 `session/set_config_option` 并核对 `currentValue`。opencode 只能走 flag，kimi-code 只能走会话内切；都不行就保持默认（配置了 model 会在 start 被拒） |
| `mcp-naming` | 挂一个 FastMCP fixture，看 `tool_call` 的 `title`：opencode 是 `{server}_{tool}`，kimi-code 是 `mcp__{server}__{tool}` |
| `failure-codes` | 触发真实错误（未登录、错模型），把厂商自有 JSON-RPC 错误码加进 `_VENDOR_RPC_CODE_CATEGORY` |
| `fixtures` | 用录到的真实往返改写 `fake_agent.py` 的记录形状 |
| `skills-dir` | CLI 的项目级技能发现目录，例如 `.opencode/skills`（以厂商文档为准） |
| `agents-md` | 填包内 `AGENTS.md`"验证过的厂商事实" |

## 已知坑

- **工具参数晚到**：`tool_call` 可能 `pending` + 空 `rawInput`，参数在 `in_progress` 更新里；kimi-code
  还会先发几条**不带参数**的 `in_progress`。模板只在"拿到参数或调用已结束"时发 STARTED；不要改成见
  `tool_call` 或见 `in_progress` 就发，否则 STARTED 的 `arguments` 是空的。
- **`title` 会变**：工具名取第一次见到的值。
- **不要在 reader 里 await 宿主交互**：权限请求在独立 task 里应答，reader 必须继续读通知，
  否则死锁。通知则必须 inline await，保留顺序和缓冲区背压。
- **超时要等迟到的响应**：`session/prompt` 超时后先 `session/cancel`，再用 `asyncio.shield` 等
  原 future 一小段时间，免得这次 prompt 的尾部通知漏进下一轮。
- **关闭顺序**：先关子进程（reader 继续排空 stdout 到 EOF），再关 RPC；反过来子进程可能卡在写满的管道上。
- **一个会话同一时刻只有一个 prompt**：ACP 不支持 steer，不要声明 STEER。
- **认证**：`initialize` 回的 `authMethods` 不由 adapter 处理（需要交互）；未登录时 prompt 回 `-32000`，
  映射为 `auth_required`，由用户在终端登录。
- 厂商自定义的 `session/update` 类型（`plan`、`available_commands_update`、`config_option_update`、
  `usage_update`）落为 `ProviderEvent`；需要时再提升为正式事件。

## 多个 ACP provider 时

第二个 ACP 厂商接入前，先把 `process.py` / `rpc.py` / ACP 映射与 harness 主体抽成一个共享的
ACP 基础包（例如 `openjiuwen-harness-acp-base`），厂商包依赖它，只留 config、`MODEL_ARGS` / `MODEL_CONFIG_ID`、`MCP_TOOL_NAME_PATTERN`、
错误码表与厂商特有通知映射。**不要让第二份 ACP 客户端代码进仓库。**
