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
- openjiuwen/harness/recoverable/manifest.py
- openjiuwen/harness/recoverable/models.py
- openjiuwen/harness/recoverable/objects.py
- openjiuwen/harness/recoverable/rail.py
- openjiuwen/harness/recoverable/store.py
- openjiuwen/harness/recoverable/workspace.py
- tests/unit_tests/harness/recoverable/test_harness.py
- tests/unit_tests/harness/recoverable/test_workspace.py
designs:
- D-20261113-1506-recoverable-harness
design_note: null
---

# Recoverable Harness 实施与决策归档

## 关联依据

- 设计：[D-20261113-1506-recoverable-harness](../designs/active/D-20261113-1506-recoverable-harness/design.md)。
- spec：[S_20 Recoverable Harness](../specs/harness/S_20_recoverable-harness.md)，新增。
- 日期：2026-11-13。
- 实际实施范围：本记录按开发单元续写。已落地：
  - **U1 检查点骨架**：每个步骤边界自动建检查点；经人确认回退上下文和 Session 状态；回退说明；进程重启后从 vcs 恢复 Session。
  - **U2 工作区快照**：每个检查点记下工作区清单；回退时让工作区回到目标清单，不管是谁改的；
    依赖目录不进快照，被 git 忽略的构建产物不进快照、回退时删除；第一份清单在后台建立，工具调用等它完成；
    单文件上限；计划里列出文件变化和会消失的 git 提交数。

  副作用账本（U3）、shell 声明（U4）、依赖与全局安装（U5）、工作区外的文件（U6）、
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

U2 新增的做法：

- **新增三个模块：**
  - `manifest.py`：清单和它的增量存法；
  - `objects.py`：按 sha256 存文件内容，能克隆就克隆；
  - `workspace.py`：扫描、比对、恢复工作区，以及统计会消失的 git 提交。
- **恢复按状态比对，不靠逐步回放。** 恢复前重新扫一遍工作区，直接与目标清单比对：内容不同的写回，目标里没有的删除。
  这样确认期间、或 HEAD 之后用户改的文件也一并撤掉，不必另外收集"目标之后动过哪些路径"，而且回退的代价只和扫描及改动的文件数有关。
- **先克隆、再算指纹。** 能克隆时先把文件克隆到临时位置，再对克隆算指纹。克隆不会再变，指纹和存下的内容一定一致。
  不能克隆时边复制边算指纹，只读一遍文件。
- **构建产物的判断时机：** 遍历到名字在名单里的目录时，先用一次 `git check-ignore --stdin` 判断是否被忽略，被忽略就不进入，
  避免先扫描、算指纹再丢掉。不是 git 仓库时，没有目录算构建产物。
- **防"同一时刻改两次"：** 修改时间离上次扫描开始不到 2 秒的文件，下次一定重新读取，做法同 git 的 racy-git 处理。
- **清单和检查点都用 ID 引用。** 工作区没变时，新检查点沿用上一个清单 ID，不存新记录。
  回退时，只被删除的检查点引用的清单记录一起删掉。
- **同一工作区的新会话复用指纹：** 每轮结束时把最新扫描存为"工作区最新扫描"；新会话建第一份清单时，以它为缓存，属性没变的文件不重读。
- **写回文件用原子替换。** 先克隆或复制到目标文件所在目录下的临时文件，设好权限位，再 `os.replace`。

## 与设计的偏差

| 设计 | 实际 | 原因与影响 |
| --- | --- | --- |
| 执行回退时就写回上下文、提交回退检查点（§5.7.2、§5.11.4） | `apply_rollback` 只删除目标之后的检查点、移动 HEAD、留下待写回的回退；下一轮 `BEFORE_INVOKE` 时才写回上下文、截断 vcs、追加回退说明并提交回退检查点 | 两轮之间没有活的 Session 可写。不影响 Agent 看到的结果：Agent 本来就要等下一轮才继续。在下一轮开始前，HEAD 是目标检查点，回退检查点还不存在 |
| `plan_rollback` 的目标要在 HEAD 之前 | 目标可以是 HEAD 本身 | 用来丢弃 HEAD 之后没建检查点的内容，例如停在中断上的那一步 |
| 运行中收到信号，在步骤边界结束本轮（§5.7.2） | 未实现；会话运行中时 `apply_rollback` 直接拒绝 | 属于 U7 |
| 确认时列出文件、包、反向操作等（§5.11.5） | 计划列出要丢弃的检查点、文件变化、构建产物和会消失的 git 提交数 | 包和反向操作随 U3～U6 加入 |
| 回退说明写明副作用去向（§5.11.5） | 写明回到哪里、丢弃了几个检查点、文件恢复情况、理由，并说明依赖目录和外部效果没有回退 | 依赖和外部效果随 U3～U6 加入 |
| 检查点事件、回退事件（§5.3） | 未实现，失败只记日志 | 属于 U7 |
| 任务循环模式（§5.7.1 的 `AFTER_TASK_ITERATION`） | 不支持 | 任务循环模式下外层 invoke 没有 Session，写回和重启恢复都挂在外层 `BEFORE_INVOKE` 上 |
| 回退时找出目标之后动过的路径，再扫一次 HEAD 之后的改动（§5.11.2） | 重新扫描整个工作区，与目标清单比对 | 结果相同，更简单，而且不依赖中间清单是否完整。代价是每次回退多扫一遍工作区（本仓库约 170 毫秒） |
| 第一个"可能改文件"的工具等第一份清单（§5.11.2） | 所有工具调用都等 | 判断工具会不会改文件要靠 U3 的声明表。只影响每个会话的第一次工具调用 |
| 只被已删除检查点用到的文件内容立即清理（§5.10） | 不清理文件内容，只清理清单记录 | 内容按工作区共用，要知道哪些内容没人用，需要遍历该工作区所有会话的清单，和总量上限一起做（U7） |
| 重启后宿主可以直接回退 | 重启后要在第一轮之前回退，宿主需先调用 `DeepAgent.ensure_initialized()` | Rail 要在初始化时才拿到工作区；不调用的话，计划里会显示工作区不恢复 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-13 / U1：V-01、V-04 的一部分、V-06、V-17、P19 | 本地工作区，`feature/recoverable-harness`；Python 3.11.17，macOS arm64；真实 DeepAgent、Rail、ReAct、Session、vcs，模型用 `MockLLMModel` 脚本 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/test_harness.py tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：10 个用例（本特性 7 个，含 2 种存储参数；路由契约 3 个） | 任务循环模式、运行中信号、写回失败路径没有测试 |
| 2026-11-13 / U2：V-07、V-09、V-10、V-18 | 同上；工具直接写文件（模拟 shell），测试代码直接改文件（模拟用户）；APFS，git 2.x | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ -q -p no:cacheprovider` | 通过：14 个用例（U1 的 7 个 + U2 的 7 个） | ext4、NTFS 上的复制路径只用 `use_clones=False` 模拟；Linux 的 reflink 没有验证 |
| 2026-11-13 / U2 测试有效性 | 同上 | 临时把 racy 窗口改为 0、把等第一份清单改为不等，各跑对应用例 | 两个对应用例都失败，恢复后都通过 | — |
| 2026-11-13 / V4、V7 实测 | 本仓库工作目录（8,527 个文件，218 MB，含 `.git`），APFS | 直接调用 `WorkspaceSnapshot.scan`，各测克隆和复制 | 第一份清单：克隆 2.52 秒、复制 2.24 秒；之后每步扫描约 170 毫秒 | 系统缓存为空时、更大的仓库上没有测。克隆没有更快，因为时间主要花在算指纹 |

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
- **工作区（U2）**，回退后直接读磁盘上的文件核对：
  - 工具改的文件和用户改的文件都回到目标内容，目标之后新建的文件被删除；
  - 第一份清单比工具先完成，即使人为把它拖慢 0.3 秒；
  - 被 git 忽略的 `dist/` 被删除；没被忽略的 `build/` 正常快照和恢复；`node_modules` 不动；
  - git 提交被撤销：`rev-list --count` 回到 1，工作区与 HEAD 一致，计划里会消失的提交数是 1；
  - 大小、inode、修改时间都相同、只有内容不同的改写，也能被发现；
  - 超过上限的文件在 `uncovered_files`、计划和结果里列出；
  - 重启后（新的 harness，默认 SQLite）能把文件恢复到第 1 个检查点。
  - 回退说明里写着"Workspace files are back"以及写回和删除的数量。

## 验证结论

- **已证明的范围：** 单轮模式下，检查点在步骤边界建立并能去重；确认后的上下文回退和回退说明；同一进程内和"新 harness + 空 Session"两种情况下的恢复；
  不匹配或已执行过的确认被拒绝；存储在工作区内时被拒绝。
- **未覆盖的：** 任务循环模式；回退写回失败时本轮报错的路径；正在运行时拒绝回退的路径（`_running` 只在进程内有效）；真实 CLI 的重启流程。
- **U2 已证明的范围：** 本机 APFS 上的工作区快照与恢复，包括上面列出的各种情况。
- **U2 未覆盖的：** Linux（reflink、inotify 上限都不涉及，但复制路径只是模拟的）、Windows；文件属主和目录权限的恢复；
  写回失败后的重试；回退时有其他进程正在写工作区。
- **交付判断：** U1、U2 满足各自范围；整个特性尚未交付。

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
| 外部效果和依赖目录 | 未实现 | 回退说明里已如实写明 | U3～U6 |
| 文件内容从不清理 | 未实现 | 内容目录只增不减 | U7（总量上限与清理） |
| 文件写回失败不重试 | 未实现 | 失败的文件列在结果和回退说明里 | 设计 D-10 要求重试，随 U3 的执行顺序一起做 |
| 文件属主、目录权限位、扩展属性不恢复 | 有意取舍 | 只恢复文件内容和文件的权限位 | 有需要再做 |
| 保留原修改时间、大小也不变的改写（如 `cp -p`），发生在上次扫描 2 秒以外 | 已知限制（V6） | 不会被发现，回退时不恢复 | 改用文件变更通知时可以解决（R-08） |
| 沙箱模式 | 有意取舍 | 不做工作区快照，回退说明写明文件没有回退 | P3 |

## 实施演进与修订

### 2026-11-13 U2：回退说明改为如实说明文件已恢复

- 原结论：U1 的回退说明写"工作区文件和外部效果没有回退"。
- 变化：工作区快照可用时，说明改为写明写回和删除了多少文件、删除了哪些构建产物、哪些文件没能恢复，
  以及依赖目录和工作区以外的效果仍没有回退。快照不可用时（沙箱、目标检查点没有清单），仍写"工作区文件没有回退"。
- 旧结论的效力：只在快照不可用时仍然适用。
