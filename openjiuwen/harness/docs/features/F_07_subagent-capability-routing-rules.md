# 渐进式工具规则与子代理能力的边界

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-10-14 |
| Refs | 本次不关联 issue |
| 范围 | `harness/prompts/sections/progressive_tool_rail.py` |
| 关联 spec | `S_06_prompts-and-l10n.md` |
| 测试基线 | 2026-10-14，渐进式工具、tool_search、prompts 与子代理 rail 相关 144 项通过 |

## 背景

Swarm 的真实模型验收中，用户要求"把三步交给同一个 Browser Agent，使用 Bilibili adapter"。
主 Agent 在自己的 deferred 工具目录里调用 `tool_search`，没有找到匹配项，随后回答
"目前可用工具中没有 Bilibili adapter"，没有派发 Browser Agent。该 adapter 实际是
Browser Agent 的局部工具。

渐进式工具规则中有两处措辞把主 Agent 引向这个结论：

- "最新目录覆盖……以及 task_tool 或子代理描述中的工具名称"：本意是让被删除的工具不再被
  间接调用，但字面上让主 Agent 自己的目录压过子代理声明的能力。
- tool_search 最后一条规则在搜索结果不适用时直接要求"说明当前没有合适工具"，没有提示先考虑
  子代理。

子代理的工具不会进入主 Agent 的 deferred 目录，`tool_search` 无法证明子代理不具备某项能力。

## 数据结构与状态

本次只修改 `PROGRESSIVE_TOOL_RULES_BODY` 的中英文文案，不改变 rail 行为、目录快照、
附件或 `tool_search` 的检索逻辑。

- 「动态工具状态优先级」：最新目录仍覆盖初始目录、历史消息、历史 tool_search 结果和历史
  toolResult；对 task_tool 或子代理描述中的工具名称，只在最新目录将其标记为删除时才覆盖。
  明确子代理自身的能力不属于本目录。
- 「tool_search 使用规则」最后一条（中文第 7 条、英文第 8 条）：在说明没有合适工具之前，
  先检查 `task_tool` 中的子代理能否完成任务；网页、网站相关任务（包括提到某站点的 adapter
  或站点命令）交给浏览器子代理；仍无法完成时才调用 `ask_user` 或说明没有合适工具。

## 决策

1. 在通用规则层面修正，而不是在 Core 中写入 OpenCLI、站点名或具体 adapter。产品层通过
   子代理描述声明具体能力（Swarm 在 OpenCLI 启用时向 Browser Agent 卡片追加站点命令说明）。
2. 保留"被删除的工具不得通过 task_tool 或子代理间接调用"的约束，只收窄覆盖范围。
3. 只点名浏览器子代理处理网页任务，其他子代理仍按 task_tool 中的描述选择，不新增分派规则。
4. 中英文同步修改，保持两种语言规则编号与语义一致。

## 拒绝的方案

- 在主 Agent 的 deferred 目录中注册子代理工具：会把子代理局部工具暴露给主 Agent，破坏子代理
  隔离，并让主 Agent 尝试直接调用。
- 在 `tool_search` 结果中按站点名注入提示：需要 Core 认识产品层的站点目录；如真实模型复验
  仍出现误判，再评估由产品层通过扩展点提供提示。
- 修改 task_tool 描述的「何时不使用」规则：该规则主要约束代码检索类任务，本次问题出在
  渐进式工具规则的覆盖语义上。

## 验证

2026-10-14 使用 Swarm 的本地开发环境和当前可编辑 Core 运行：

```bash
python -m pytest tests/unit_tests/harness/test_progressive_tool_rail.py \
  tests/unit_tests/harness/test_tool_search_contract.py \
  tests/unit_tests/harness/test_prompt_report_categories.py \
  tests/unit_tests/harness/test_progressive_tool_index_lifecycle.py \
  tests/unit_tests/harness/prompts tests/unit_tests/harness/test_subagent_rail.py \
  -q -p no:cacheprovider --no-cov
```

共 144 项通过。新增 `test_rules_keep_subagent_capabilities_outside_the_tool_directory`
按中英文校验覆盖范围的收窄和"先考虑子代理"的规则。

## 已知遗留

- 规则只提高主 Agent 委派的概率，不强制委派；尚未用真实模型复验原失败对话。复验应同时覆盖
  不应委派的任务，确认规则没有让主 Agent 过度派发浏览器子代理。
- 修改对所有启用渐进式工具加载的 DeepAgent 生效，不只影响 Swarm 的 OpenCLI 场景。
- 子代理可用列表中浏览器能力仍由 `DEFAULT_BROWSER_CAPABILITIES` 固定生成；产品层追加的能力
  只能通过子代理卡片描述体现，尚无结构化扩展点。
