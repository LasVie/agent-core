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
- openjiuwen/harness/recoverable/cleanup.py
- openjiuwen/harness/recoverable/dependencies.py
- openjiuwen/harness/recoverable/effects.py
- openjiuwen/harness/recoverable/external.py
- openjiuwen/harness/recoverable/harness.py
- openjiuwen/harness/recoverable/lock.py
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
| 关联模块 | `openjiuwen/harness/recoverable/`（`harness.py` / `rail.py` / `store.py` / `models.py` / `manifest.py` / `objects.py` / `workspace.py` / `effects.py` / `processes.py` / `dependencies.py` / `external.py` / `lock.py` / `cleanup.py`） |
| 最近一次修订日期 | 2026-11-16 |
| 关联设计 | D-20261113-1506-recoverable-harness |
| 关联 feature | F_20261113_1706_recoverable-harness.md |

## 范围 / 边界

本规约定义 DeepAgent 的步级检查点和回退：`RecoverableHarness` 在每个步骤边界记下一个检查点，
包括上下文、Session 状态和工作区文件；工具调用对工作区以外的影响记在副作用账本里。
人确认回退计划后，能撤销的外部效果按声明撤销，再把工作区、上下文和状态恢复到指定检查点。

依赖目录（`node_modules`、`.venv` 等）不进快照，改为每个检查点记下包列表，回退时按列表安装和卸载。

不回退的，回退说明会如实告诉 Agent：
- Agent 的记忆目录；无法按列表恢复的依赖环境；
- 不可逆、无法撤销、结果未知的外部效果，账本里没有的外部效果；
- Agent 没有声明的工作区外文件，没能备份的工作区外文件，以及 Agent 之后又被改过的工作区外文件；
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
19. **执行顺序：停后台进程 → 撤销 → 恢复依赖环境 → 恢复工作区文件 → 放弃记录与检查点。** 撤销从最新的记录往前执行，跳过人在确认时划掉的。
    第一次撤销失败后，其余撤销不再执行（记为待执行），工作区照常恢复；工作区写回失败的文件重试一次。
    有撤销失败、待执行或未恢复的文件时，回退状态为 `partial`，否则为 `completed`；人划掉的撤销不算失败。
20. **撤销调用自己也进账本**，带被撤销记录的 ID 和回退 ID。同一回退里已成功的撤销不再执行；
    上次执行被中断、结果未知的撤销不自动重试，改为报告。撤销直接调用 Agent 的工具，在 Agent 的执行环境里运行，不经过模型和 Rail。
    模型写的反向命令执行前再检查一次：含命令替换、进程替换等注入写法的，或被 Agent 的权限策略判为 `deny` 的，不执行，记为撤销失败；
    `ask` 视为已由人在确认计划时同意。反向命令在工作区根目录执行。
21. **`partial` 的回退不自动继续。** `resume_rollback` 重试失败和待执行的撤销，再恢复一次工作区，并在下一轮开始时给 Agent 一条说明；
    `accept_partial` 接受现状。被进程崩溃打断的回退（`applying`）也由 `resume_rollback` 完成，见不变量 38。
22. **后台进程在回退时先停掉。** 用 `run_in_background` 启动的 shell 命令一律进账本，记下 pid 和 `ps` 报告的启动时间。
    回退时，在撤销和恢复工作区之前，停掉目标之后启动、仍在运行且启动时间一致的进程组（先 `SIGTERM`，2 秒后 `SIGKILL`）；
    启动时间不一致的（pid 已被别的进程占用）不动，只报告。没有进程组信号的平台只报告。
23. **声明参数只在开启 ④ 时出现。** `CheckpointRail.init` 和每轮 `BEFORE_INVOKE` 把三个参数加进 `bash`、`code` 工具卡片的
    `input_params`，`side_effect` 列为必填；描述随 `DeepAgentConfig.language` 用中文或英文。工具本身忽略这些参数。
24. **每个检查点记下依赖环境的包列表。** 扫描时遇到的依赖目录里，有 `pyvenv.cfg` 的是 Python 虚拟环境，
    名为 `node_modules` 且上一级有 `package.json` 的是 Node 环境。包列表直接从磁盘读：Python 读 `site-packages` 的 `*.dist-info`，
    Node 读顶层（含 `@scope/`）每个包的 `package.json`，不运行包管理器。先比较 `site-packages` 或 `node_modules`
    与 `.package-lock.json` 的修改时间，有变化才重读。包列表没变时，新检查点沿用上一个的 `packages_id`。
25. **依赖按列表恢复，不管是谁装的。** 回退时在撤销之后、写回文件之前，把每个环境恢复成目标检查点的列表：
    多出来的卸载，少了的或版本不同的安装目标版本。Python 用环境自己的 `pip`，没有时用 `uv pip --python`；
    Node 只处理 npm 项目，用 `npm uninstall/install --no-save`，不改 `package.json`；pnpm、yarn 项目只报告。
    目标之后新建的环境整个删除；目标时有、现在已不存在的环境只报告。安装或卸载失败的环境记下错误，回退为 `partial`。
26. **全局安装只撤 Agent 做的。** `bash` 命令里认出 `npm -g`、`brew install/uninstall/upgrade/reinstall`、不在虚拟环境中的
    `pip` / `python -m pip` 时，不论声明什么都进账本，执行前后各取一次全局包列表，比较后生成反向命令
    （新增的卸载，删掉或版本变了的装回原版本；brew 的版本变化装不回，列为无法撤销）。
    这条反向命令作为单独的一项排在模型写的反向命令之后执行。
27. **工作区外的文件只按 Agent 的声明备份和恢复。** `bash`、`code` 调用在 `external_paths` 里列出的路径，
    在 `BEFORE_TOOL_CALL` 写意图记录之前备份：`~` 和相对路径（相对 `workdir`，没有时相对工作区根目录）先变成绝对路径，
    符号链接连同它指向的路径一起备份；文件内容存进工作区的内容目录，条目存进这条记录，不存在的路径记为"不存在"。
    工作区里的路径不备份，由快照负责。包含工作区或与 ④ 存储目录重叠的、超过 1 万个条目或总大小超过 `file_size_cap` 的、
    读不了的、在沙箱里的路径不备份，回退时列为不可逆。调用结束后（失败也一样，停在中断上的除外）按内容再记一次每个路径。
28. **工作区外的文件逐个路径恢复，Agent 之后又被改过的不覆盖。** 回退时每个路径是单独的一项，可以在确认时划掉，
    排在同一调用的模型反向命令和全局包反向命令之后。现在的内容与备份相同的不动；与调用结束时不同的（有人又改过）不覆盖，
    结果记为保留（`kept`），不算失败；其余恢复成备份，原来不存在的删除。写不回去的（例如需要 sudo）记为撤销失败，回退为 `partial`；
    `resume_rollback` 重试时不再比对调用结束时的内容，因为上次的尝试可能已经改了一部分。
    调用结束时的内容没能记下的（停在中断上、超过上限），列为待人确认（`review`）。
29. **回退经信号发起，同一会话同一时间只有一个未结的信号。** `request_rollback` 按 `signal_id` 去重，重复投递返回同一张回执。
    目标不在当前历史上的拒绝。已有未结信号（等待步骤边界或等待确认）时，新信号被拒绝；例外是人发起的信号取代自动信号，
    被取代的信号和它的计划作废。人拒绝计划（`decline_rollback`）时，计划和信号作废。作废的计划不能再执行。
    任何一次回退执行后，同一会话其他未结的信号都作废（它们的计划是按旧历史算的）。
30. **运行中收到的信号等到下一个步骤边界。** `BEFORE_MODEL_CALL` 和 `AFTER_REACT_ITERATION` 在建完检查点之后，
    发现有等待中的信号就用 `request_force_finish` 结束本轮，本轮结果的 `result_type` 是 `"rollback"`。
    本轮结束时（`AFTER_INVOKE`，包括停在确认中断上、抛出异常）生成计划，信号变为等待确认。会话空闲时收到的信号立刻生成计划。
    进程在等待期间退出的，下次查询回执时生成计划。
31. **自动信号有次数上限。** 同一目标默认 2 次，同一轮默认 5 次，超过的直接拒绝，不交人。只计被接受的自动信号，人发起的不计。
    计数单独保存，不随历史删除。每轮开始时清零"同一轮"的计数；以写回回退开始的一轮不清零，因为它接着做被回退打断的任务。
32. **检查点和回退都发出事件。** `on_event` 收到：检查点已建立（含 `checkpoint_id`、`seq`、轮次；回退检查点也算）、建立失败；
    回退已接收、已拒绝、等待确认、已作废、已执行、部分完成（`resume_rollback` 的结果同样发出）；存储超过上限、按用户要求删除了旧检查点。
    事件只是通知，事实以检查点库和回退计划为准；监听方出错只记日志。
33. **同一工作区同一时间只有一个会话开启回退。** 会话第一轮开始时（`BEFORE_INVOKE`）占住工作区，执行或继续回退时也要占住；
    占住的会话调用 `release_session`，或它所在的进程退出，才释放。被别的会话占住时，本轮不开始、回退不执行，报
    `DEEPAGENT_RUNTIME_ERROR`。同一进程内按会话 ID 判断；跨进程用存储目录下锁文件上的 `flock`，进程崩溃时由系统释放。
    沙箱中的工作区不占。没有 `fcntl` 的平台只做进程内的判断。
34. **本轮开始时的准备失败就不开始本轮。** `BEFORE_INVOKE` 里占住工作区、写回回退、重启后恢复 Session 任何一步失败，
    `CheckpointRail` 抛出 `AbortError`，调用方收到原来的错误，模型不会被调用。普通回调异常只会被记日志，
    本轮会在没恢复的上下文上照常运行。待写回的回退保留，下一轮再写。
35. **回退之后清理用不到的数据。** 每次回退执行后，删除工作区各会话都用不到的清单记录、包列表、回退计划和文件内容。
    还用得到的文件内容：剩下的检查点、待写回的回退和最新扫描的清单（由增量链重建后）里的文件；
    以及账本里还能回退的调用的工作区外备份（在最老的检查点之后、没被放弃的，或部分完成的回退还会重试的）。
    被增量替换掉的旧内容不再需要，即使记着它的旧清单记录因为是增量的基础而保留。目标已不在历史上的计划、作废的计划被删除，
    等待确认的计划先作废它的信号。
36. **存储超过上限时只提示，不自动删除。** 每轮结束时查看：存下的文件内容超过 `storage_cap`（默认 100 GB），
    或磁盘剩余空间少于 `min_free_ratio`（默认 20%），就发出一次 `storage_low` 事件并记日志，什么都不删。
    同一次超限只提示一次，回到上限以内后再超才再提示。文件内容的大小在每个进程第一次查看时和每次清理后统计一次，
    其间累加新存的文件；克隆的文件按完整大小计。用户的三种选择：腾出磁盘空间；调大上限（`set_storage_limits`）；
    清除历史检查点（`prune_checkpoints`）。
    `prune_checkpoints(keep_latest)` 删除工作区每个会话除最近 `keep_latest` 个以外的检查点（至少保留 1 个），随即清理，
    并为每个受影响的会话发出 `checkpoints_dropped` 事件；带 `dry_run=True` 时不删，只报告各会话要删几个、能腾出多少字节，
    供用户决定。有一轮在运行、或别的进程占着工作区时拒绝。被删的检查点不能再作为回退目标。
37. **删除会话。** `delete_session` 删除会话的检查点、清单、包列表、账本、信号、计数、回退计划和 vcs 日志。
    没有别的会话占住工作区时立即清理只有它用到的文件内容，否则等占住的会话下次清理。会话正在运行时拒绝。
38. **回退有进度记录，崩溃后从中断处接着做。** `apply_rollback` 在动手前把计划状态改为 `applying`，记下要丢弃的检查点和确认后的效果，
    并在会话上留一个"回退执行中"的标记；之后每完成一个阶段（停进程、撤销、恢复依赖、恢复文件）记一次进度和这个阶段的结果。
    提交阶段（放弃记录、写回退说明、移动 HEAD、删除检查点、计划定稿、清除标记）的每一步都可以重做：回退说明按计划 ID 去重。
    进程在中途退出时，计划停在 `applying`；有标记时会话不能开始新的一轮、也不接受新的回退信号，直到 `resume_rollback`
    从最后记下的阶段之后接着做。已成功的撤销按账本不再执行；执行到一半、结果未知的撤销不重试，记为失败，回退为 `partial`。
    回退说明里文件和进程的数字取自各阶段当时的结果，不是重做时的。标记还在、计划却已定稿时（崩溃发生在提交的最后一步），
    下一轮开始时清除标记。

## 接口契约

| 入口 | 说明 |
|---|---|
| `RecoverableHarness(*, kv_store=None, storage_dir=None, dependency_dirs=..., build_output_dirs=..., file_size_cap=1 GB, use_clones=True, declarations=None, on_event=None, auto_limit_per_target=2, auto_limit_per_round=5, storage_cap=100 GB, min_free_ratio=0.2)` | `kv_store` 默认是 `storage_dir/recoverable.db` 上的 SQLite（`DbBasedKVStore`）；`storage_dir` 默认 `~/.openjiuwen/recoverable`，文件内容在 `storage_dir/objects/<工作区键>/`。依赖目录默认 `node_modules`、`.venv`、`venv`、`.tox`；构建产物默认 `dist`、`build`、`target`、`.next`。`declarations` 是宿主工具的 `EffectDeclaration`，按工具名；`outward_commands` 默认 `ssh`、`scp`、`sftp`、`kubectl`、`helm`、`docker`、`podman`、`aws`、`gcloud`、`az`、`terraform`、`psql`、`mysql`、`mongosh`、`redis-cli`、`crontab`、`systemctl`、`launchctl` |
| `rail() -> CheckpointRail` | 注册到 DeepAgent 的 Rail；一个 harness 服务一个 DeepAgent |
| `history(session_id, limit=None) -> list[Checkpoint]` | 从 HEAD 沿父检查点往回，最新的在前 |
| `uncovered_files(session_id) -> list[str]` | 最新清单中超过单文件上限、没有存下的文件 |
| `plan_rollback(session_id, target_id, *, initiator, source, reason) -> RollbackPlan` | 目标必须在当前历史上，否则报 `DEEPAGENT_INPUT_PARAM_ERROR`；目标可以是 HEAD 本身。计划列出要写回、删除、无法恢复的文件，要删除的构建产物，会消失的 git 提交数，以及每条外部效果的处理方式（`effects`，最新的在前） |
| `apply_rollback(RollbackApproval) -> RollbackResult` | 不变量 4、5、6、10、19、22、25、28；`RollbackApproval.cancelled_effect_ids` 是人划掉的撤销，`confirmed_effect_ids` 是人确认执行的 `review` 项。执行时重新收集账本记录。结果给出状态、每条效果的结果、停掉的进程、实际写回、删除、删除的构建产物和没能恢复的文件；计划里有 `processes_to_stop` 和 `package_changes`（每个环境要卸载和安装的包），结果里有每个环境的恢复情况 |
| `request_rollback(RollbackSignal) -> RollbackTicket` | 不变量 29～31。`RollbackSignal` 含 `signal_id`、`session_id`、`target_id`、`initiator`（`human` / `auto`）、`source`、`reason`；回执的 `status` 为 `waiting`、`awaiting_approval`、`rejected`、`invalidated`、`applied`，等待确认时带 `plan_id`，被拒绝或作废时 `detail` 写明原因 |
| `get_ticket(session_id, signal_id) -> RollbackTicket \| None` | 查询回执；进程退出时还在等待的信号，此时生成计划 |
| `decline_rollback(plan_id, reason="") -> RollbackPlan` | 人拒绝一份等待确认的计划；计划状态变为 `invalidated`，信号作废 |
| `delete_session(session_id)` | 不变量 37 |
| `storage_status() -> StorageStatus` | 存下的文件内容、上限、磁盘剩余和总量，以及超过了哪条上限 |
| `set_storage_limits(storage_cap, min_free_ratio)` | 调整两条上限，例如用户同意给回退更多空间 |
| `prune_checkpoints(keep_latest, *, dry_run=False) -> PruneReport` | 不变量 36；报告每个会话删除（或将删除）的检查点数和腾出的字节数 |
| `release_session(session_id) -> bool` | 不变量 33；宿主在同一进程里关闭一个会话、开始另一个时调用。被释放会话的检查点保留，以后回退它时会重新占住工作区 |
| `add_declaration_params(deep_agent)` | 不变量 23，`CheckpointRail` 调用 |
| `resume_rollback(plan_id) -> RollbackResult` / `accept_partial(plan_id) -> RollbackPlan` | `resume_rollback` 接受 `partial` 和 `applying` 的回退（不变量 21、38）；`accept_partial` 只接受 `partial` |
| `check_workspace(root)` / `bind_workspace(root, *, sandboxed)` | 不变量 8；`CheckpointRail` 在拿到工作区或 sys_operation 时调用。sys_operation 是沙箱模式时不做工作区快照 |
| `begin_round` / `end_round` / `before_tool` / `after_tool` / `take_checkpoint` | 只供 `CheckpointRail` 调用 |

`CheckpointRail` 的回调：

| 事件 | 路由 | callback priority | 做什么 |
|---|---|---|---|
| `BEFORE_INVOKE` | 外层 DeepAgent | 10000 | 占住工作区（不变量 33）；标记会话运行中；写回待写回的回退或重启后丢失的 Session（不变量 6、7）；会话还没有清单时在后台开始建第一份（不变量 11） |
| `BEFORE_MODEL_CALL` | 内层 ReAct | 1 | 建检查点：每轮开始，以及没调工具却继续循环的那一步；有等待中的信号时结束本轮（不变量 30） |
| `BEFORE_TOOL_CALL` | 内层 ReAct | 1 | 等第一份清单完成；登记意图（不变量 16） |
| `AFTER_TOOL_CALL` / `ON_TOOL_EXCEPTION` | 内层 ReAct | 1000000 | 补上结果和撤销参数（不变量 17） |
| `AFTER_REACT_ITERATION` | 内层 ReAct | 1 | 建检查点：每步结束；有等待中的信号时结束本轮 |
| `AFTER_INVOKE` | 外层 DeepAgent | 1 | 本轮没有异常时建检查点；保存最新扫描供同一工作区的下一个会话复用；标记会话空闲；为等待中的信号生成计划 |

Rail 的 `priority` 是 1，在所有内置 Rail 之后 init，检查点记下同一边界上其他 Rail 处理完的状态。

**回退说明**是一条 `UserMessage`，`metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] = "rollback_note"`，以 `[ROLLBACK]` 开头，写明：
- 回到第几个检查点、丢弃了几个检查点；
- 工作区是否恢复，写回和删除了多少文件；
- 删除了哪些构建产物，需要重新构建；
- 哪些文件没能恢复；
- 依赖目录和记忆没有回退；
- 每条外部效果的去向：已撤销、人要求保留、因 Agent 之后又被改过而保留的工作区外文件、撤销失败或未执行、不可逆、无法撤销、结果未知、有意保留；
  账本以外的效果和没有声明的工作区外文件没有处理；
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
| `Checkpoint` | `recoverable:{session_id}:checkpoint:{checkpoint_id}` | `seq`、`parent_id`、vcs `commit_id` 与 `event_id`、`manifest_id`、`packages_id`、`ledger_seq`（当时账本的最大序号）、`kind`（`step` / `rollback`） |
| `LedgerRecord` | `recoverable:{session_id}:ledger:{seq:012d}` | 工具名、参数摘要（最多 500 字符）、类别及原因（`detail`）、状态（`intended` / `succeeded` / `failed`）、错误、撤销工具与参数、`external_paths` 及其备份 `external_backups`（每个路径执行前和执行后的条目、没能备份的原因）、后台进程的 `pid` 与启动时间、全局安装的反向命令（`generated_undo`）与无法撤销项、`reverses` / `rollback_id`（撤销调用）、`abandoned` |
| 账本序号 | `recoverable:{session_id}:ledger_seq` | 已分配的最大序号；同一会话内用锁串行分配 |
| HEAD | `recoverable:{session_id}:head` | 当前检查点 ID |
| 清单 `ManifestRecord` | `recoverable:{session_id}:manifest:{manifest_id}` | 相对 `parent_id` 的变化（路径 → 条目，删除记 `null`）；`parent_id` 为空时是完整清单，每 50 份增量存一份完整的 |
| 包列表 | `recoverable:{session_id}:packages:{packages_id}` | 环境的工作区相对路径 → 包名 → 版本 |
| 工作区的会话 | `recoverable:workspace:{工作区键}:sessions` | 在这个工作区跑过的会话 ID，清理时据此找出还要用的文件内容 |
| 工作区最新扫描 | `recoverable:workspace:{工作区键}:scan` | 完整清单，供同一工作区的新会话复用指纹 |
| `RollbackPlan` | `recoverable:plan:{plan_id}` | 目标、当时的 HEAD、要丢弃的检查点（最新的在前）、发起方、来源、理由、对应的信号、文件变化、会消失的提交数、效果及其结果、状态（`planned` / `applying` / `completed` / `partial` / `accepted` / `invalidated`）、`applied_at`、进度 `progress`（最后完成的阶段、要丢弃的检查点、停掉的进程、恢复依赖后的包列表、写回和删除的文件、恢复后的清单）。也是回退的进度记录 |
| 回退执行中 | `recoverable:{session_id}:applying` | 正在执行的回退计划 ID；提交完成后删除 |
| `RollbackTicket` | `recoverable:{session_id}:signal:{signal_id}` | 信号、状态、计划 ID、原因 |
| 未结信号 | `recoverable:{session_id}:active_signal` | 等待步骤边界或等待确认的信号 ID，最多一个 |
| `RollbackCounts` | `recoverable:{session_id}:counts` | 轮次、本轮被接受的自动信号数、每个目标被接受的自动信号数 |
| `PendingRollback` | `recoverable:{session_id}:pending` | 目标检查点（为空时只有说明待写入）、恢复后的清单、待写入的回退说明，以及这些说明来自哪些回退计划 |
| vcs 日志 | `{session_id}:vcs:...` | 由 vcs `KvBackend` 维护 |
| 工作区锁 | 文件 `storage_dir/locks/{工作区键}.lock`，不在 `BaseKVStore` 里 | 占住时写入 `session {session_id}, pid {pid}`，被拒绝的一方在报错里看到 |

清单条目是 `[kind, digest, size, mtime_ns, ino, mode, link]`，`kind` 为 `f`（文件）、`l`（链接）、`d`（目录）、`s`（超过上限未存）。
工作区键是工作区绝对路径 sha256 的前 16 位。持久化的记录都带 `schema_version`，读到未知版本报 `DEEPAGENT_RUNTIME_ERROR`。

## 与其它 spec 的关系

- S_04：`CheckpointRail` 遵守事件路由集合与 priority 规则，不新增事件。
- S_02：依赖 DeepAgent 的 `save_state` / `clear_state` / `react_agent.context_engine` / `ensure_initialized`；运行状态缓存在 Session 对象上，写回后必须清掉。
- S_09：工作区根目录取自 `Workspace.root_path`。
- S_08：反向命令的复检用 `PermissionInterruptRail.engine` 的 `evaluate_global_policy_directly`，以及 bash 工具层的注入检查。
- S_05：`bash`、`code` 工具的参数定义不变，声明参数只加在开启 ④ 的 Agent 的工具卡片上。
