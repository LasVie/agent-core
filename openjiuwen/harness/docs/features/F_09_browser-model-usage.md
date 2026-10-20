# Browser 任务的模型用量统计

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-10-20 |
| Refs | 本次不关联 issue |
| 范围 | `harness/tools/browser_move/playwright_runtime/runtime.py` |
| 关联 spec | `S_05_tools-contract.md` |
| 测试基线 | 2026-10-20，浏览器工具与结果窗口相关 755 项通过、18 项跳过 |

## 背景

Swarm 的 OpenCLI M4 评估需要比较纯页面路径与混合路径的成本。主 Agent 的用量记录在会话的
`chat.usage_summary` 中，但 Browser Agent 自身的模型调用不出现在会话历史、`llm.log` 或
`full.log` 中，无法得知子任务的模型轮次与 token。

## 数据结构与状态

- `after_model_call` 把每次模型响应累加到任务状态 `model_usage`：`model_calls`、
  `tool_calls`、`tool_calls_by_name`（最多 32 个名称）、`input_tokens`、`output_tokens`、
  `cache_tokens`、`total_cost`（取自响应的 `usage_metadata`，缺失的字段不累加）。
- 终态 `browser_result` 在有统计时附带 `model_usage`。

## 决策

1. 统计放在 Browser runtime 的任务状态中，随终态结果返回，父 Agent 与评估脚本无需额外通道。
2. 只累加，不影响状态判定、预算或证据。

## 拒绝的方案

- 写入独立日志：评估需要按会话关联，日志缺少稳定的会话标识。
- 由 Swarm 侧统计：Swarm 看不到 Browser Agent 内部的模型响应。

## 验证

新增 `test_browser_task_reports_its_own_model_usage`（三次模型响应的调用数、按名称的工具调用、
token 与费用累加）。浏览器工具与结果窗口相关共 755 项通过、18 项跳过。Swarm 的 M4 评估
（76 次对话）使用该字段统计 Browser Agent 成本。

## 已知遗留

- 该字段面向评估；`browser_result` 因此略有增大，父 Agent 会读到这部分内容。若团队不需要长期
  保留该指标，可改为仅写日志或在评估分支中保留。
