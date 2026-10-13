# Browser runtime 的外部工具契约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-10-13 |
| Refs | 本次不关联 issue |
| 范围 | `harness/subagents/browser_agent.py`、`harness/tools/browser_move/playwright_runtime/`、`core/context_engine/processor/offloader/tool_result_window_processor.py` |
| 关联 spec | `S_05_tools-contract.md`、`S_18_subagents-and-lifecycle.md` |
| 测试基线 | 2026-10-13，浏览器工具与结果窗口相关 559 项通过 |

## 背景

Browser Agent 的工具列表可以包含在 Playwright 页面之外执行的应用工具。现有页面引用、
阶段预算和页面 generation 不能代表这些工具的工作页状态。产品层需要保留自己的执行方式，
同时让发现、操作、证据和人工处理结果参与现有 Browser runtime。

## 数据结构与状态

- 工具通过 `ToolCard.properties["browser_runtime"]` 声明 `role`、`windowed` 和
  `inline_limit_chars`。角色为 `discovery` 或 `action`；没有声明时维持原有行为。
- discovery 不消耗操作/阶段预算，也不推动 Playwright 的页面状态。
- action 维护独立的任务账本：最多 16 次操作，同一工具与完整参数最多执行 2 次。
  参数先规范化 JSON 再取哈希，保留嵌套字段与长参数，不将参数原文保存在账本。
- action 结果中的 `extracted`、`field_provenance`、`source` 与 `observed_count`
  进入既有证据和完成判定；外部观察保留来源及内容哈希。
- `user_action_required` 使当前 Browser 任务以 blocked 强制结束。工作页保留、确认和用户
  处理后重新派发属于产品宿主，Core 不替宿主恢复页面。
- `browser_remaining_time()` 从回调上下文读取任务与本轮 deadline 的剩余时间。
- `ToolResultWindowProcessorConfig.inline_limit_by_tool` 允许刚加入窗口的大结果立即卸载，
  避免最新一条长命令输出仍完整进入模型上下文。

## 决策

1. 用工具卡片 properties 声明运行时参与方式，Core 不引用 OpenCLI、站点名或命令目录。
2. 传入 Browser Agent 的工具保留原实例，允许应用工具使用自己的执行器和结果渲染。
3. 外部 action 的预算与重复判定独立于 Playwright；它不修改主页面引用或 generation。
4. 复用当前结构化证据与 blocked 终态，控制参数和写确认留在应用层。
5. 结果大小限制是通用窗口配置，由 Browser Agent 装配时根据声明选用。

## 拒绝的方案

- 在 Core 中内置 OpenCLI 命令或站点规则：会将产品适配器和 SDK 生命周期绑定。
- 将外部命令伪装成 `browser_*` 页面操作：会错误地更新主页面状态并消耗页面阶段预算。
- 用 Playwright 的简化语义签名判断任意命令重复：会丢失命令身份、嵌套字段和长参数。
- 让 Core 管理 Electron 工作页或写操作确认：这些是具体产品宿主的职责。

## 验证

2026-10-13 使用 Swarm 的本地开发环境和当前可编辑 Core 运行：

```powershell
python -m pytest tests/unit_tests/harness/tools/browser_move tests/unit_tests/core/context_engine/test_tool_result_window_processor.py -q -o addopts='' -o asyncio_mode=auto -W ignore::SyntaxWarning
```

共 559 项通过，其中外部工具测试覆盖角色声明、工具实例保留、独立预算、完整参数重复
判定、结构化证据、人工处理终止和 deadline；窗口测试覆盖最新结果超过单工具阈值时卸载。
测试日志及 XML 保存在本地的 2026-10-13 提交复验目录。

静态检查未通过：当前环境没有 `make` 和 `codespell`，已直接运行 Makefile 对应的
Ruff/Pylint 检查，仍有导入排序、格式和代码诊断；完整输出保存在同目录的
`core-lint.log`。本次保存现有进度，不对整个浏览器运行时进行格式化或无关重构。

## 已知遗留

本次只归档并提交已有实现。Swarm 的真实模型验收已发现：主 Agent 可能错误地用自己的
工具搜索判断子 Agent 能力；列表摘要可能与首行结构化证据不一致；Swarm 的模型重绑定
调用与当前 Core 参数不兼容。相关问题尚未修复，不能将上述测试通过解释为完整产品验收
通过。该契约也不证明桌面 CLI 或多成员产品装配已经完成。
