---
title: S_20 Recoverable Harness（步级检查点与回退）
id: S_20_recoverable-harness
type: S
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
---

# S_20 Recoverable Harness（步级检查点与回退）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/recoverable/`（`harness.py` / `rail.py` / `store.py` / `models.py` / `manifest.py` / `objects.py` / `workspace.py`） |
| 最近一次修订日期 | 2026-11-13 |
| 关联设计 | D-20261113-1506-recoverable-harness |
| 关联 feature | F_20261113_1706_recoverable-harness.md |

## 范围 / 边界

本规约定义 DeepAgent 的步级检查点和回退：`RecoverableHarness` 在每个步骤边界记下一个检查点，
包括上下文、Session 状态和工作区文件；人确认回退计划后，把它们恢复到指定检查点。

不回退的有三类，回退说明会如实告诉 Agent：依赖目录（`node_modules`、`.venv` 等）、工作区以外的效果，
以及沙箱中的工作区。只支持单轮模式，不支持任务循环模式。

上下文日志用 `core/session/vcs`（`for_session`、`commit`、`rewind`），vcs 本身的契约见其 `CLAUDE.md`。
Rail 的事件路由与 priority 规则见 S_04。

## 不变量

1. **检查点只在步骤边界建立。** 步骤边界指上下文中最后一条带工具调用的助手消息的每个调用都已有工具结果。
   停在确认中断上、工具结果还没写回时不建；本轮抛出异常时 `AFTER_INVOKE` 不建。
2. **没变化就不新建。** vcs 日志位置和工作区清单都与 HEAD 相同时复用 HEAD，所以同一步上的多次
   `BEFORE_MODEL_CALL`（模型重试）只产生一个检查点。只改了修改时间、内容没变的文件不算变化。
3. **历史只有一条线。** 每个检查点记一个父检查点；回退检查点的父检查点是回退目标。`seq` 从 0 开始，等于父检查点的 `seq` 加 1。
4. **回退必须经确认，且只在两轮之间执行。** `apply_rollback` 只接受与已存计划的 `plan_id`、`target_id` 都一致、
   且未执行过的确认；会话正在执行一轮时拒绝。执行时按当时的工作区重新比对，计划之后的改动也会被撤掉。
5. **回退不能撤销。** `apply_rollback` 立即删除目标之后的检查点和只被它们引用的清单，并把 HEAD 移到目标；
   下一轮开始时 vcs `rewind` 删除目标之后的上下文日志。被删除的检查点不能再作为 `plan_rollback` 的目标。
6. **文件立即恢复，上下文在下一轮开始时写回。** `apply_rollback` 先让工作区与目标清单一致，再留下待写回的回退；
   下一轮 `BEFORE_INVOKE` 时把目标的上下文和状态写回 Session，清掉 DeepAgent 的运行状态缓存，追加回退说明，
   并提交一个 `kind=rollback` 的检查点。写回失败时这一轮报错，不在旧路线上继续。
7. **重启后恢复。** `BEFORE_INVOKE` 时，若会话有 HEAD 而上下文池和 Session 状态里都没有消息，先把 HEAD 写回 Session，
   再记任何新内容，避免 vcs 把空 Session 记成"清空上下文"。重启后要在第一轮之前回退，宿主先调用
   `DeepAgent.ensure_initialized()`，让 Rail 拿到工作区。
8. **存储不在工作区里。** `storage_dir` 等于工作区根目录或位于其下时，设置工作区即报错。
9. **建立检查点失败不影响 Agent。** 只记日志，这一步不能作为回退目标。
10. **工作区恢复只看状态，不看是谁改的。** 恢复时重新扫描工作区，与目标清单比对：内容不同的写回，目标里没有的删除，
    目标里有而现在没有的重建。Agent、shell、脚本和用户的改动一样撤掉。
11. **第一份清单先于任何工具。** 会话的第一份清单在 `BEFORE_INVOKE` 时于后台开始建立；每个工具调用的 `BEFORE_TOOL_CALL`
    等它完成，模型调用不等。在它完成前建立的检查点引用这份清单。
12. **快照范围。** 工作区下的一切，除了：名字在依赖目录名单里的目录（任何深度）；名字在构建产物名单里、并且被 git 忽略的目录；
    设备、管道、套接字。符号链接记链接本身，不跟随。回退时删除当前存在的构建产物目录；依赖目录不动；
    比目标多出的空目录删除，非空的（里面只剩依赖目录或构建产物）保留。
13. **文件内容按 sha256 存，相同内容只存一份。** 能克隆就克隆；不能克隆时，超过单文件上限的文件不存，
    清单里记为 `skipped`，它变化后无法恢复，计划和结果里列为无法恢复。`.git` 下的文件不受上限限制。
14. **用文件属性判断变化。** 修改时间、大小、inode、权限位与上次扫描相同，且修改时间早于上次扫描开始前 2 秒，
    才沿用上次的指纹；否则重新读取。保留原修改时间、大小也不变的改写（例如 `cp -p`）在 2 秒以外发生时会漏掉。

## 接口契约

| 入口 | 说明 |
|---|---|
| `RecoverableHarness(*, kv_store=None, storage_dir=None, dependency_dirs=..., build_output_dirs=..., file_size_cap=1 GB, use_clones=True)` | `kv_store` 默认是 `storage_dir/recoverable.db` 上的 SQLite（`DbBasedKVStore`）；`storage_dir` 默认 `~/.openjiuwen/recoverable`，文件内容在 `storage_dir/objects/<工作区键>/`。依赖目录默认 `node_modules`、`.venv`、`venv`、`.tox`；构建产物默认 `dist`、`build`、`target`、`.next` |
| `rail() -> CheckpointRail` | 注册到 DeepAgent 的 Rail；一个 harness 服务一个 DeepAgent |
| `history(session_id, limit=None) -> list[Checkpoint]` | 从 HEAD 沿父检查点往回，最新的在前 |
| `uncovered_files(session_id) -> list[str]` | 最新清单中超过单文件上限、没有存下的文件 |
| `plan_rollback(session_id, target_id, *, initiator, source, reason) -> RollbackPlan` | 目标必须在当前历史上，否则报 `DEEPAGENT_INPUT_PARAM_ERROR`；目标可以是 HEAD 本身。计划列出要写回、删除、无法恢复的文件，要删除的构建产物，以及会消失的 git 提交数 |
| `apply_rollback(RollbackApproval) -> RollbackResult` | 不变量 4、5、6、10；结果列出实际写回、删除、删除的构建产物和没能恢复的文件 |
| `check_workspace(root)` / `bind_workspace(root, *, sandboxed)` | 不变量 8；`CheckpointRail` 在拿到工作区或 sys_operation 时调用。sys_operation 是沙箱模式时不做工作区快照 |
| `begin_round` / `end_round` / `before_tool` / `take_checkpoint` | 只供 `CheckpointRail` 调用 |

`CheckpointRail` 的回调：

| 事件 | 路由 | callback priority | 做什么 |
|---|---|---|---|
| `BEFORE_INVOKE` | 外层 DeepAgent | 10000 | 标记会话运行中；写回待写回的回退或重启后丢失的 Session（不变量 6、7）；会话还没有清单时在后台开始建第一份（不变量 11） |
| `BEFORE_MODEL_CALL` | 内层 ReAct | 1 | 建检查点：每轮开始，以及没调工具却继续循环的那一步 |
| `BEFORE_TOOL_CALL` | 内层 ReAct | 1 | 等第一份清单完成 |
| `AFTER_REACT_ITERATION` | 内层 ReAct | 1 | 建检查点：每步结束 |
| `AFTER_INVOKE` | 外层 DeepAgent | 1 | 本轮没有异常时建检查点；保存最新扫描供同一工作区的下一个会话复用；标记会话空闲 |

Rail 的 `priority` 是 1，在所有内置 Rail 之后 init，检查点记下同一边界上其他 Rail 处理完的状态。

**回退说明**是一条 `UserMessage`，`metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] = "rollback_note"`，以 `[ROLLBACK]` 开头，写明：
- 回到第几个检查点、丢弃了几个检查点；
- 工作区是否恢复，写回和删除了多少文件；
- 删除了哪些构建产物，需要重新构建；
- 哪些文件没能恢复；
- 依赖目录和工作区以外的效果没有回退；
- 回退理由。

同一轮开始前叠加的多次回退，说明按顺序合在一条消息里。

## 数据结构

| 记录 | 键（`BaseKVStore`） | 内容 |
|---|---|---|
| `Checkpoint` | `recoverable:{session_id}:checkpoint:{checkpoint_id}` | `seq`、`parent_id`、vcs `commit_id` 与 `event_id`、`manifest_id`、`kind`（`step` / `rollback`） |
| HEAD | `recoverable:{session_id}:head` | 当前检查点 ID |
| 清单 `ManifestRecord` | `recoverable:{session_id}:manifest:{manifest_id}` | 相对 `parent_id` 的变化（路径 → 条目，删除记 `null`）；`parent_id` 为空时是完整清单，每 50 份增量存一份完整的 |
| 工作区最新扫描 | `recoverable:workspace:{工作区键}:scan` | 完整清单，供同一工作区的新会话复用指纹 |
| `RollbackPlan` | `recoverable:plan:{plan_id}` | 目标、当时的 HEAD、要丢弃的检查点（最新的在前）、发起方、来源、理由、文件变化、会消失的提交数、`applied_at` |
| `PendingRollback` | `recoverable:{session_id}:pending` | 目标检查点、恢复后的清单、待写入的回退说明 |
| vcs 日志 | `{session_id}:vcs:...` | 由 vcs `KvBackend` 维护 |

清单条目是 `[kind, digest, size, mtime_ns, ino, mode, link]`，`kind` 为 `f`（文件）、`l`（链接）、`d`（目录）、`s`（超过上限未存）。
工作区键是工作区绝对路径 sha256 的前 16 位。持久化的记录都带 `schema_version`，读到未知版本报 `DEEPAGENT_RUNTIME_ERROR`。

## 与其它 spec 的关系

- S_04：`CheckpointRail` 遵守事件路由集合与 priority 规则，不新增事件。
- S_02：依赖 DeepAgent 的 `save_state` / `clear_state` / `react_agent.context_engine` / `ensure_initialized`；运行状态缓存在 Session 对象上，写回后必须清掉。
- S_09：工作区根目录取自 `Workspace.root_path`。
