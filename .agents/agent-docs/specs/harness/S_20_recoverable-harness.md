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
- openjiuwen/harness/recoverable/models.py
- openjiuwen/harness/recoverable/rail.py
- openjiuwen/harness/recoverable/store.py
---

# S_20 Recoverable Harness（步级检查点与回退）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/recoverable/`（`harness.py` / `rail.py` / `store.py` / `models.py`） |
| 最近一次修订日期 | 2026-11-13 |
| 关联设计 | D-20261113-1506-recoverable-harness |
| 关联 feature | F_20261113_1706_recoverable-harness.md |

## 范围 / 边界

本规约定义 DeepAgent 的步级检查点和上下文回退：`RecoverableHarness` 在每个步骤边界记下一个检查点，
人确认回退计划后，把上下文和运行状态恢复到指定检查点。当前只覆盖上下文与 Session 状态；
工作区文件和工作区以外的效果**不回退**，回退说明会如实告诉 Agent。只支持单轮模式，不支持任务循环模式。

上下文日志用 `core/session/vcs`（`for_session`、`commit`、`rewind`），vcs 本身的契约见其 `CLAUDE.md`。
Rail 的事件路由与 priority 规则见 S_04。

## 不变量

1. **检查点只在步骤边界建立。** 步骤边界指上下文中最后一条带工具调用的助手消息的每个调用都已有工具结果。
   停在确认中断上、工具结果还没写回时不建；本轮抛出异常时 `AFTER_INVOKE` 不建。
2. **没变化就不新建。** vcs 日志位置与 HEAD 检查点相同时复用 HEAD，所以同一步上的多次 `BEFORE_MODEL_CALL`
   （模型重试）只产生一个检查点。
3. **历史只有一条线。** 每个检查点记一个父检查点；回退检查点的父检查点是回退目标。`seq` 从 0 开始，等于父检查点的 `seq` 加 1。
4. **回退必须经确认，且只在两轮之间执行。** `apply_rollback` 只接受与已存计划的 `plan_id`、`target_id` 都一致、
   且未执行过的确认；会话正在执行一轮时拒绝。
5. **回退不能撤销。** `apply_rollback` 立即删除目标之后的检查点并把 HEAD 移到目标；下一轮开始时
   vcs `rewind` 删除目标之后的上下文日志。被删除的检查点不能再作为 `plan_rollback` 的目标。
6. **上下文在下一轮开始时写回。** 两轮之间没有活的 Session 可写，所以 `apply_rollback` 只留下待写回的回退；
   下一轮 `BEFORE_INVOKE` 时把目标的上下文和状态写回 Session，清掉 DeepAgent 的运行状态缓存，追加回退说明，
   并提交一个 `kind=rollback` 的检查点。写回失败时这一轮报错，不在旧路线上继续。
7. **重启后恢复。** `BEFORE_INVOKE` 时，若会话有 HEAD 而上下文池和 Session 状态里都没有消息，先把 HEAD 写回 Session，
   再记任何新内容，避免 vcs 把空 Session 记成"清空上下文"。
8. **存储不在工作区里。** `storage_dir` 等于工作区根目录或位于其下时，设置工作区即报错。
9. **建立检查点失败不影响 Agent。** 只记日志，这一步不能作为回退目标。

## 接口契约

| 入口 | 说明 |
|---|---|
| `RecoverableHarness(*, kv_store=None, storage_dir=None)` | `kv_store` 默认是 `storage_dir/recoverable.db` 上的 SQLite（`DbBasedKVStore`）；`storage_dir` 默认 `~/.openjiuwen/recoverable` |
| `rail() -> CheckpointRail` | 注册到 DeepAgent 的 Rail；一个 harness 服务一个 DeepAgent |
| `history(session_id, limit=None) -> list[Checkpoint]` | 从 HEAD 沿父检查点往回，最新的在前 |
| `plan_rollback(session_id, target_id, *, initiator, source, reason) -> RollbackPlan` | 目标必须在当前历史上，否则报 `DEEPAGENT_INPUT_PARAM_ERROR`；目标可以是 HEAD 本身，用来丢弃 HEAD 之后没建检查点的内容 |
| `apply_rollback(RollbackApproval) -> RollbackResult` | 不变量 4、5、6 |
| `check_workspace(root)` | 不变量 8，`CheckpointRail.set_workspace` 调用 |
| `begin_round` / `end_round` / `take_checkpoint` | 只供 `CheckpointRail` 调用 |

`CheckpointRail` 的回调：

| 事件 | 路由 | callback priority | 做什么 |
|---|---|---|---|
| `BEFORE_INVOKE` | 外层 DeepAgent | 10000 | 标记会话运行中；写回待写回的回退或重启后丢失的 Session（不变量 6、7） |
| `BEFORE_MODEL_CALL` | 内层 ReAct | 1 | 建检查点：每轮开始，以及没调工具却继续循环的那一步 |
| `AFTER_REACT_ITERATION` | 内层 ReAct | 1 | 建检查点：每步结束 |
| `AFTER_INVOKE` | 外层 DeepAgent | 1 | 本轮没有异常时建检查点；标记会话空闲 |

Rail 的 `priority` 是 1，在所有内置 Rail 之后 init，检查点记下同一边界上其他 Rail 处理完的状态。

**回退说明**是一条 `UserMessage`，`metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] = "rollback_note"`，
以 `[ROLLBACK]` 开头，写明回到第几个检查点、丢弃了几个检查点、文件和外部效果没有回退，以及理由。
同一轮开始前叠加的多次回退，说明按顺序合在一条消息里。

## 数据结构

| 记录 | 键（`BaseKVStore`） | 内容 |
|---|---|---|
| `Checkpoint` | `recoverable:{session_id}:checkpoint:{checkpoint_id}` | `seq`、`parent_id`、vcs `commit_id` 与 `event_id`、`kind`（`step` / `rollback`） |
| HEAD | `recoverable:{session_id}:head` | 当前检查点 ID |
| `RollbackPlan` | `recoverable:plan:{plan_id}` | 目标、当时的 HEAD、要丢弃的检查点（最新的在前）、发起方、来源、理由、`applied_at` |
| `PendingRollback` | `recoverable:{session_id}:pending` | 目标检查点、待写入的回退说明 |
| vcs 日志 | `{session_id}:vcs:...` | 由 vcs `KvBackend` 维护 |

持久化的记录都带 `schema_version`，读到未知版本报 `DEEPAGENT_RUNTIME_ERROR`。

## 与其它 spec 的关系

- S_04：`CheckpointRail` 遵守事件路由集合与 priority 规则，不新增事件。
- S_02：依赖 DeepAgent 的 `save_state` / `clear_state` / `react_agent.context_engine`；运行状态缓存在 Session 对象上，写回后必须清掉。
