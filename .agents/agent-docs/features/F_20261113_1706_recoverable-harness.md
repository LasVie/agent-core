---
id: F_20261113_1706_recoverable-harness
title: Recoverable Harness 实施与决策归档
type: F
author: Song Haoran
author_email: song.haoran@h-partners.com
modules:
- harness
- core/foundation/tool
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
- tests/unit_tests/harness/recoverable/conftest.py
- tests/unit_tests/harness/recoverable/test_cleanup.py
- tests/unit_tests/harness/recoverable/test_crash.py
- tests/unit_tests/harness/recoverable/test_dependencies.py
- tests/unit_tests/harness/recoverable/test_external.py
- tests/unit_tests/harness/recoverable/test_harness.py
- tests/unit_tests/harness/recoverable/test_ledger.py
- tests/unit_tests/harness/recoverable/test_lock.py
- tests/unit_tests/harness/recoverable/test_shell.py
- tests/unit_tests/harness/recoverable/test_signals.py
- tests/unit_tests/harness/recoverable/test_workspace.py
- openjiuwen/harness/rails/security/tool_security_rail.py
- openjiuwen/core/foundation/tool/mcp/base.py
- openjiuwen/core/foundation/tool/mcp/client/stdio_client.py
- openjiuwen/core/foundation/tool/mcp/client/sse_client.py
- openjiuwen/core/foundation/tool/mcp/client/playwright_client.py
- openjiuwen/core/foundation/tool/mcp/client/streamable_http_client.py
- tests/unit_tests/harness/recoverable/test_mcp.py
- tests/unit_tests/core/foundation/tool/mcp/test_tool_annotations.py
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
  - **U3 副作用账本**：工具的副作用声明表；调用前登记意图、登记不了就不执行；调用后补结果和撤销参数；
    回退时先撤销（最新的在前）、再恢复工作区；人可以在确认时划掉撤销；失败即停、状态为部分完成；
    `resume_rollback` / `accept_partial`；Agent 的记忆目录不回退。
  - **U4 shell 与执行代码的逐条声明**：`bash`、`code` 的三个声明参数（只在开启 ④ 时出现）；对外命令名单；
    退出码不为 0 的命令，反向命令要人确认才执行；反向命令执行前复检；后台进程在回退时先停掉。
  - **U5 依赖与全局安装**：每个检查点记下工作区里 Python 虚拟环境和 npm 项目的包列表；回退时按列表安装和卸载，
    目标之后新建的环境整个删除；Agent 的全局安装（`npm -g`、`brew`、虚拟环境以外的 `pip`）按前后对比生成反向命令。
  - **U6 工作区外的文件**：模型在 `external_paths` 里声明的路径，执行前备份、执行后记下内容；回退时逐个路径恢复，
    原来不存在的删除，Agent 之后又被改过的不覆盖、只报告；太大或读不了的不备份，列为不可逆。
  - **U7a 回退信号与事件**：`request_rollback` 接收人或自动发起方的信号；运行中的信号在下一个步骤边界结束本轮，
    本轮结束时生成计划；同一会话只有一个未结信号，人发起的取代自动的；人可以拒绝计划；自动信号的次数上限；检查点和回退事件。
  - **U7b 一个工作区一个会话**：会话第一轮开始时占住工作区，到 `release_session` 或进程退出为止；
    别的会话（同一进程或其他进程）开始一轮、执行回退时被拒绝。顺带修正：本轮开始时写回回退失败会中止本轮。
  - **U7c 存储清理与上限**：回退后清理用不到的清单、包列表、计划和文件内容；存储超过 100 GB 或磁盘剩余不足 20% 时
    提示用户、不自动删除，用户可以先预览再清除历史检查点（每个会话保留最近 N 个）；`delete_session` 删除一个会话的全部记录。

  - **U7d 回退进度记录**：回退按阶段记进度；中途进程崩溃时，会话不能开始新一轮，重启后 `resume_rollback` 从中断处接着做，
    已成功的撤销不重复执行，回退说明只写一次。

  - **U8 MCP 提示**：core 的 MCP 工具卡片保留服务器给的提示（`annotations`）；声明表里没有的 MCP 工具，服务器标了只读的
    按无外部效果，其余按不可逆，宿主的声明优先。

  开发单元 U1～U8 都已完成。

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

U3 新增的做法：

- **新增 `effects.py`：** 类别（无外部效果 / 可撤销 / 不可逆 / 保留）、`EffectDeclaration`、内置声明表、撤销参数的取法。
  撤销参数用 `result.<键>` 或 `args.<键>` 指明从调用结果还是参数里取，取不到就记为无法撤销，不猜。
- **内置声明表只收能核实的工具。** 名字取自各工具的 `build_tool_card` 调用。`cron_create_job` 的返回结构取决于 cron 后端，
  所以按"结果里有 `id` 才能撤销"声明；后端不返回 `id` 时，计划里显示无法撤销。
- **登记的位置：** 在 `BEFORE_TOOL_CALL` 的最后一个（priority 1）登记意图。这时权限和中断 Rail 已经决定这次调用会不会执行；
  已被跳过的调用不登记。登记失败时，按 ability_manager 的跳过约定（`_skip_tool_calls` 加上预设的结果）跳过这次调用，
  模型收到错误说明。没有像 PA 那样结束整轮：一次调用登记失败，不必让整轮停下。
- **补结果的位置：** `AFTER_TOOL_CALL` / `ON_TOOL_EXCEPTION` 用 callback priority 1000000 排在最前，读到其他 Rail 改写之前的原始结果，做法同 PA。
  每个工具调用有独立的回调上下文，意图记录放在它的 `extra` 里，并行调用之间不会串。
- **撤销怎样执行：** 直接取 Agent 注册的工具实例，在 Agent 的执行环境里调用，不经过模型和 Rail（A14）。
  人已经确认过列着这些撤销的计划，所以不再走权限确认；也因此撤销调用不会被账本 Rail 重复登记。
  撤销调用另记一条账本记录，带被撤销记录的 ID 和回退 ID，用来防止重复执行。
- **账本序号：** 每个会话一个递增序号，用会话级的锁串行分配，与建检查点用的锁分开，避免工具调用和建检查点互相等待。
  检查点记下当时的最大序号；回退时处理序号大于目标的记录。
- **计划和进度合在一份记录里。** `RollbackPlan` 带上每条效果的结果和回退状态，也就是设计里的"回退进度记录"。
- **继续部分完成的回退时补一条说明：** 下一轮开始前给 Agent 一条"部分回退已重试"的说明。
  做法是让待写回记录允许没有目标、只带说明。
- **记忆目录：** Rail 从工作区节点 `memory`、`coding_memory` 取路径，传给快照作为不进快照的目录。

U4 新增的做法：

- **声明参数加在工具卡片上，不改工具。** `CheckpointRail.init`（以及每轮 `BEFORE_INVOKE`，以防工具被重载）把三个参数加进
  这个 Agent 的 `bash`、`code` 工具卡片的 `input_params`；卡片 ID 带 Agent ID，不会影响别的 Agent。
  `bash` 和 `code` 用 `inputs.get` 取参数，多出的参数被忽略，所以既不改工具实现，也不从调用参数里删掉它们
  （删掉会改到上下文里那条模型回复的工具调用）。参数说明就是给模型的提示，按 `DeepAgentConfig.language` 选中文或英文。
- **对外命令的判断：** 按 shell 分隔符切出每条简单命令，跳过环境变量赋值和 `sudo`、`env` 等前缀，看程序名；
  `git push` 和带写方法或请求体的 `curl` / `wget` 单独判断。只用来把"无外部效果"的声明改为不可逆，不拦命令。
- **失败命令的反向命令要人确认（A32）：** `bash` 的结果 `success` 为 `False`（退出码按语义判为失败）时，记录状态为失败，
  但仍从参数里取出反向命令。计划里这类记录是 `review`，人在确认时把它列进 `confirmed_effect_ids` 才执行。
- **反向命令的复检（A32）：** 用 bash 工具层的 `check_injection`（命令替换、进程替换等），再用 Agent 上 `PermissionInterruptRail` 的
  权限引擎判断；判为 `deny` 不执行，`ask` 视为人已在确认计划时同意。为此给 `PermissionInterruptRail` 加了只读属性 `engine`（S_08 已同步）。
- **反向命令在工作区根目录执行：** 两轮之间没有绑定当前目录，所以撤销时给 `bash` 传 `workdir`。
- **后台进程：** 后台启动的 `bash` 调用不论声明什么都进账本，补结果时从返回的 `pid` 记下 `ps -o lstart=` 报告的启动时间。
  回退最先停这些进程：后台命令有自己的会话（`start_new_session`），pid 就是进程组号，所以 `killpg` 能停掉它派生的子进程。
  启动时间对不上（例如重启后 pid 被复用）就不动。没有用 psutil：它不是基础依赖（只在 `cua` 额外依赖里）。
- **账本里记下 `external_paths`**，供 U6 在执行前备份。

U5 新增的做法：

- **新增 `dependencies.py`。** 包列表直接从磁盘读，不运行包管理器：Python 用 `importlib.metadata.distributions(path=...)`
  读 `site-packages`；Node 读 `node_modules` 顶层和 `@scope/` 下每个包的 `package.json`。这样快、不需要网络，
  也不要求虚拟环境里有 pip（uv 建的 `.venv` 就没有）。
- **先看修改时间，再决定要不要重读。** Python 看 `site-packages` 目录，Node 看 `node_modules` 目录和 `.package-lock.json`：
  安装、卸载、升级都会增删其中的条目，所以目录的修改时间会变。
- **依赖目录在扫描时顺便记下。** 工作区扫描遇到名单里的目录本来就跳过，现在把路径记进 `Manifest.dependency_dirs`，
  不需要另外遍历工作区。
- **恢复依赖用的命令：** Python 优先用环境自己的 `python -m pip`，没有 pip 时用 `uv pip --python <环境>`；
  Node 用 `npm uninstall/install --no-save`，不改 `package.json`（它是普通文件，随后由快照恢复）。
  pnpm、yarn 的 `node_modules` 结构和命令都不同，只报告不处理。
- **目标之后新建的环境整个删除**，符合"回到检查点时的状态"；目标时有、现在已不存在的环境无法重建（不知道用哪个 Python），只报告。
- **全局安装：** 只认几种明确的写法，执行前后各取一次全局列表（`npm ls -g --json`、`brew list --versions`、`pip list --format=json`），
  比较差异生成反向命令，所以只撤这次调用造成的变化，用户自己装的不动。即使命令失败到一半，差异也是准确的。
  反向命令作为单独一项排在模型写的反向命令之后（设计 A30）。
- **检查点的去重**加上了 `packages_id`。

U6 新增的做法：

- **新增 `external.py`。** 备份的条目沿用工作区清单的格式，文件内容存进同一个内容目录；恢复直接复用 `WorkspaceSnapshot.restore`，
  以路径的上一级目录为根，只传入这个路径下的条目，所以不会碰到旁边的文件。
- **每个路径是单独的一项**（`PlannedEffect.path`），所以确认时可以只划掉其中一个，结果也逐个报告。
  同一调用里排在模型的反向命令和全局包反向命令之后：反向命令执行时可能还要读这些文件。
- **"之后又被改过"按内容判断，不按修改时间。** 调用结束时给每个路径记一份内容指纹（只算 sha256，不存内容）。
  两次调用改同一个文件时，先恢复较新的一次，文件内容就回到较早那次调用结束时的样子，较早那次的比对仍然通过；
  按修改时间判断的话，这里会被误判成"被人改过"。
- **"目录太大"的界限：** 超过 1 万个条目，或总大小超过 `file_size_cap`（默认 1 GB）。设计只说"目录太大不备份"，
  1 GB 沿用单文件上限；条目数限制让误声明 `~` 这类路径时很快停下，不会遍历整个主目录。
- **符号链接：** 声明的路径是链接时，链接本身和它指向的路径都备份，因为 `echo >> ~/.zshrc` 写的是链接指向的文件。
- **不备份、列为不可逆的路径：** 包含工作区的（与工作区恢复冲突）；与 ④ 存储目录重叠的；沙箱里的（它们不在本机上）。
- **外部文件用单独的 `ObjectStore` 实例**，指向同一个内容目录：放在另一个卷上的文件不能克隆，
  而 `ObjectStore` 一次克隆失败后就不再尝试，不能因此让工作区快照也改成复制。
- **提示词：** 声明参数的说明改为"`external_paths` 里列出的文件回退时会恢复，只改它们填 none"，
  否则模型会把改 `~/.zshrc` 声明成不可逆。

U7a 新增的做法：

- **信号和回执。** `RollbackSignal` 由发起方给出 `signal_id`；`RollbackTicket` 记下信号走到哪一步。
  同一会话最多一个未结信号，存在 `active_signal` 里。
- **运行中的信号怎样结束本轮。** `request_rollback` 发现会话正在运行，只把信号记为等待，并在进程内标记这个会话。
  检查点 Rail 在 `BEFORE_MODEL_CALL` 和 `AFTER_REACT_ITERATION` 建完检查点后看这个标记，有就调用 `request_force_finish`。
  ReAct 循环在下一次迭代开始前（或跳过这次模型调用）结束本轮，所以正在执行的模型调用和工具不会被打断。
- **计划在本轮结束时生成，** 不在步骤边界生成：本轮结束时还会建最后一个检查点，计划按那时的状态算。
- **"同一轮"怎样计数。** 一个自动回退在运行中结束了本轮，确认执行后宿主要再发起一轮才能继续，按调用次数算的话，
  "同一轮 5 次"永远不会超过 1。所以以写回回退开始的一轮不清零计数，它被视为被打断的任务的继续。
- **事件经回调发出：** 构造参数 `on_event`，同步或异步函数都可以；出错只记日志。
- **人拒绝计划：** 新增 `decline_rollback`，计划状态新增 `invalidated`。直接用 `plan_rollback` 做的计划（不经信号）仍可执行，
  用于预览和测试；执行后同一会话其他未结的信号作废。

U7b 新增的做法：

- **新增 `lock.py`。** 进程内记下占住工作区的会话 ID；跨进程在 `storage_dir/locks/<工作区键>.lock` 上加 `flock`，
  文件里写着占住它的会话和 pid，被拒绝的一方在报错里能看到。`flock` 跟着打开的文件走，同一进程里另一个 harness
  再打开同一个锁文件也会被拒绝；进程退出（包括崩溃）时系统自动释放，不会留下死锁文件。
- **什么时候占住：** 会话第一轮的 `BEFORE_INVOKE`，以及 `apply_rollback`、`resume_rollback`。只看计划（`plan_rollback`）不占。
  沙箱中的工作区不在本机上，不占。
- **什么时候释放：** 宿主调用 `release_session`，或进程退出。宿主在同一进程里换会话（例如 CLI 开新对话）时要调用它，
  否则新会话的第一轮会被拒绝。
- **拒绝怎样传给宿主：** 回调框架只记录普通回调异常的日志，不往外抛，本轮照样运行。所以 `CheckpointRail` 把本轮开始时的
  失败包成 `AbortError`，调用方收到原来的错误（`DEEPAGENT_RUNTIME_ERROR`），模型不会被调用。

U7c 新增的做法：

- **新增 `cleanup.py`，** 只放判断"哪些还用得到"的纯函数。文件内容按工作区共用，所以先登记每个工作区跑过哪些会话
  （`recoverable:workspace:<键>:sessions`），清理时汇总这些会话还用得到的内容，其余的从内容目录删掉。
- **清单按增量链重建后再取内容。** 清单是增量的，老的检查点删掉后，它的清单记录往往还是后面清单的基础，不能删；
  但其中被后面的增量替换掉的文件内容已经没人要了。所以按剩下的每个检查点重建出完整清单，只保留这些清单里的内容。
  这样删检查点时不用把后面的清单改写成完整清单。
- **工作区外文件的备份：** 账本记录永不删除，但它们的备份只在还能回退时需要：在最老的检查点之后、没被放弃，
  或者某个部分完成的回退还可能重试它。最后这条是必须的：回退执行时就把记录标为放弃了，而 `resume_rollback` 还要用备份。
- **什么时候清理：** 回退执行后、`delete_session` 时、用户调用 `prune_checkpoints` 时。都发生在这个进程占住工作区、没有一轮在运行的时候：
  一轮中途存下的内容在检查点建好之前还没被引用，这时清理会误删。
- **超限只提示（需求方 2026-11-16 修订 D-11）：** 最初按设计在超限时从最老的检查点删起，每批删四分之一。
  需求方指出磁盘被别的程序占满时会把历史删光，改为只发出 `storage_low` 事件，由用户选择；清除用 `prune_checkpoints`，
  先用 `dry_run=True` 报告能腾出多少空间。预览时按"删完后剩下的检查点"重新算一遍还要用的内容，与真正清理用同一套判断。
- **上限怎样算：** 磁盘剩余空间每轮结束时用 `shutil.disk_usage` 查；文件内容的总量每个进程第一次检查时遍历内容目录统计一次，
  之后累加新存的文件，每次清理后重新统计，所以平时每轮只多一次系统调用。克隆共享磁盘块，按完整大小计，偏保守。
- **测试里磁盘看起来是空的：** 新增 `conftest.py`，让 `shutil.disk_usage` 报告磁盘全空，避免测试机磁盘较满时历史被随机删短；
  测试这条规则的用例自己再改成"只剩 10%"。

U7d 新增的做法：

- **进度记在回退计划里。** `RollbackPlan` 新增状态 `applying` 和 `progress`：最后完成的阶段，以及各阶段的结果
  （停掉的进程、恢复依赖后的包列表、写回和删除的文件、恢复后的清单）。会话上另有一个"回退执行中"的标记，开始新一轮和接收信号时查它，
  不用遍历所有计划。
- **为什么要存各阶段的结果：** 重做一个已完成的阶段不会出错（各阶段都按状态比对，撤销按账本去重），但结果不同：
  例如文件已经恢复过，重做时写回 0 个，回退说明就会写成"0 written back, 0 deleted"。所以继续时跳过已完成的阶段，说明用当时的结果。
- **提交阶段不分小步，** 改为每一步都可以重做：放弃记录、移动 HEAD、删除检查点本来就幂等；回退说明在 `PendingRollback.plan_ids`
  里记下来自哪个计划，已写过就不再写。原来在回退里删除被丢弃检查点的清单，改由随后的清理处理（U7c），提交阶段少一步。
- **崩溃发生在提交的最后一步**（计划已定稿、标记还没清除）：下一轮开始时发现计划不是 `applying`，直接清除标记。

U8 新增的做法：

- **core 的改动（增量）：** `McpToolCard` 新增可选字段 `annotations`，默认 `None`；`mcp/base.py` 新增 `mcp_tool_annotations`，
  把 MCP SDK 的 `ToolAnnotations` 转成字典，没给的提示不放进去；stdio、sse、playwright、streamable-http 四个客户端在
  `list_tools` 时填入。`tool_info()` 不变，模型看到的内容不变。按设计这部分要由 core 中 MCP 的维护者评审。
- **④ 怎样拿到提示：** Agent 把 MCP 工具注册成名为 `mcp_<服务器>_<工具>` 的普通 `ToolCard`，提示不在上面；
  但它的 id（`<服务器 ID>.<服务器名>.<工具>`）就是 `Runner.resource_mgr` 里那个 `MCPTool` 的 id，那个工具的卡片正是客户端列出的
  `McpToolCard`。所以登记时按名字取 Agent 的卡片，再按 id 取回服务器的卡片。只在声明表里没有这个工具、名字以 `mcp_` 开头时才查。
- **只用 `readOnlyHint`：** 标了只读的按无外部效果，其余按不可逆。`destructiveHint` 等提示不改变结论：没有撤销工具就撤不回，
  不论是否"破坏性"。服务器的提示是它对自己的描述，可能不实，所以只作默认值，宿主的声明优先。



## 与设计的偏差

| 设计 | 实际 | 原因与影响 |
| --- | --- | --- |
| 执行回退时就写回上下文、提交回退检查点（§5.7.2、§5.11.4） | `apply_rollback` 只删除目标之后的检查点、移动 HEAD、留下待写回的回退；下一轮 `BEFORE_INVOKE` 时才写回上下文、截断 vcs、追加回退说明并提交回退检查点 | 两轮之间没有活的 Session 可写。不影响 Agent 看到的结果：Agent 本来就要等下一轮才继续。在下一轮开始前，HEAD 是目标检查点，回退检查点还不存在 |
| `plan_rollback` 的目标要在 HEAD 之前 | 目标可以是 HEAD 本身 | 用来丢弃 HEAD 之后没建检查点的内容，例如停在中断上的那一步 |
| 运行中收到信号，在步骤边界结束本轮，"把计划作为本轮结果交给调用方"（§5.7.2） | 本轮结果只说明因回退请求停下（`result_type` 为 `"rollback"`），计划在本轮结束时生成，调用方用 `get_ticket` 取 | 计划要按本轮最后的检查点算，而本轮结果在那之前就定了 |
| 接口 `get_rollback(rollback_id)`、`accept_partial(rollback_id, approval)`（§5.3） | `get_ticket(session_id, signal_id)` 查信号；`accept_partial(plan_id)` 不带确认；另加 `decline_rollback(plan_id)` | 计划 ID 就是回退 ID；接受部分完成不需要再核对目标 |
| 同一轮的自动回退上限（P8） | 以写回回退开始的一轮继续计数 | 见上文"同一轮"怎样计数；需求方 2026-11-16 确认，设计 P8 已同步 |
| 确认时列出文件、包、反向操作等（§5.11.5） | 计划列出要丢弃的检查点、文件变化、构建产物、会消失的 git 提交数和每条外部效果的处理方式 | 包随 U5 加入；"经用户批准过的调用"标记没有做（P7） |
| 回退说明写明副作用去向（§5.11.5） | 写明文件恢复情况和每条外部效果的去向 | 依赖随 U5 加入 |
| 工作区整体回到检查点（D-04） | 记忆目录不进快照、回退时不动 | 与"记忆写入不回退"冲突，2026-11-13 由需求方定为记忆优先，设计第 4.1、10 节已修订 |
| 失败的调用（§4.1 只提到"只登记了意图、没有结果"） | 抛出异常或报告失败的调用，和没有结果的一样列为"结果未知"，不撤销 | 失败不代表一点效果都没发生，按未知处理更稳妥 |
| 退出码不为 0 的命令"列进计划由人判断"（A32） | 状态为失败、有反向命令的记录列为 `review`，确认时显式列出才执行 | 与 A32 一致，补充了确认的接口 |
| 反向命令"重新过一遍 shell 的安全检查"（A32） | 注入检查 + 权限策略的 `deny`；`ask` 不再询问 | 回退在两轮之间执行，没有中断可以询问；人已在确认计划时看过这些命令 |
| 继续部分完成的回退（§5.6） | 不阻止下一轮开始，由宿主决定先 `resume_rollback`、`accept_partial` 还是直接继续 | 每一轮本来都由宿主发起 |
| 检查点事件、回退事件（§5.3） | 经构造参数 `on_event` 回调发出 | 设计没有指定通道；回调最简单，② 可以在回调里转发到自己的存储 |
| 任务循环模式（§5.7.1 的 `AFTER_TASK_ITERATION`） | 不支持 | 任务循环模式下外层 invoke 没有 Session，写回和重启恢复都挂在外层 `BEFORE_INVOKE` 上 |
| 回退时找出目标之后动过的路径，再扫一次 HEAD 之后的改动（§5.11.2） | 重新扫描整个工作区，与目标清单比对 | 结果相同，更简单，而且不依赖中间清单是否完整。代价是每次回退多扫一遍工作区（本仓库约 170 毫秒） |
| 第一个"可能改文件"的工具等第一份清单（§5.11.2） | 所有工具调用都等 | 判断工具会不会改文件要靠 U3 的声明表。只影响每个会话的第一次工具调用 |
| 只被已删除检查点用到的文件内容在提交后清理（§5.10） | 回退执行后清理，范围是整个工作区所有会话都用不到的内容（U7c） | 内容按工作区共用，只看一个会话会误删别的会话还要的内容 |
| 超过上限时提示用户（§5.10，2026-11-16 修订） | 发出 `storage_low` 事件并记日志，由宿主提示用户；用户的选择经 `set_storage_limits`、`prune_checkpoints` 执行 | ④ 不直接面对用户 |
| 删除检查点 | 检查点、清单、包列表、文件内容都会删；vcs 里这些检查点之前的上下文日志不删 | vcs 只有截断后面（`rewind`）、没有删除前面的操作；上下文日志比文件内容小得多 |
| 重启后宿主可以直接回退 | 重启后要在第一轮之前回退，宿主需先调用 `DeepAgent.ensure_initialized()` | Rail 要在初始化时才拿到工作区；不调用的话，计划里会显示工作区不恢复 |
| 需要 sudo 才能写回的工作区外文件，"写回失败时报告"（§5.11.3） | 记为撤销失败：后面的撤销不再执行，回退为 `partial` | 与其他撤销失败的处理一致；人可以修好权限后 `resume_rollback` |
| Agent 之后又被改过的工作区外文件"不覆盖，报告"（§5.11.3） | 另加一种情况：调用结束时的内容没能记下（停在中断上、超过上限），无法判断有没有人改过，列为待人确认 | 设计没有覆盖这种情况；直接恢复可能覆盖用户的改动，直接跳过又可能漏掉 Agent 的改动 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-13 / U1：V-01、V-04 的一部分、V-06、V-17、P19 | 本地工作区，`feature/recoverable-harness`；Python 3.11.17，macOS arm64；真实 DeepAgent、Rail、ReAct、Session、vcs，模型用 `MockLLMModel` 脚本 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/test_harness.py tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：10 个用例（本特性 7 个，含 2 种存储参数；路由契约 3 个） | 任务循环模式、运行中信号、写回失败路径没有测试 |
| 2026-11-13 / U2：V-07、V-09、V-10、V-18 | 同上；工具直接写文件（模拟 shell），测试代码直接改文件（模拟用户）；APFS，git 2.x | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ -q -p no:cacheprovider` | 通过：14 个用例（U1 的 7 个 + U2 的 7 个） | ext4、NTFS 上的复制路径只用 `use_clones=False` 模拟；Linux 的 reflink 没有验证 |
| 2026-11-13 / U2 测试有效性 | 同上 | 临时把 racy 窗口改为 0、把等第一份清单改为不等，各跑对应用例 | 两个对应用例都失败，恢复后都通过 | — |
| 2026-11-16 / U3：V-03 的一部分、V-13、V-14、V-16 的一部分 | 同上；模拟的工单服务（创建可撤销、发消息不可逆） | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：25 个用例（U1 7 个、U2 7 个、U3 8 个、路由契约 3 个） | 撤销中途进程崩溃、结果未知的撤销没有测试 |
| 2026-11-16 / U3 测试有效性 | 同上 | 临时分别去掉记忆目录、把撤销顺序改为最旧的在前、把登记失败改为照常执行 | 对应用例都失败（顺序改动使 3 个用例失败），恢复后都通过 | — |
| 2026-11-16 / U4：V-12、V-15 | 同上；真实的 `bash` 工具，本地 sys_operation，命令白名单关闭 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：31 个用例（U1～U3 22 个、U4 6 个、路由契约 3 个）；使用 `PermissionInterruptRail` 的 5 个测试文件 67 个用例通过 | `code` 工具没有端到端测试；Windows 没有测试 |
| 2026-11-16 / U4 测试有效性 | 同上 | 临时分别去掉反向命令复检、让 `review` 项自动执行、去掉对外命令判断、不停后台进程 | 对应用例各失败 1 个，恢复后都通过 | — |
| 2026-11-16 / U5：V-08、V-11 的一部分 | 同上；假的包管理器：虚拟环境的 `bin/python` 和 PATH 上的 `npm` 都是 shell 脚本，按参数增删 `*.dist-info` 和 `package.json`，全局包放在一个临时目录 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：36 个用例（U1～U4 28 个、U5 5 个、路由契约 3 个） | 没有用真实的 pip、uv、npm、brew 验证（V8、V9），也没有联网安装 |
| 2026-11-16 / U5 测试有效性 | 同上 | 临时分别让依赖恢复什么都不做、让全局反向命令为空、让指纹恒定 | 对应用例都失败（依赖恢复 4 个、全局 1 个、指纹 1 个），恢复后都通过 | — |
| 2026-11-16 / U6：V-11 的一部分 | 同上；真实的 `bash` 工具；用测试目录下的 `outside/` 代替用户主目录，测试代码直接改文件模拟用户 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：43 个用例（U1～U5 33 个、U6 7 个、路由契约 3 个）；`tests/unit_tests/harness/prompts/` 83 个用例通过 | 没有在真实的 `~/.zshrc`、需要 sudo 的系统路径上测；root 用户下写回失败的用例跳过 |
| 2026-11-16 / U7a：V-02、V-05、V-03 的一部分 | 同上；信号由工具在执行中发出，模拟运行中的 JEV | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：49 个用例（U1～U6 40 个、U7a 6 个、路由契约 3 个） | 停在确认中断上时收到信号、进程在信号等待期间退出，没有测试 |
| 2026-11-16 / U7a 测试有效性 | 同上 | 临时分别去掉步骤边界的结束、去掉"只有一个未结信号"、去掉次数上限、让每轮都清零计数、不发事件、允许执行作废的计划 | 对应用例都失败（分别 1、2、2、1、1、2 个），恢复后都通过 | — |
| 2026-11-16 / U7b：V-20 | 同上；另一个进程用 `python -c` 启动，持有同一个锁文件 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：52 个用例（U1～U7a 46 个、U7b 3 个、路由契约 3 个）；两个模拟重启的旧用例改为先释放旧会话 | Windows 没有测试 |
| 2026-11-16 / U7b 测试有效性 | 同上 | 临时分别让占住总是成功、去掉 `flock`、让本轮开始时的异常被吞掉、执行回退时不占住、释放时不关文件 | 对应用例都失败（分别 2、1、3、1、1 个），恢复后都通过 | — |
| 2026-11-16 / U7c：V-19 | 同上；磁盘剩余空间由测试替换 `shutil.disk_usage` 模拟 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：57 个用例（U1～U7b 49 个、U7c 5 个、路由契约 3 个）；改为只提示后重跑同样 57 个通过 | 真实的满磁盘、上百 GB 的内容目录没有测 |
| 2026-11-16 / U7c 只提示的测试有效性 | 同上 | 临时分别让每轮都提示、让预览也删除、让清除多删、让超限不提示、让预览不按删完后的检查点计算 | 对应用例都失败（"不提示"使 2 个失败，其余各 1 个），恢复后都通过 | — |
| 2026-11-16 / U7c 测试有效性 | 同上 | 临时分别让回退后不清理、把清单记录里出现过的内容都当成还要用、去掉磁盘剩余空间规则、不保留工作区外备份、删除会话时不删记录，以及把"最老的先删"改成"最新的先删" | 对应用例各失败 1 个（不保留备份时是 U6 的"写回失败后继续"用例：备份被清理，重试失败），恢复后都通过 | — |
| 2026-11-16 / U7d：V-16 | 同上；崩溃用不被任何处理器捕获的异常模拟，重启用同一存储上的新 harness 和新 Agent 模拟 | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py -q -p no:cacheprovider` | 通过：63 个用例（U1～U7c 54 个、U7d 6 个、路由契约 3 个） | 真实进程被杀死、依赖安装到一半时崩溃没有测 |
| 2026-11-16 / U7d 测试有效性 | 同上 | 临时分别让回退说明重复写入、去掉新一轮前的检查、让继续时从头做起、在回退执行中仍接受信号 | 对应用例都失败（分别 1、5、1、5 个），恢复后都通过。只按"以 [ROLLBACK] 开头的消息"计数时漏掉了重复的说明（多条说明合在一条消息里），已改为数标记 | — |
| 2026-11-16 / U8：I-06 | 同上；真实的 stdio MCP 服务器（测试里用 FastMCP 写的脚本，`lookup` 标了只读，`write` 没标） | `.venv/bin/python -m pytest tests/unit_tests/harness/recoverable/ tests/unit_tests/harness/test_deep_agent_rail_event_routing.py tests/unit_tests/core/foundation/tool/mcp/test_tool_annotations.py -q -p no:cacheprovider`；core 回归：`.venv/bin/python -m pytest tests/unit_tests/core/foundation/tool tests/unit_tests/core/runner tests/unit_tests/core/single_agent -q -p no:cacheprovider` | 通过：67 个用例（U1～U7d 60 个、U8 ④ 侧 2 个、core 侧 2 个、路由契约 3 个）；core 回归 973 个通过、3 个跳过 | sse、playwright、streamable-http 客户端没有接真实服务器测，改动与 stdio 相同 |
| 2026-11-16 / U8 测试有效性 | 同上 | 临时分别让客户端不带提示、让 ④ 取不到服务器的卡片、忽略只读提示、让服务器提示先于宿主声明 | 对应用例都失败（分别 3、2、2、1 个），恢复后都通过 | — |
| 2026-11-16 / U6 测试有效性 | 同上 | 临时分别去掉"之后又被改过"的比对、不记调用结束时的内容、去掉与工作区重叠的检查、让恢复什么都不做、去掉大小上限 | 对应用例都失败（分别 1、4、1、5、1 个），恢复后都通过 | — |
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
- **副作用（U3）**，回退后直接检查模拟服务的状态：
  - 两张工单按 T-2、T-1 的顺序关闭，已发出的消息保留；计划里的顺序和撤销参数（`T-2`、`T-1`）正确；
  - 回退说明写着"Undone outside the workspace: create_ticket"和"Could NOT be undone and still in effect: send_message"；
  - 人划掉 T-1 的撤销后，T-1 保持打开，状态仍是 `completed`；
  - 第一次关闭失败时，T-1 不再尝试、两张工单都还开着，但工作区照样恢复，状态 `partial`；`resume_rollback` 之后都关闭，状态 `completed`，
    再次 `resume_rollback` 报错；`accept_partial` 后状态为 `accepted`；
  - 登记写不进时工单工具没有执行，模型收到"could not be recorded"；
  - 没有补上结果的调用列为结果未知，不撤销；结果里没有工单号时列为无法撤销；
  - `memory/` 下的文件在回退后仍在，普通文件被删除。
- **shell（U4）**，用真实的 `bash` 工具执行，回退后检查工作区外的文件和进程：
  - 开启 ④ 时，模型拿到的 `bash` 参数里有三个声明参数，`side_effect` 必填；没开启的 Agent 没有；
  - 声明为可撤销的命令在回退时由反向命令删掉它建的文件；
  - 两条失败的命令都列为 `review`，只确认较新的一条时，只有它的反向命令执行；
  - `ssh -V` 声明为无外部效果，被改为不可逆并写明原因；没有声明的命令为不可逆；声明为无外部效果的普通命令不进账本；
  - 含 `$(...)` 的反向命令被拦下，撤销记为失败，回退为 `partial`；
  - 后台的 `sleep 60` 在回退时被停掉，计划和结果里都有它的 pid。
- **依赖（U5）**，回退后直接读环境里的包：
  - Agent 装了 bar、把 foo 从 1.0 升到 1.1，之后用户又装了 baz：计划是卸载 bar、baz，安装 foo==1.0；回退后只剩 foo 1.0，
    回退说明写着"Dependencies in .venv restored"；装包那一步的检查点记下了新的包列表；
  - 目标之后才建的 `.venv` 被整个删除；
  - 装不回去的包（假 pip 拒绝安装）让回退成为 `partial`，结果里带着错误；
  - npm 项目的 `node_modules` 回到 left-pad 1.0.0，is-odd 被卸载；
  - `npm install -g eslint@9.0.0 typescript@5.4.0` 生成的反向命令是卸载 eslint、把 typescript 装回 5.0.0，用户原有的全局包不动。
- **工作区外的文件（U6）**，回退后直接读 `outside/` 下的文件：
  - 一条命令追加了 `rc`、新建了 `created`、删了权限为 600 的 `gone`、改了目录 `config/` 的内容：回退后 `rc` 内容复原，
    `created` 被删，`gone` 连同 600 权限回来，`config/` 回到原来的文件；计划里是 4 项，写明"恢复"或"原来不存在，删除"；
  - 命令之后用户又改了 `rc`：回退时不覆盖，结果为 `kept`，回退状态仍是 `completed`；
  - 两次调用先后改同一个文件：两项都恢复，文件回到第一次调用之前的内容；
  - 超过上限的目录、包含工作区的目录列为不可逆，写明原因；工作区里的路径不列出，由快照恢复；
  - 命令写出的内容超过上限、调用结束时的内容没能记下：列为 `review`，确认后恢复；
  - 父目录改成只读后写回失败，回退为 `partial`，提示可能需要权限；恢复权限后 `resume_rollback` 成功。
- **信号（U7a）**，信号由工具在执行中发出，模拟运行中的 JEV：
  - 第一个工具调用里发出自动信号：回执为等待，本轮在这一步结束后停下，结果的 `result_type` 是 `"rollback"`，第二个调用没有执行；
    本轮结束后回执变为等待确认；执行后回执为已执行，下一轮的回退说明写着"JEV rolled this conversation back"和理由；
  - 同一信号再发一次，返回同一张回执；有未结信号时另一个信号被拒绝；人拒绝后计划不能执行、历史不变；目标已删除的信号被拒绝；
  - 人发起的信号取代未结的自动信号，被取代的计划不能执行；自动信号不能取代人发起的；
  - 同一目标第 3 次自动信号、同一轮超过上限的自动信号被拒绝；新的一轮重新计数；以写回回退开始的一轮继续计数；
  - 事件依次为已接收、等待确认、已执行，以及已接收、已拒绝；检查点事件带 `checkpoint_id`、`seq` 和轮次。
- **一个工作区一个会话（U7b）：**
  - s1 跑过一轮后，同一个 Agent 上 s2 的一轮报错"session s1 already has rollback on"，s1 照常运行；
    `release_session("s2")` 返回 False，释放 s1 后 s2 能运行；这时执行 s1 的回退被拒绝，s1 的历史不变；
  - 另一个进程持有锁文件时，本轮报错并写出那个进程的 pid；那个进程退出后，本轮正常运行；
  - 写回回退失败时本轮报错、模型没有被调用；故障消失后下一轮正常写回，模型收到回退后的上下文。
- **存储清理（U7c）**，直接看内容目录里按 sha256 命名的文件：
  - 回退到第 1 步之后，只有被丢弃的第 2 步写过的内容被删，第 0、1 步的内容还在；
  - 上限 450 KB、五步各写 200 KB，再跑一轮：两轮都没有删任何检查点和内容，`storage_low` 只发了一次，提示里写着可用的做法；
    预览"保留最近 3 个"报告删 6 个、腾出 4 × 200 KB，预览后历史不变；真正清除的结果与预览相同，剩第 6、7、8 个检查点，
    前四个版本的内容被删；回到上限以内；回退到剩下最老的检查点仍然正常；`keep_latest=0` 被拒绝；
  - 磁盘只剩 10% 时，历史完整保留，事件写明"less than 20% of the disk is free. Nothing was deleted"；调低 `min_free_ratio` 后不再超限；
  - 删除会话后，历史为空，`recoverable:s1:` 和 vcs 的 `s1:` 下没有任何键，计划被删，只有它用到的内容被删，引用它的检查点报错。
- **崩溃后接着做（U7d）**，用模拟的工单服务和文件写入，回退到第 0 个检查点：
  - 分别在停完进程后、两个撤销之间、撤销之后、提交开始时、提交中途（说明已写、HEAD 未动）崩溃；重启后新的一轮报错
    "was interrupted"、模型没有被调用，新的回退信号被拒绝；`resume_rollback` 之后状态 `completed`，两张工单按 T-2、T-1 各关闭一次，
    文件被删除，HEAD 是目标；下一轮里回退说明只出现一次，并写着"0 written back, 1 deleted"；
  - 撤销工具已执行、记录还没补上时崩溃：继续后这一项记为失败（"check it by hand"），回退为 `partial`，工单没有被再关闭一次。
- **MCP 工具（U8）**，Agent 通过真实的 stdio MCP 服务器调用：
  - 服务器列出的卡片带着提示：`lookup` 是 `{"readOnlyHint": true}`，`write` 是 `{"destructiveHint": false}`；模型看到的工具信息里没有提示；
  - `mcp_notes_lookup` 和 `mcp_notes_write` 都真的执行了（服务器写了日志）；回退计划里只有 `mcp_notes_write`，列为不可逆，
    原因写着"an MCP tool its server does not mark read-only"，只读的 `lookup` 没进账本；
  - 宿主在声明表里把 `mcp_notes_write` 写成无外部效果后，计划里没有任何外部效果。

## 验证结论

- **已证明的范围：** 单轮模式下，检查点在步骤边界建立并能去重；确认后的上下文回退和回退说明；同一进程内和"新 harness + 空 Session"两种情况下的恢复；
  不匹配或已执行过的确认被拒绝；存储在工作区内时被拒绝。
- **未覆盖的：** 任务循环模式；正在运行时拒绝回退的路径（`_running` 只在进程内有效）；真实 CLI 的重启流程。
- **U2 已证明的范围：** 本机 APFS 上的工作区快照与恢复，包括上面列出的各种情况。
- **U2 未覆盖的：** Linux（reflink、inotify 上限都不涉及，但复制路径只是模拟的）、Windows；文件属主和目录权限的恢复；
  写回失败后的重试；回退时有其他进程正在写工作区。
- **U3 已证明的范围：** 声明过的工具在回退时按顺序撤销、划掉、失败即停、继续和接受；登记失败不执行；结果未知和无法撤销的处理；记忆不回退。
- **U3 未覆盖的：** 真实的 cron 后端；`bash`、`code`、MCP 工具，
  它们在 U4、U8 之前都按不可逆处理。
- **U4 已证明的范围：** `bash` 的逐条声明、对外命令判断、失败命令的确认、反向命令复检、后台进程停止。
- **U4 未覆盖的：** `code` 工具（同一套逻辑，没有端到端测试）；模型实际会不会认真填写声明（需要真实模型评估）；Windows。
- **U5 已证明的范围：** 包列表的记录和按列表恢复、新建环境的删除、失败时 `partial`、全局 npm 安装的反向命令，都是在假的包管理器上。
- **U5 未覆盖的：** 真实的 pip、uv、npm、brew；联网安装；装回已下架版本、从本地路径或 git 地址安装的包；conda 环境。
- **U6 已证明的范围：** 声明过的工作区外文件和目录的备份与恢复、新建的删除、删除的复原（含权限位）、之后被改过的保留、
  同一文件多次修改的倒序恢复、无法备份时列为不可逆、写回失败与重试。
- **U6 未覆盖的：** 真实的用户主目录和需要 sudo 的系统路径；`code` 工具；另一个卷上的文件（复制路径）；恢复到一半进程崩溃。
- **U7a 已证明的范围：** 运行中和空闲时的信号、去重、只有一个未结信号、人取代自动、人拒绝、自动次数上限、事件。
- **U7a 未覆盖的：** 停在确认中断上时收到信号；进程在信号等待期间退出后查询回执；建立检查点失败的事件。
- **U7b 已证明的范围：** 同一进程两个会话、两个进程之间的互斥，释放后可以使用，进程退出后锁自动释放；本轮开始时失败会中止本轮。
- **U7b 未覆盖的：** Windows（只有进程内的判断）；网络文件系统上的 `flock`；CLI 换会话时是否调用 `release_session`。
- **U7c 已证明的范围：** 回退后清理、超限只提示一次、预览与清除旧检查点、调整上限、删除会话，以及部分完成的回退重试时备份仍在。
- **U7c 未覆盖的：** 多个会话共用一个工作区时的清理（只有删除会话的用例涉及）；上百 GB 时遍历内容目录的耗时；网络文件系统。
- **U7d 已证明的范围：** 五个位置崩溃后的继续、不重复撤销、说明只写一次且数字准确、崩溃期间拒绝新一轮和新信号、结果未知的撤销只报告。
- **U7d 未覆盖的：** 真实的进程被杀死（用异常模拟）；依赖安装、文件写回进行到一半时崩溃。
- **U8 已证明的范围：** stdio 客户端带上服务器的提示；④ 按 `readOnlyHint` 给声明表里没有的 MCP 工具定默认类别；宿主声明优先。
- **U8 未覆盖的：** 其他三种连接方式接真实服务器；服务器提示不实时的后果（只能靠宿主覆盖）。
- **交付判断：** U1～U8 都满足各自范围。整个特性还不能算交付：真实包管理器（V8、V9）、真实模型是否认真声明、
  CLI 接入（按 session_id 重新打开会话、换会话时 `release_session`）、停在中断上回退后又收到恢复输入，都还没有验证或处理。

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
| 任务循环模式 | 未实现 | 不建检查点 | 待定 |
| 模型漏写的工作区外路径 | 已知限制 | 不会备份，回退说明提示"没有声明的工作区外文件没有处理" | 靠声明；无更好办法 |
| 命令顺带建的上级目录 | 有意取舍 | 声明 `~/.config/new/x` 而 `new/` 原来不存在时，回退只删 `x`，留下空的 `new/` | 让模型声明最上面一级新建的目录 |
| 工作区外的目录恢复到一半进程崩溃 | 已知限制 | 继续时这个目录既不是备份的样子、也不是调用结束时的样子，会被当成"之后又被改过"而保留并报告，不会被覆盖。单个文件的写回是原子的，没有这个问题 | 需要时在写回前登记意图 |
| 真实包管理器上的依赖恢复（V8、V9） | 未验证 | 只在假的包管理器上测过 | 在真实项目上实测 |
| pnpm、yarn、conda 环境 | 未实现 | 记下包列表，恢复时只报告 | 有需要再加 |
| 全局 `pip` 的识别 | 已知限制 | 按 ④ 所在进程的 `VIRTUAL_ENV` 判断是不是全局；命令里先激活虚拟环境再装包时，前后比较的是全局 pip，差异为空 | 需要时改为解析命令实际用的解释器 |
| Node 只看顶层包 | 有意取舍 | 嵌套依赖随顶层包一起装卸，不单独记 | — |
| 人拒绝 JEV 的计划后，下一轮清零"同一轮"的计数 | 已知限制（需求方 2026-11-16 接受） | JEV 发信号、人拒绝，反复下去不受"同一轮"上限限制；"同一目标"上限仍然有效 | 需要时由宿主标记新需求 |
| CLI 换会话时释放工作区 | 未接入 | CLI 在同一进程里开新对话时不调用 `release_session` 的话，新会话的第一轮会被拒绝 | 接入 CLI 时处理 |
| Windows 上跨进程的互斥 | 未实现 | 没有 `fcntl`，只在进程内判断 | 有需要时用 `msvcrt.locking` |
| 名单以外的对外命令（如 `python deploy.py`） | 已知限制 | 以模型的声明为准，确认时有提示 | 无更好办法，靠声明和名单补漏 |
| 模型是否认真填写声明 | 未验证 | 没填的按不可逆，回退说明会多出"撤不回"的条目 | 用真实模型评估 |
| MCP 服务器的提示不实 | 已知风险 | 服务器把会写东西的工具标成只读时，这个工具不进账本，回退说明里看不到它 | 宿主在声明表里覆盖；服务器的提示只作默认值 |
| 其他 MCP 来源 | 未改 | OpenAPI 转成的 MCP 工具（`openapi_client`）没有提示，按不可逆；`harness/tools/browser_move` 自带的两个客户端没有带上提示 | 有需要时同样加上 |
| "经用户批准过的调用"标记 | 未实现（P7） | 计划里不能标出 | 待 rails 维护者 |
| 账本的参数摘要可能含密钥 | 已知风险（P20） | 摘要最多 500 字符，存在本机存储里 | 和敏感内容处理一起做 |
| vcs 里早期的上下文日志不删 | 已知限制 | 被删的老检查点的上下文还留在 vcs 日志里，随会话删除才删 | 需要时给 vcs 加删除前面日志的操作 |
| 用户一直不处理存储不足 | 已知限制 | 只提示一次；磁盘真的写满后，建检查点失败（发出失败事件），Agent 照常运行 | 宿主可以随时用 `storage_status` 再提示 |
| 同一个存储目录配多个 `kv_store` | 不支持 | 清理只看得到自己的 `kv_store` 里的会话，会删掉另一个 `kv_store` 的会话还要的内容 | 写进接入说明：一个存储目录只配一个 `kv_store` |
| 回执、计数记录不清理 | 有意取舍 | 很小，随会话删除 | — |
| 文件属主、目录权限位、扩展属性不恢复 | 有意取舍 | 只恢复文件内容和文件的权限位 | 有需要再做 |
| 保留原修改时间、大小也不变的改写（如 `cp -p`），发生在上次扫描 2 秒以外 | 已知限制（V6） | 不会被发现，回退时不恢复 | 改用文件变更通知时可以解决（R-08） |
| 沙箱模式 | 有意取舍 | 不做工作区快照，回退说明写明文件没有回退 | P3 |

## 实施演进与修订

### 2026-11-13 U2：回退说明改为如实说明文件已恢复

- 原结论：U1 的回退说明写"工作区文件和外部效果没有回退"。
- 变化：工作区快照可用时，说明改为写明写回和删除了多少文件、删除了哪些构建产物、哪些文件没能恢复，
  以及依赖目录和工作区以外的效果仍没有回退。快照不可用时（沙箱、目标检查点没有清单），仍写"工作区文件没有回退"。
- 旧结论的效力：只在快照不可用时仍然适用。

### 2026-11-16 U3：记忆目录不回退

- 原结论：U2 把工作区下的一切（依赖目录、构建产物除外）都放进快照，回退时一起恢复。
- 新证据：DeepAgent 的记忆工具写在工作区内的 `memory/`、`coding_memory/`（`tools/memory.py`、`tools/coding_memory.py`
  通过 `workspace.get_node_path` 取路径），U2 的做法会把记忆一起回退，与设计的"记忆写入不回退"矛盾。
- 确认：2026-11-13 需求方选择记忆优先，设计第 4.1、10 节已修订。
- 最终变化：这两个目录不进快照、回退时不动；记忆工具在声明表里是"保留"类，回退说明列出它们。
- 旧结论的效力：对记忆目录以外的路径仍然适用。

### 2026-11-16 U3：工作区写回失败重试一次

- 原结论：U2 写回失败不重试，列为遗留。
- 变化：失败的文件重新扫描后再写一次，仍失败的列为未恢复，回退状态为 `partial`。

### 2026-11-16 U7b：本轮开始时写回失败改为中止本轮

- 原结论：U1 在 `BEFORE_INVOKE` 写回回退或恢复 Session 失败时抛出异常，以为本轮会报错停下（验证结论里列为"未覆盖"）。
- 新证据：回调框架只记录普通回调异常的日志、不往外抛（`core/runner/callback/framework.py` 的 `trigger`），
  只有 `AbortError` 会传给调用方。所以写回失败时，本轮其实在没有回退的上下文上照常运行了。
- 变化：`CheckpointRail.before_invoke` 把失败包成 `AbortError`，调用方收到原来的错误，模型不会被调用；待写回的回退保留，下一轮再写。
- 旧结论的效力：不再适用。

### 2026-11-16 U7c：超过存储上限从自动删除改为只提示

- 原结论：按设计 D-11，超过 100 GB 或磁盘剩余不足 20% 时，每轮结束从最老的检查点删起，每批删四分之一，并发出事件。
- 新证据：磁盘剩余空间不只取决于 ④，被别的程序占满时，每轮结束都会把历史删到只剩最新检查点。
- 确认：需求方 2026-11-16 决定两条上限都只提示，由用户选择腾出空间、调大上限，或按"每个会话保留最近 N 个"清除，清除前先报告能腾出多少空间。设计 D-11、第 5.4、5.10 节和 V-19 已修订。
- 最终变化：`storage_low` 事件（每次超限一次）、`storage_status`、`set_storage_limits`、`prune_checkpoints(keep_latest, dry_run=...)`。
- 旧结论的效力：不再适用。
