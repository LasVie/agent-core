# Browser 列表结果的按行证据

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-10-14 |
| Refs | 本次不关联 issue |
| 范围 | `harness/tools/browser_move/playwright_runtime/runtime.py`、`harness/prompts/tools/task_tool.py` |
| 关联 spec | `S_05_tools-contract.md` |
| 测试基线 | 2026-10-14，浏览器工具与结果窗口相关 563 项通过 |

## 背景

Swarm 的真实模型验收中，用户要求 B站热门前 3 条的标题、UP 主和链接。外部命令返回 3 行，
`browser_result` 判为 `completed`，但证据只有第一行的标题和链接；Agent 随后用页面探针
得到另一组链接，summary 和最终答复使用了探针的结果。原因有五处：

1. 字段别名没有"UP主 / uploader"，作者字段没有被要求。
2. 证据每个字段只保存一个值：外部结果只读取 `extracted`（第一行），`data` 只取哈希；
   卡片探针也只取第一张卡片。
3. `observed_result_count` 对所有来源取 `max()`，数量与字段值可以来自不同来源。
4. 已有的 `present` 槽位不会被后来的观察替换。
5. summary 是模型文本，从未与证据比对。

## 数据结构与状态

- 外部 action 结果新增可选字段 `records`：行列表，每行为字段到标量值的映射。键经
  `_canonical_field_name` 规范化。
- 运行时状态新增 `evidence_records`：来源 → 该来源最新一次报告的行。外部来源键为
  `external:<source.command>`，卡片探针为 `probe:<tool>`。每来源最多 20 行、20 个字段，
  最多保留 8 个来源；同一来源的新结果整体替换旧行。
- `_evidence_backed_count`：在同一来源中，统计带齐所需字段（`required_fields` 中已被任一
  来源覆盖的字段）的行数，取各来源最大值。没有任何来源报告行时返回 `None`。
- 终态 `browser_result` 新增 `records`（最佳来源的前 max(N, 5) 行）、`records_source`、
  `backed_result_count`，以及在 summary 引用了证据中不存在的链接时的 `unbacked_items`。

## 决策

1. 用独立的 `records` 字段表达多行结果，保留 `extracted` 的单值语义，旧工具不受影响。
2. 任一来源报告过行时，"前 N 条"只由同一来源的 N 行完整记录满足；没有行时沿用
   `observed_result_count`，保持未升级来源的行为。
3. 卡片探针也写入行，使页面路径与外部命令路径使用同一判定。
4. summary 与证据不一致时只标记 `unbacked_items`，不改变状态（产品决定：保持 completed
   并提示，避免把可能正确的答案判为 partial）。task_tool 描述同时告知父 Agent：列表回答
   以 `records` 为准，`unbacked_items` 不得当作已核实结果。
5. 别名只补充"uploader / UP主"，不加入"channel / 频道"，避免泛化地要求作者字段。

## 拒绝的方案

- 在 `extracted` 中用 `title_1`、`title_2` 等带序号的键：扩展了单值契约的含义，且数量仍可
  来自其他来源。
- 让后来的观察覆盖已有槽位：哪一次观察更可信取决于任务，覆盖规则会在别的场景出错。
- summary 不一致时降为 partial：误报会让可用答案被当作失败，并触发不必要的续跑。
- 在 Core 中解析具体站点 ID（如 BV 号）：Core 不认识具体站点，只比对链接。

## 验证

2026-10-14 使用 Swarm 的本地开发环境和当前可编辑 Core 运行：

```bash
python -m pytest tests/unit_tests/harness/tools/browser_move \
  tests/unit_tests/core/context_engine/test_tool_result_window_processor.py \
  -q -o addopts='' -o asyncio_mode=auto -W ignore::SyntaxWarning
```

共 563 项通过。新增测试覆盖：UP主 / uploader 别名；三行中只有一行带齐字段时报告
`result_count:1/3`，补齐后满足并返回 `records`；单行加 `observed_count=3` 不再满足前 3 条；
summary 中不在证据里的链接列入 `unbacked_items` 且状态保持 completed。原有不带 `records`
的外部结果测试保持不变，验证旧来源行为。Swarm 侧 `opencli_run` 同步输出 `records`。

## 已知遗留

- 链接比对只做规范化后的精确匹配（去掉末尾 `/` 和片段），跟踪参数不同的同一页面会被标记。
- summary 中的非链接条目（标题、ID）不做比对。
- 尚未用真实模型复验原问题对话。
