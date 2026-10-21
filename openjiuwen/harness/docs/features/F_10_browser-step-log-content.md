# Browser 子代理步骤日志的可选内容记录

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-10-21 |
| Refs | 本次不关联 issue |
| 范围 | `harness/tools/browser_move/playwright_runtime/status_logging.py` |
| 关联 spec | 无（日志内容，不改变模块契约） |
| 测试基线 | 2026-10-21，`test_browser_subagent_status_logging.py` 17 项通过 |

## 背景

Swarm 的 OpenCLI 评估需要知道 Browser Agent 每一步的选择及原因。`BrowserSubagentStatusLogger`
已默认把每步写入 `browser_agent.log`（`model_end`、`tool_start`、`tool_end`、`task_end`），足以还原
工具调用顺序与耗时，但模型文本、推理与参数值按设计被脱敏，无法分析"查看命令后改用页面"等行为。

## 数据结构与状态

- 新增环境变量 `BROWSER_SUBAGENT_STATUS_LOG_CONTENT`（默认关闭）。
- 开启时，`model_end` 的 `response_summary` 增加 `content`、`reasoning`（各最多 2,000 字符）；
  `tool_start` 增加 `args`（参数 JSON，最多 1,000 字符）。关闭时输出与之前完全相同。

## 决策

1. 复用现有状态日志与事件，不新增日志通道或存储。
2. 默认关闭，只用于评估；开启后日志包含页面与账号数据，需本地保存。

## 拒绝的方案

- 把模型消息写入会话历史：改变产品数据与存储结构。
- 复用 `OPENJIUWEN_BROWSER_AGENT_AUDIT_RAW`：该开关保存原始页面观察，不包含模型文本，含义不同。

## 验证

新增 `test_model_text_is_logged_only_when_content_logging_is_opted_in`：未设置时只有长度、无内容；
设置为 1 时包含模型文本与推理。`test_browser_subagent_status_logging.py` 共 17 项通过。

## 已知遗留

- 文本被截断，超长推理只保留前 2,000 字符。
