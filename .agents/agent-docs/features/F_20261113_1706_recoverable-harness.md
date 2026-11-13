---
id: F_20261113_1706_recoverable-harness
title: Recoverable Harness 实施与决策归档
type: F
author: Song Haoran
author_email: song.haoran@h-partners.com
modules:
- harness
files:
- openjiuwen/harness/recoverable/__init__.py
- openjiuwen/harness/recoverable/harness.py
- openjiuwen/harness/recoverable/models.py
- openjiuwen/harness/recoverable/rail.py
- openjiuwen/harness/recoverable/store.py
- tests/unit_tests/harness/recoverable/test_harness.py
designs:
- D-20261113-1506-recoverable-harness
design_note: null
---

# Recoverable Harness 实施与决策归档

## 关联依据

- 设计：[D-20261113-1506-recoverable-harness](../designs/active/D-20261113-1506-recoverable-harness/design.md)。
- spec：[S_20 Recoverable Harness](../specs/harness/S_20_recoverable-harness.md)，新增。
- 日期：2026-11-13。
- 实际实施范围：本记录按开发单元续写。已落地的是 **U1 检查点骨架**：
  - 每个步骤边界自动建检查点；
  - 经人确认回退上下文和 Session 状态；
  - 回退说明；
  - 进程重启后从 vcs 恢复 Session。

  工作区文件（U2）、副作用账本（U3）、shell 声明（U4）、依赖与全局安装（U5）、工作区外的文件（U6）、
  运行中的信号与并发控制（U7）、MCP 提示（U8）尚未实现。

## 决策归属与关系

新特性，首次建档。方案决策以设计 D-20261113-1506 为准，本记录只写实施中的选择和偏差。

## 上下文与跨边界影响

已核实的事实（2026-11-13，读代码确认）：

- **vcs 此前没有生产使用方**，④ 是第一个。`for_session` 的写回会整体替换 Session 状态（去掉 `context` 键），
  上下文由各个 context 实例的 `load_state` 写回。
- **DeepAgent 把运行状态缓存在 Session 对象上**（`load_state` / `save_state`），一轮结束时 `save_state` 后 `clear_state`。
  在一轮中途写回 Session 状态后，必须 `clear_state`，否则之后读到的还是旧缓存。
- **`ContextEngine.create_context` 命中缓存时会从 Session 重新加载**；Session 状态里没有 `context` 键时保留实例现有内容。
  所以在 `BEFORE_INVOKE` 写回后，ReAct 的 `_init_context` 拿到的就是写回的上下文。
- **`conversation_id` 方式调用时每轮都新建 Session**，状态靠 checkpointer 的 `pre_run` 恢复；两轮之间不存在活的 Session 对象。
- **模型只回文字、循环却继续的 4 种情况**（[react_agent.py](../../../openjiuwen/core/single_agent/agents/react_agent.py)
  中没有工具调用的分支）都跳过 `AFTER_REACT_ITERATION`，对应 P9。

## 实际实现与开发决策

- **包结构：** `openjiuwen/harness/recoverable/`：
  - `RecoverableHarness`：对外入口；
  - `CheckpointRail`：DeepAgent 一侧；
  - `CheckpointStore`：检查点、HEAD、计划和待写回的回退，存在 `BaseKVStore` 里；
  - `models.py`：数据结构。
- **检查点只记 vcs 位置。** 每个检查点记 vcs 的 `commit_id` 和 `event_id`。建立前先 `append`，如果 `event_id` 和 HEAD 相同就复用 HEAD，
  所以模型重试、没有变化的 `BEFORE_MODEL_CALL` 都不会产生新检查点。
- **在 `BEFORE_MODEL_CALL` 上，只要有变化就建检查点。** 每轮开始和 P9 的情况用同一条规则处理，不用区分是第几次模型调用。
- **步骤边界的判断：** 只看最后一条带工具调用的助手消息，它的每个调用是否都已有工具结果。
  更早的调用默认已完成，这样上下文压缩删掉早期消息也不会让检查点永远建不了。
- **默认存储：** `storage_dir/recoverable.db` 上的 SQLite（`DbBasedKVStore` + `aiosqlite`），vcs 用同一个 store 的 `KvBackend`。
  这满足 P19 "默认持久化"的要求。
- **写回时机：** 回退的写回和重启后的恢复都放在外层 `BEFORE_INVOKE`，callback priority 10000，排在其他 Rail 之前。
  这时 Session 已由 `pre_run` 恢复、ReAct 还没读 Session；即使会话停在中断上，写回也发生在 ReAct 读取中断状态之前。
- **错误码：** 复用 `DEEPAGENT_INPUT_PARAM_ERROR`、`DEEPAGENT_RUNTIME_ERROR`、`DEEPAGENT_CONFIG_PARAM_ERROR`，
  不在 core 中新增 StatusCode。

## 与设计的偏差

| 设计 | 实际 | 原因与影响 |
| --- | --- | --- |
| 执行回退时就写回上下文、提交回退检查点（§5.7.2、§5.11.4） | `apply_rollback` 只删除目标之后的检查点、移动 HEAD、留下待写回的回退；下一轮 `BEFORE_INVOKE` 时才写回上下文、截断 vcs、追加回退说明并提交回退检查点 | 两轮之间没有活的 Session 可写。不影响 Agent 看到的结果：Agent 本来就要等下一轮才继续。在下一轮开始前，HEAD 是目标检查点，回退检查点还不存在 |
| `plan_rollback` 的目标要在 HEAD 之前 | 目标可以是 HEAD 本身 | 用来丢弃 HEAD 之后没建检查点的内容，例如停在中断上的那一步 |
| 运行中收到信号，在步骤边界结束本轮（§5.7.2） | 未实现；会话运行中时 `apply_rollback` 直接拒绝 | 属于 U7 |
| 确认时列出文件、包、反向操作等（§5.11.5） | 计划里只有要丢弃的检查点 | 其余内容随 U2～U6 加入 |
| 回退说明写明副作用去向（§5.11.5） | 只写回到哪里、丢弃了几个检查点、理由，并说明文件和外部效果没有回退 | U1 还不回退这些东西，说明必须如实 |
| 检查点事件、回退事件（§5.3） | 未实现，失败只记日志 | 属于 U7 |
| 任务循环模式（§5.7.1 的 `AFTER_TASK_ITERATION`） | 不支持 | 任务循环模式下外层 invoke 没有 Session，写回和重启恢复都挂在外层 `BEFORE_INVOKE` 上 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-13 / V-01、V-04 的一部分、V-06、V-17、P19 | 本地工作区，`feature/recoverable-harness`；Python 3.11.17，macOS arm64；真实 DeepAgent、Rail、ReAct、Session、vcs，模型用 `MockLLMModel` 脚本 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/test_harness.py tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：10 个用例（本特性 7 个，含 2 种存储参数；路由契约 3 个） | 任务循环模式、运行中信号、写回失败路径没有测试 |

### 可见行为、副作用与证据有效范围

- **模型实际收到的输入**（`MockLLMModel.call_history`）：
  - 回退后的下一轮里，被丢弃的第二步（`call_2`）和上一轮的结束回复都不在了；
  - `[ROLLBACK]` 说明排在新的用户输入之前，写着"checkpoint #1"和理由。
  - 重启后的那一轮里，模型收到了上一个进程第一轮的全部消息。
- **检查点：**
  - 一轮两次工具调用产生 4 个检查点：每轮开始、两步结束、每轮结束；
  - 停在确认中断上时只有每轮开始那 1 个，确认继续后补上 2 个；
  - 回退检查点的父检查点是目标，`seq` 接续；
  - 被删除的检查点再用作目标时报错。
- **存储：** 重启用例在默认 SQLite 和注入的内存存储上各跑一次。"重启"的模拟方式是：新建 harness 和 DeepAgent，传入一个空的 Session。
  没有验证真实进程重启后 CLI 能否按 session_id 重新打开会话。

## 验证结论

- **已证明的范围：** 单轮模式下，检查点在步骤边界建立并能去重；确认后的上下文回退和回退说明；同一进程内和"新 harness + 空 Session"两种情况下的恢复；
  不匹配或已执行过的确认被拒绝；存储在工作区内时被拒绝。
- **未覆盖的：** 任务循环模式；回退写回失败时本轮报错的路径；正在运行时拒绝回退的路径（`_running` 只在进程内有效）；真实 CLI 的重启流程。
- **交付判断：** U1 满足自身范围，可以作为 U2、U3 的基础；整个特性尚未交付。

### 覆盖与资料同步

- 已读并核对：vcs `manager` / `adapter` / `config` / `kv_backend`，`ContextEngine.create_context` / `_load_state_from_session` / `save_contexts`，
  ReAct 循环中没有工具调用的分支和用户输入准入，DeepAgent 的 `invoke`、`_prepare_single_round_session`、`save_state` / `load_state` / `clear_state`，
  Rail 基类和 `DeepAgentRail`。
- 新增 S_20；在 `openjiuwen/harness/AGENTS.md` 的模块地图和 spec 对照表里加入 `recoverable/`；
  设计第 3、10 节记下了开工前定下的 P8、P9、P10、P19、P22、P23。

## 已知遗留

| 遗留项 | 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| 会话停在中断上时执行回退，宿主随后又发来恢复中断的输入 | 未实现 | 目标状态里已经没有中断记录，这次恢复的结果没有定义。宿主在回退后应当发新问题，不要发恢复输入 | U7 |
| 宿主用同一个 session_id 开始一段新对话 | 有意取舍 | Session 为空时会恢复旧上下文。同一个 session_id 视为同一段对话 | 写进接入说明 |
| CLI 能否按 session_id 重新打开会话 | 未核实 | 不能的话，CLI 用户在重启后无法回退 | U7 或 CLI 接入时核实 |
| 回退计划记录从不清理 | 未实现 | 存储慢慢增长 | 和总量上限、清理一起做（U7） |
| 任务循环模式 | 未实现 | 不建检查点 | 待定 |
| 文件和外部效果 | 未实现 | 回退说明里已如实写明 | U2～U6 |
