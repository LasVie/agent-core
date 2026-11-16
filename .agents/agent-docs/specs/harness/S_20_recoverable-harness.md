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
- openjiuwen/harness/recoverable/effects.py
- openjiuwen/harness/recoverable/harness.py
- openjiuwen/harness/recoverable/manifest.py
- openjiuwen/harness/recoverable/models.py
- openjiuwen/harness/recoverable/objects.py
- openjiuwen/harness/recoverable/processes.py
- openjiuwen/harness/recoverable/rail.py
- openjiuwen/harness/recoverable/store.py
- openjiuwen/harness/recoverable/workspace.py
---

# S_20 Recoverable Harness（步级检查点与回退）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 关联模块 | `openjiuwen/harness/recoverable/`（`harness.py` / `rail.py` / `store.py` / `models.py` / `manifest.py` / `objects.py` / `workspace.py` / `effects.py` / `processes.py`） |
| 最近一次修订日期 | 2026-11-16 |
| 关联设计 | D-20261113-1506-recoverable-harness |
| 关联 feature | F_20261113_1706_recoverable-harness.md |

## 范围 / 边界

本规约定义 DeepAgent 的步级检查点和回退：`RecoverableHarness` 在每个步骤边界记下一个检查点，
包括上下文、Session 状态和工作区文件；工具调用对工作区以外的影响记在副作用账本里。
人确认回退计划后，能撤销的外部效果按声明撤销，再把工作区、上下文和状态恢复到指定检查点。

不回退的，回退说明会如实告诉 Agent：
- 依赖目录（`node_modules`、`.venv` 等）、Agent 的记忆目录；
- 不可逆、无法撤销、结果未知的外部效果，账本里没有的外部效果；
- 沙箱中的工作区。

只支持单轮模式，不支持任务循环模式。

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
    工作区节点 `memory`、`coding_memory` 所在的目录；设备、管道、套接字。符号链接记链接本身，不跟随。
    回退时删除当前存在的构建产物目录；依赖目录和记忆目录不动；比目标多出的空目录删除，非空的（里面只剩不进快照的目录）保留。
13. **文件内容按 sha256 存，相同内容只存一份。** 能克隆就克隆；不能克隆时，超过单文件上限的文件不存，
    清单里记为 `skipped`，它变化后无法恢复，计划和结果里列为无法恢复。`.git` 下的文件不受上限限制。
14. **用文件属性判断变化。** 修改时间、大小、inode、权限位与上次扫描相同，且修改时间早于上次扫描开始前 2 秒，
    才沿用上次的指纹；否则重新读取。保留原修改时间、大小也不变的改写（例如 `cp -p`）在 2 秒以外发生时会漏掉。
15. **工具的类别来自声明表，shell 和执行代码逐条声明。** 宿主的声明覆盖内置表；两者都没有的工具按不可逆处理。
    `bash`、`code` 的每次调用由模型在参数里声明：`side_effect`（`none` / `reversible` / `irreversible`）、
    `reverse_command`（`reversible` 时的反向 shell 命令）、`external_paths`（会写的工作区外路径）。
    没有声明的按不可逆；声明为 `none`、但 shell 命令命中对外命令名单（`outward_commands`、`git push`、写请求的 `curl` / `wget`）的，
    改按不可逆登记，命令照常执行。宿主在声明表里写了 `bash` 或 `code` 时，以宿主的为准，不再逐条声明。
    类别为"无外部效果"的调用不进账本，后台启动的 shell 命令除外（见不变量 22）。
16. **先登记、后执行。** 进账本的调用在 `BEFORE_TOOL_CALL` 写入意图记录；写入失败时这次调用被跳过，
    模型收到错误结果，说明副作用无法登记。已被更早的 Rail 跳过的调用不登记。
17. **调用后补结果。** `AFTER_TOOL_CALL` / `ON_TOOL_EXCEPTION` 在其他 Rail 改写结果之前读取原始结果：
    抛出异常或结果的 `success` 为 `False` 记为失败；成功且可撤销的，按声明从调用结果或参数中取出撤销参数，取不到时记为无法撤销。
    调用因中断暂停时保留意图记录，回退时视为结果未知。
18. **回退时对每条记录的处理。** 目标检查点之后、未被放弃、不是撤销调用本身的记录：
    "保留"类列为保留；不可逆类列为不可逆；失败了但有撤销参数的（例如退出码不为 0 的 shell 命令）列为待人确认（`review`），
    只有人在 `RollbackApproval.confirmed_effect_ids` 里确认才执行；其余没有成功结果的列为结果未知；
    可撤销但缺撤销参数的列为无法撤销；其余执行撤销。都不悄悄丢掉。
19. **执行顺序：撤销 → 恢复工作区 → 放弃记录与检查点。** 撤销从最新的记录往前执行，跳过人在确认时划掉的。
    第一次撤销失败后，其余撤销不再执行（记为待执行），工作区照常恢复；工作区写回失败的文件重试一次。
    有撤销失败、待执行或未恢复的文件时，回退状态为 `partial`，否则为 `completed`；人划掉的撤销不算失败。
20. **撤销调用自己也进账本**，带被撤销记录的 ID 和回退 ID。同一回退里已成功的撤销不再执行；
    上次执行被中断、结果未知的撤销不自动重试，改为报告。撤销直接调用 Agent 的工具，在 Agent 的执行环境里运行，不经过模型和 Rail。
    模型写的反向命令执行前再检查一次：含命令替换、进程替换等注入写法的，或被 Agent 的权限策略判为 `deny` 的，不执行，记为撤销失败；
    `ask` 视为已由人在确认计划时同意。反向命令在工作区根目录执行。
21. **`partial` 的回退不自动继续。** `resume_rollback` 重试失败和待执行的撤销，再恢复一次工作区，并在下一轮开始时给 Agent 一条说明；
    `accept_partial` 接受现状。
22. **后台进程在回退时先停掉。** 用 `run_in_background` 启动的 shell 命令一律进账本，记下 pid 和 `ps` 报告的启动时间。
    回退时，在撤销和恢复工作区之前，停掉目标之后启动、仍在运行且启动时间一致的进程组（先 `SIGTERM`，2 秒后 `SIGKILL`）；
    启动时间不一致的（pid 已被别的进程占用）不动，只报告。没有进程组信号的平台只报告。
23. **声明参数只在开启 ④ 时出现。** `CheckpointRail.init` 和每轮 `BEFORE_INVOKE` 把三个参数加进 `bash`、`code` 工具卡片的
    `input_params`，`side_effect` 列为必填；描述随 `DeepAgentConfig.language` 用中文或英文。工具本身忽略这些参数。

## 接口契约

| 入口 | 说明 |
|---|---|
| `RecoverableHarness(*, kv_store=None, storage_dir=None, dependency_dirs=..., build_output_dirs=..., file_size_cap=1 GB, use_clones=True, declarations=None)` | `kv_store` 默认是 `storage_dir/recoverable.db` 上的 SQLite（`DbBasedKVStore`）；`storage_dir` 默认 `~/.openjiuwen/recoverable`，文件内容在 `storage_dir/objects/<工作区键>/`。依赖目录默认 `node_modules`、`.venv`、`venv`、`.tox`；构建产物默认 `dist`、`build`、`target`、`.next`。`declarations` 是宿主工具的 `EffectDeclaration`，按工具名；`outward_commands` 默认 `ssh`、`scp`、`sftp`、`kubectl`、`helm`、`docker`、`podman`、`aws`、`gcloud`、`az`、`terraform`、`psql`、`mysql`、`mongosh`、`redis-cli`、`crontab`、`systemctl`、`launchctl` |
| `rail() -> CheckpointRail` | 注册到 DeepAgent 的 Rail；一个 harness 服务一个 DeepAgent |
| `history(session_id, limit=None) -> list[Checkpoint]` | 从 HEAD 沿父检查点往回，最新的在前 |
| `uncovered_files(session_id) -> list[str]` | 最新清单中超过单文件上限、没有存下的文件 |
| `plan_rollback(session_id, target_id, *, initiator, source, reason) -> RollbackPlan` | 目标必须在当前历史上，否则报 `DEEPAGENT_INPUT_PARAM_ERROR`；目标可以是 HEAD 本身。计划列出要写回、删除、无法恢复的文件，要删除的构建产物，会消失的 git 提交数，以及每条外部效果的处理方式（`effects`，最新的在前） |
| `apply_rollback(RollbackApproval) -> RollbackResult` | 不变量 4、5、6、10、19、22；`RollbackApproval.cancelled_effect_ids` 是人划掉的撤销，`confirmed_effect_ids` 是人确认执行的 `review` 项。执行时重新收集账本记录。结果给出状态、每条效果的结果、停掉的进程、实际写回、删除、删除的构建产物和没能恢复的文件；计划里有 `processes_to_stop` |
| `add_declaration_params(deep_agent)` | 不变量 23，`CheckpointRail` 调用 |
| `resume_rollback(plan_id) -> RollbackResult` / `accept_partial(plan_id) -> RollbackPlan` | 只接受 `partial` 的回退，不变量 21 |
| `check_workspace(root)` / `bind_workspace(root, *, sandboxed)` | 不变量 8；`CheckpointRail` 在拿到工作区或 sys_operation 时调用。sys_operation 是沙箱模式时不做工作区快照 |
| `begin_round` / `end_round` / `before_tool` / `after_tool` / `take_checkpoint` | 只供 `CheckpointRail` 调用 |

`CheckpointRail` 的回调：

| 事件 | 路由 | callback priority | 做什么 |
|---|---|---|---|
| `BEFORE_INVOKE` | 外层 DeepAgent | 10000 | 标记会话运行中；写回待写回的回退或重启后丢失的 Session（不变量 6、7）；会话还没有清单时在后台开始建第一份（不变量 11） |
| `BEFORE_MODEL_CALL` | 内层 ReAct | 1 | 建检查点：每轮开始，以及没调工具却继续循环的那一步 |
| `BEFORE_TOOL_CALL` | 内层 ReAct | 1 | 等第一份清单完成；登记意图（不变量 16） |
| `AFTER_TOOL_CALL` / `ON_TOOL_EXCEPTION` | 内层 ReAct | 1000000 | 补上结果和撤销参数（不变量 17） |
| `AFTER_REACT_ITERATION` | 内层 ReAct | 1 | 建检查点：每步结束 |
| `AFTER_INVOKE` | 外层 DeepAgent | 1 | 本轮没有异常时建检查点；保存最新扫描供同一工作区的下一个会话复用；标记会话空闲 |

Rail 的 `priority` 是 1，在所有内置 Rail 之后 init，检查点记下同一边界上其他 Rail 处理完的状态。

**回退说明**是一条 `UserMessage`，`metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] = "rollback_note"`，以 `[ROLLBACK]` 开头，写明：
- 回到第几个检查点、丢弃了几个检查点；
- 工作区是否恢复，写回和删除了多少文件；
- 删除了哪些构建产物，需要重新构建；
- 哪些文件没能恢复；
- 依赖目录和记忆没有回退；
- 每条外部效果的去向：已撤销、人要求保留、撤销失败或未执行、不可逆、无法撤销、结果未知、有意保留；账本以外的效果没有处理；
- 回退理由。

**内置声明表**（`effects.BUILTIN_DECLARATIONS`）：

| 类别 | 工具 |
|---|---|
| 无外部效果 | 文件工具（`read_file`、`write_file`、`edit_file`、`glob`、`list_files`、`grep`）、`todo_*`、计划与模式切换、搜索与网页读取、多模态理解、`ask_user`、记忆读取、`cron_get_job` / `cron_list_jobs` / `cron_preview_job`、`subagent_list` |
| 保留 | `write_memory`、`edit_memory`、`coding_memory_write`、`coding_memory_edit` |
| 可撤销 | `cron_create_job`：用 `cron_delete_job(job_id=result.id)` 撤销 |
| 逐条声明 | `bash`、`code`（不变量 15） |
| 不可逆（未列出的工具都是） | 其余 cron 操作、子 Agent 工具、MCP 工具 |

同一轮开始前叠加的多次回退，说明按顺序合在一条消息里。

## 数据结构

| 记录 | 键（`BaseKVStore`） | 内容 |
|---|---|---|
| `Checkpoint` | `recoverable:{session_id}:checkpoint:{checkpoint_id}` | `seq`、`parent_id`、vcs `commit_id` 与 `event_id`、`manifest_id`、`ledger_seq`（当时账本的最大序号）、`kind`（`step` / `rollback`） |
| `LedgerRecord` | `recoverable:{session_id}:ledger:{seq:012d}` | 工具名、参数摘要（最多 500 字符）、类别及原因（`detail`）、状态（`intended` / `succeeded` / `failed`）、错误、撤销工具与参数、`external_paths`、后台进程的 `pid` 与启动时间、`reverses` / `rollback_id`（撤销调用）、`abandoned` |
| 账本序号 | `recoverable:{session_id}:ledger_seq` | 已分配的最大序号；同一会话内用锁串行分配 |
| HEAD | `recoverable:{session_id}:head` | 当前检查点 ID |
| 清单 `ManifestRecord` | `recoverable:{session_id}:manifest:{manifest_id}` | 相对 `parent_id` 的变化（路径 → 条目，删除记 `null`）；`parent_id` 为空时是完整清单，每 50 份增量存一份完整的 |
| 工作区最新扫描 | `recoverable:workspace:{工作区键}:scan` | 完整清单，供同一工作区的新会话复用指纹 |
| `RollbackPlan` | `recoverable:plan:{plan_id}` | 目标、当时的 HEAD、要丢弃的检查点（最新的在前）、发起方、来源、理由、文件变化、会消失的提交数、效果及其结果、状态（`planned` / `completed` / `partial` / `accepted`）、`applied_at`。也是回退的进度记录 |
| `PendingRollback` | `recoverable:{session_id}:pending` | 目标检查点（为空时只有说明待写入）、恢复后的清单、待写入的回退说明 |
| vcs 日志 | `{session_id}:vcs:...` | 由 vcs `KvBackend` 维护 |

清单条目是 `[kind, digest, size, mtime_ns, ino, mode, link]`，`kind` 为 `f`（文件）、`l`（链接）、`d`（目录）、`s`（超过上限未存）。
工作区键是工作区绝对路径 sha256 的前 16 位。持久化的记录都带 `schema_version`，读到未知版本报 `DEEPAGENT_RUNTIME_ERROR`。

## 与其它 spec 的关系

- S_04：`CheckpointRail` 遵守事件路由集合与 priority 规则，不新增事件。
- S_02：依赖 DeepAgent 的 `save_state` / `clear_state` / `react_agent.context_engine` / `ensure_initialized`；运行状态缓存在 Session 对象上，写回后必须清掉。
- S_09：工作区根目录取自 `Workspace.root_path`。
- S_08：反向命令的复检用 `PermissionInterruptRail.engine` 的 `evaluate_global_policy_directly`，以及 bash 工具层的注入检查。
- S_05：`bash`、`code` 工具的参数定义不变，声明参数只加在开启 ④ 的 Agent 的工具卡片上。
