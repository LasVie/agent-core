# 调研与取证：先拿到厂商的真实记录

写任何映射之前，先回答下面的问题，并且每个答案都要能指回一条**真实运行记录**或厂商**源码**。
铁律：映射之后的东西只能证明映射本身；字段丢没丢，只有厂商原始记录说了算。

## 1. 必答问题

| # | 问题 | 决定什么 |
|---|---|---|
| 1 | 厂商提供哪些驱动入口？Python SDK / ACP（`<cli> acp`、`--acp`、`--experimental-acp`）/ 结构化 stdout 的一次性模式（`-p ... --output-format stream-json`、`run --format json`）/ HTTP server？ | 接入模式（见 SKILL.md 决策表） |
| 2 | 一个外部 Turn 对应厂商的什么？（一次 `session/prompt`、一次进程调用、一次 `thread.turn()`） | `_execute_turn` 的边界 |
| 3 | Turn 结束的信号是什么？（RPC 响应的 `stopReason`、`turn_end` 记录、EOF+退出码） | 终态判定；`STREAM_END_IS_TURN_END` |
| 4 | 会话怎么延续？进程内常驻 / 会话 id + resume 参数 / `session/load` | PERSISTENT_SESSION、CHECKPOINT |
| 5 | 中途能否中止？（`session/cancel`、SDK `interrupt()`、只能杀进程） | GRACEFUL_ABORT / FORCE_ABORT |
| 6 | 运行中能否追加输入（steer）？ | STEER（多数 CLI 不能） |
| 7 | 系统提示词怎么注入？（SDK 参数、CLI flag、配置文件、只能拼进首条输入） | `system_prompt_mode` / prelude |
| 8 | MCP 服务器怎么挂？（会话参数、CLI flag、配置文件、`<cli> mcp add`）支持 stdio / http？ | MCP_TOOLS、`_acp_mcp_servers` 等 |
| 9 | 模型侧看到的 MCP 工具名长什么样？ | `MCP_TOOL_NAME_PATTERN`（不变量 12） |
| 10 | 工具审批 / 向用户提问以什么形态出现？ | TOOL_APPROVAL / USER_INPUT |
| 11 | 文本是否有增量？推理是否可见？用量在哪里报？ | OutputEvent DELTA/FINAL、UsageUpdatedEvent |
| 12 | 错误以什么结构出现？有无稳定错误码 / HTTP 状态？ | failure_classifier |
| 13 | 模型怎么选？（启动 flag、会话配置项、配置文件）两种都实测，厂商之间相反 | `MODEL_ARGS` / `MODEL_CONFIG_ID` |
| 14 | 技能（skills）从哪个项目目录发现？ | `SKILLS_SCAN_DIR` |
| 15 | 需要登录吗？未登录时报什么？ | `auth_required` 映射；e2e 前置条件 |

## 2. 取证方法

### CLI

```bash
<cli> --version
<cli> --help
<cli> <subcommand> --help          # acp / run / exec / mcp
```

把一次性模式的输出整段落盘（**不要只看终端**，终端会吞掉 stderr 与结构）：

```bash
cd <scratch>; echo "TOKEN-ABC123" > secret.txt
<cli> run --format json "Read secret.txt with your file tool and reply with exactly the token." > out.jsonl 2> err.txt
```

ACP：用一个最小 JSON-RPC 客户端录下全部往返（initialize → session/new → 带工具的 prompt →
被 `session/cancel` 的 prompt → 再一次 prompt 验证会话延续 → session/load）。录制脚本要点：

- 单 reader 读 stdout，每行 `json.loads`；把 `(方向, 消息)` 逐条写 jsonl；
- 收到 `session/request_permission` 时选 `allow_once` 回包，否则 agent 会一直等；
- `initialize` 时 `clientCapabilities` 的 `fs` / `terminal` 设 `false`，让 agent 用自己的工具；
- 统计 `session/update` 里 `sessionUpdate` 的种类分布，每种取一条样本。

### SDK

- 读安装目录里的 SDK 源码（`python -c "import x, inspect; print(inspect.getsourcefile(x))"`），
  找：客户端构造参数、会话 API、流式迭代器的元素类型、中断 / steer 方法、异常类型、服务端请求回调。
- SDK 往往再拉起一个 CLI 子进程：确认它用的是自带二进制还是 PATH 上的（Claude / Codex 的 e2e 都必须
  显式指向本机 CLI，因为自带版本可能过旧）。
- 生成类型（pydantic）是便利而不是边界：Codex 的 `item/tool/requestUserInput` 没有生成类型，但底层
  回调照样把原始 method + params 交上来。

## 3. 证据规则

1. 每个映射分支、每条能力声明，都能指回一条落盘的原始记录或一段 SDK 源码。
2. 单测夹具从真实记录裁剪而来；凭想象写的夹具只会把错误映射钉死。
3. 记录里出现、映射用不上的字段，保留在 `ProviderEvent` / `provider_data` 里，不丢。
4. 记录中可能有凭据、请求体、绝对路径：录制文件放 scratchpad，不进仓库；夹具只留结构必需字段。

## 4. 已验证的实例（2026-10，用于校准判断，不代替你自己的取证）

### opencode 1.17.18

- 入口：`opencode acp`（ACP v1，stdio）；`opencode run --format json`（一次性 JSONL）。
- ACP `initialize` 回 `agentCapabilities.loadSession=true`、`mcpCapabilities.http=true`，
  `sessionCapabilities` 还有 `resume/fork/list/close`。
- `session/new` 回 `configOptions`（含 `category: model` 的选择项）；但 `session/set_config_option`
  对列表里的合法值也回 `-32602 model not found`——在 opencode 上会话内切模型不可用，模型走启动参数
  （kimi-code 正相反，见下）。
- 工具调用：先 `tool_call`（`status: pending`、`rawInput: {}`），再 `tool_call_update`
  （`in_progress`，此时才带 `rawInput`），最后 `completed`（`content` 为渲染文本，`rawOutput`
  为结构化结果）。`title` 第一次是工具名，完成时变成文件路径。
- 文本：`agent_message_chunk` / `agent_thought_chunk` 带 `messageId`，是真正的增量。
- `session/prompt` 响应带非标准 `usage`（`inputTokens/outputTokens/totalTokens/cachedReadTokens`）。
- `session/cancel` 后 prompt 响应 `stopReason: "cancelled"`，会话可继续使用。
- MCP 工具在模型侧命名为 `{server}_{tool}`（如 `fixture_lookup_marker`）。
- 一次性 JSON：记录类型 `step_start` / `tool_use`（只在完成时出现一次，带完整 input/output）/
  `text`（整段，无增量）/ `step_finish`（`reason: tool-calls | stop`，带 tokens / cost）；每条都带
  `sessionID`；没有显式 turn 结束记录，**EOF + 退出码 0 即结束**（`STREAM_END_IS_TURN_END = True`）。
- 本 skill 的 ACP 模板在 opencode 上零厂商代码通过 text / tool / follow-up / abort / resume 五项 e2e，
  MCP stdio 挂载与调用也通过。

### kimi-code 2.1.1

- 入口：`kimi acp`（ACP v1，stdio）；`initialize` 回 `loadSession: true`、`mcpCapabilities.http/sse`。
  ACP 模板只填常量即通过 text / tool / follow-up / abort / resume / entry point 六项契约 e2e，
  外加工具参数、MCP、技能、模型四项厂商 e2e。
- **模型只能会话内切**：`kimi acp -m` 报 unknown option，`kimi -m <model> acp` 被静默忽略；
  `session/set_config_option`（`configId: "model"`）生效并回报 `currentValue`，`session/load` 保持切换后的
  模型。→ `MODEL_CONFIG_ID = "model"`。
- 工具调用：`tool_call` 以 `pending`、`rawInput: null`、标题 `Read` 到达，随后**数条不带 `rawInput` 的
  `in_progress`**，参数在之后一条 `in_progress` 里；`rawOutput` 是字符串。
- 文本块不带 `messageId`；`session/prompt` 应答不带 token 用量，`usage_update` 是上下文窗口占用。
- MCP 工具名 `mcp__{server}__{tool}`；项目技能目录 `.kimi-code/skills`（`.agents/skills` 也会被发现）。
- 错误只有 ACP 标准码：订阅不含所选模型与 5 小时额度耗尽**都是** `-32000`（"Authentication required:
  401/403 …"），无结构化字段可区分，只能归 `auth_required`；`config.toml` 里不存在的模型是 `-32603`。
  反复跑真实 e2e 会耗尽额度，之后所有 Turn 都失败——e2e 大面积失败先看错误正文是不是额度。
- 凭据在 `~/.kimi-code`：测试里改写 `HOME` 时要把真实 `HOME` 传给子进程，否则 `session/new` 回 `-32000`。
- 一次性模式：`kimi -p <prompt> --output-format stream-json`，会话 `-S <id>` 恢复、`-c` 继续上次；
  `-p` 不能与 `--yolo` 同用；未登录时报 `auth.login_required`（stderr）并以退出码 1 结束。
