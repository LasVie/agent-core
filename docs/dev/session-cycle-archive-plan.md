# Session 私有历史与 ReAct 周期归档实施计划

日期：2026-09-18。状态：实现及本地持久化验收已完成；仅修改 agent-core 仓库。

## 1. 分支与规划依据

- 目标仓库：[LasVie/agent-core](https://github.com/LasVie/agent-core)。
- 工作分支：`feature/session-cycle-archive`，已从最新 `origin/develop` 创建并推送。
- 代码核对基线：`6dfda012`（`feat(team): add group conversations and reliable member inputs`）。本地工作分支已包含相对原工作目录新增的 28 个提交。
- 需求输入：工作空间 `documentation/` 中的《群聊 && 组织级 Agent功能方案设计》《群聊 && 组织级 Agent代码模块设计》《群聊与组织级Agent接口设计》，以及 Session 管理补充。它们位于本 Git 仓库之外；本计划在下面重述实现所需约束，避免远端阅读依赖本地绝对路径。

本文件保留已确认的实施约束，并记录实际文件、验证结果和边界。所有实现、测试、示例和文档改动均位于本仓库，不包含 WorkSwarm 文件或依赖版本变更。默认处理链与已有压缩行为保持不变。

## 2. 固定需求与仓库边界

### 首要约束：保持原有 core 功能和默认行为

本特性以可选扩展方式接入，不能通过替换默认实现实现新需求。“旧功能不受影响”是实施与合入条件，需要回归证据，不能仅凭配置默认关闭就视为已经满足。

1. **默认关闭、显式启用**：未配置新能力或显式关闭时，继续原有执行路径。只有明确启用历史配置并选择 CycleArchiveProcessor 的运行实例进入新逻辑；不完整或冲突的新配置只在该实例启用时校验报错，不影响旧配置加载。
2. **默认处理链不变**：不全局将 ContextProcessorRail 的 preset 默认值改为 False，不替换已有 Processor 注册键、默认阈值、摘要压缩、TTL、自动恢复/召回和工具处理策略。新 Processor 使用独立名称注册，导入新模块不能改写已有注册映射。
3. **旧调用接口不变**：保留已有名称、位置参数、返回结构和配置默认值，不新增必填参数。可选能力不增加第三方 ModelContext 子类必须实现的抽象方法，不修改旧错误码及其映射。
4. **旧消息与持久化格式不变**：未启用时不增加归档专用消息字段、执行结果字段或 checkpoint 命名空间，不改动原文件目录、保存时机和状态恢复方式。已启用模式的新字段与旧字段分开保存，新版本必须能读取缺少新字段的旧 checkpoint；不自动迁移或覆写旧历史文件。
5. **严格失败处理仅作用于新模式**：新模式的归档失败和预算超限在其请求出口阻断。旧模式保持原有 Processor 异常处理、文件卸载失败回退、模型重试与压缩兜底语义；不能全局把捕获后继续改成直接抛出。
6. **未启用不承担新工作**：不创建历史记录器/目录、不复制原始消息、不额外计数 tokens、不写归档、不启动后台任务，也不改变原有模型请求次数和工具执行顺序。公共接入点只做轻量模式判断。
7. **运行实例隔离**：新模式的记录器、错误状态、预算和存储绑定当前 Session/context；同一进程中的旧 Agent、其它 Session 和其它 ContextEngine 不能被带入新模式。不得修改共享配置对象或使用全局开关切换所有实例。
8. **生命周期只作受控扩展**：DeepAgent invoke/stream、取消、中断恢复和 task-loop 的新增记录/导出逻辑只在新模式进入；旧模式继续原有回调顺序、状态提交、资源清理与最终输出时机。
9. **配置切换不静默改写存量上下文**：第一版在运行实例创建时确定模式，不在执行中的 Session 热切换归档策略。旧快照导入新模式的兼容逻辑与默认旧路径分开验证，不能扫描全部历史或自动补回卸载内容。
10. **新旧两组验证均通过才交付**：保留原有测试的预期，新增默认关闭对照与同进程混用测试。不得为了使新策略通过而把旧压缩、旧恢复或旧流式输出测试改成新行为。

### 不改变的业务接口与执行方式

- `(agent_id, group_chat_id)` 唯一对应稳定的 `session_id`。同 Agent 跨群、同群不同 Agent 的私有状态均隔离。
- `SessionManager.get_or_create(agent_id, group_chat_id)`、`AgentExecutor.execute(input)`、`ContextAssembler.load/prepare/save` 的预留业务签名保持不变。
- 一次外层 `DeepAgent.invoke` 只在开始调用一次 load、prepare，统一退出时调用一次 save。内部模型和工具循环不调用业务 ContextAssembler。
- DeepAgent 决定执行任务、回复或正常静默，不增加 AgentResponseDecider。
- 历史读取使用已有 grep/read_file 和宿主授予的路径，不新增搜索、状态或 new_context 工具。
- 超限时按最早的完整 ReAct 周期归档原文，再从活跃上下文移除；不摘要、不按字符截短正文、不启用 TTL 压缩。
- 周期包括 assistant 输出及全部对应工具结果；并行调用必须全部闭合。本轮用户输入、最新完整周期和未闭合调用受保护。
- 归档写成功才移除，写失败不退回内存冒充成功；轮末另存完整轨迹，下一轮只恢复截断后的状态。

### 本仓库的交付范围

| 层 | 本次规划的职责 |
|---|---|
| agent-core / core | 周期识别、输入预算、原始消息记录、Session 范围内的 JSONL 归档、活跃状态更新、最终请求校验 |
| agent-core / harness | DeepAgent 外层执行标识与记录生命周期、原生 Processor 装配、异常/中断时的记录导出与状态保留 |
| agent-core / examples、tests、docs | 用两个独立原生 Session 演示和验证归档、完整轨迹保存与恢复，提供配置及接入说明 |

本分支实现可独立使用的 SDK 能力，不在 `core/context_engine` 中加入 `group_chat_id`、飞书 Bot、群路由或业务 SessionManager。上下文引擎只消费调用方确定的原生 Session、执行标识和存储配置。Agent × 群映射和预留 ContextAssembler 接口是接入约束，不在本提案中迁入 core 或要求改动其它仓库。验收使用原生 Session 和 SDK 示例，不依赖业务平台联调。

## 3. 已核对的原生能力与缺口

下列路径均相对于 agent-core 根目录。

| 现有文件 / 能力 | 复用方式与缺口 |
|---|---|
| `openjiuwen/core/context_engine/context/session_memory_manager.py`：`group_completed_api_rounds` | 复用闭合周期的 `[start, end)` 分组。首段可能带 user 消息，需要额外保护当前输入；不启用 Session Memory |
| `openjiuwen/core/context_engine/processor/forked/compressor/base.py`：`adjust_keep_recent_for_tool_boundaries` | 复用工具边界保护，集中封装内部依赖，不直接使用摘要压缩器 |
| `openjiuwen/core/context_engine/context/context_utils.py`：`ensure_context_message_ids` | 已有 `metadata.context_message_id`；归档 `message_id` 直接采用它，不重新生成第二套消息身份 |
| `openjiuwen/core/context_engine/processor/budget_guard.py`、`token/base.py` | 复用上下文上限解析、消息与工具 token 统计；现有头尾截短函数不适用于本需求 |
| `openjiuwen/core/context_engine/processor/base.py`：`offload_messages` | 已支持批量消息和文件引用，但默认写 JSON 且文件失败回退内存；新策略需要独立的严格 JSONL 写入适配，不能直接沿用默认失败语义 |
| `openjiuwen/core/context_engine/context/context.py` | 已有消息追加、窗口处理和状态保存。`CONTEXT_UPDATED` 在 add 处理后通知，不能据此保证取得改写前原文；需要前置记录接入点 |
| `openjiuwen/core/single_agent/agents/react_agent.py`：`_railed_model_call` | 在 BEFORE_MODEL_CALL 之后才获取窗口；附件等 window mutator 又晚于归档 Processor。最终校验必须位于最终窗口确定后、LLM invoke/stream 之前 |
| `openjiuwen/core/context_engine/context_engine.py`、`openjiuwen/core/session/agent.py` | 复用 create_context、save_contexts、Session.commit。更新 Session 内存状态和持久提交仍需区分 |
| `openjiuwen/harness/rails/context_engineer/context_processor_rail.py` | 已支持 `preset=False` 和自定义 Processor；主要复用现有注册接口。该 Rail 还包含工具配对修复等行为，不能假定它只有 init，也不把它当业务 ContextAssembler |
| `openjiuwen/harness/rails/evolution/trajectory_rail.py` | 现有演化轨迹基于观测 span，保存失败被记录后隔离；不能直接作为本方案完整原始消息与严格持久化的唯一来源 |

最新的 `agent_teams` 群聊实现已有 `tools/group_conversation.py`、`Runner.post_group_message` 和成员邮箱投递，可参考公开历史、去重和生命周期处理，但有三处不同：

1. 历史按 team/session 保存为 `history.json`，不是本方案的 Agent × 群私有 Session。
2. 无 mentions 时只归档，不通知 Agent；不能直接满足“有效消息进入 DeepAgent 自主判断”。
3. 公开历史写入与成员通知不是同一事务，重复 client ID 不补发通知；不能直接当作宿主的可靠逐 Agent 待办。

本分支保持这些现有群聊语义。`agent_teams/agent/session_manager.py` 管理 TeamAgent 会话绑定，也不直接复用为新的业务映射管理器。

## 4. 已新增的实现文件

| 新增路径 | 主要内容 | 为什么放这里 |
|---|---|---|
| `openjiuwen/core/context_engine/schema/history.py` | `SessionHistoryConfig`、`ArchiveRecord`、`ArchiveRef` 及执行记录的数据结构 | 与原生上下文配置及消息 schema 同层；不引入群业务字段 |
| `openjiuwen/core/context_engine/context/react_cycle.py` | 包装既有周期分组和工具边界函数，输出候选范围、受保护消息及最小可移除前缀 | 隔离对内部 helper 的依赖；不复制分组算法、不重写默认压缩器 |
| `openjiuwen/core/context_engine/history/__init__.py` | 历史记录与存储的必要导出 | 新增小型内部包，避免入口文件承载实现 |
| `openjiuwen/core/context_engine/history/recorder.py` | 原始消息的不可变记录、发生时间、执行内 seq、周期关联、执行结束导出 | 记录器服务轨迹和卸载；不维护第二份可变模型上下文 |
| `openjiuwen/core/context_engine/history/store.py` | 绑定 Session 根目录的 JSONL 写入、稳定 archive_id、重复写入校验、引用链保留 | 统一轨迹与卸载的文件语义；先确认文件发布成功，再允许移除消息 |
| `openjiuwen/core/context_engine/processor/offloader/cycle_archive_processor.py` | `CycleArchiveProcessor` 与配置；预算选择、完整周期归档、成功后替换窗口、超大工具结果整条落盘 | 属于无摘要的 offloader，和现有 MessageOffloader 并列，按需启用 |

第一版采用宿主可持久化且 Agent 工具可读取的文件系统目录。写入使用同目录临时文件、完整写入与刷新、原子发布；相同 ID 的已有文件必须校验内容，不能覆盖不同原文。文件系统失败返回明确错误，不自动改写到别的目录或内存。若使用远程沙箱，宿主必须提供满足同等写入语义的可读挂载或适配；本分支不同时开发新的远程存储平台。

## 5. 已修改的现有文件

### 原计划的 8 个接入文件

| 修改路径 | 精确接入点与要求 |
|---|---|
| `openjiuwen/core/context_engine/schema/config.py` | 增加可选历史配置，默认关闭；启用周期归档时校验与消息数上限、默认窗口提前裁剪及其它会改写原文的 Processor 不冲突 |
| `openjiuwen/core/context_engine/__init__.py` | 导出新 Processor/Config 和必要历史配置；保留所有现有导出及默认行为 |
| `openjiuwen/core/context_engine/context_engine.py` | 注册新 Processor；按原生 session/context 绑定记录器和存储，支持执行记录导出；保留 create_context 现有参数，不增加必填参数 |
| `openjiuwen/core/context_engine/context/context.py` | 仅新模式在消息已标准化、已有 context_message_id、尚未交给 add Processor 时记录原文；关联当前执行；保留 set_messages 原有 usage/KV 失效机制；新模式快照隔离保存日志元数据与引用，旧格式及恢复行为不变 |
| `openjiuwen/core/context_engine/processor/budget_guard.py` | 增加仅新模式调用的最终窗口预算和失败状态检查；保留既有函数语义和默认策略，新策略不调用头尾字符截短函数 |
| `openjiuwen/core/single_agent/agents/react_agent.py` | `_railed_model_call` 在新模式的最终窗口确定后校验，覆盖 invoke/stream；未启用时不增加 token 统计或阻断条件。新归档/预算错误不能被重试或压缩兜底掩盖，旧错误路径与模型调用顺序不变 |
| `openjiuwen/core/common/exception/codes.py` | 在合法 CONTEXT 段定义归档写入与输入预算错误，遵循 StatusCode 命名规则，不写死未核对的编号；宿主映射为 ARCHIVE_FAILED / CONTEXT_BUDGET_EXCEEDED |
| `openjiuwen/harness/deep_agent.py` | 仅新模式在外层 invoke/stream 绑定一次 execution_id，完成、中断或异常时冻结并提供已有记录；内层 task-loop 不重复开启业务执行。旧模式的回调、状态保存、取消与流式最终输出时机不变 |

### 核对后增加的两个内部接入点

| 路径 | 实际接入原因 |
|---|---|
| `openjiuwen/core/single_agent/rail/base.py` | 通用 rail 装饰器原本允许异常回调重试或 force_finish，可能吞掉最终守卫的错误。仅对新增的归档失败、预算超限两种状态直接透传；旧异常策略保持不变，不新增接口 |
| `openjiuwen/harness/rails/context_engineer/context_processor_rail.py` | 复用现有 `preset=False` + 自定义 Processor 注册；仅新模式跳过原工具配对修复，避免 pop / 重新追加改写未闭合原文。旧实例的修复和默认值保持不变 |

`openjiuwen/core/context_engine/base.py` 无需修改；第三方 ModelContext 不增加抽象方法。ReActAgent 的取消清理在新模式保留未闭合原文，并将持久提交交回外层，使完整轨迹先于 checkpoint 提交。

### 直接复用，不预设修改

`openjiuwen/core/context_engine/processor/base.py` 的旧文件卸载与内存回退默认行为不全局修改：新 Processor 采用专用严格写入路径，并复用原生 offload 消息/引用结构。`openjiuwen/core/session/agent.py` 的 commit 接口也不另起一套实现。

配置继续通过既有 ContextEngineConfig、ProcessorConfig 和 DeepAgent 配置透传；不向 create_deep_agent 增加一串业务参数。session_id 来自原生 Session，execution_id 由宿主在本轮开始时绑定；独立 SDK 示例可由执行入口生成。不可让模型输入决定另一个 Session 的身份或磁盘根目录。

## 6. 实现时的关键规则

### 6.1 阈值与范围

1. 输入预算 B = 模型上下文上限 − 输出预留 − 安全余量；新模式必须有明确有效预算，不照搬 20,000 token 的旧默认值。
2. 统计 system、工具定义、历史、本轮输入、附件表示和引用；预算选择计入将要保留的引用。
3. 复用完整周期分组，保护本轮全部用户指令、最新一个完整周期及未闭合调用；源分组包含 user 消息时，只允许归档副本，不能连带删除受保护输入。
4. 从最早可移除周期逐个累加，取使输入符合预算的最少周期数。归档记录来自原始消息记录器，不能用之前的文件占位内容覆盖原文。
5. 原文文件确认发布成功后，一次性更新 ModelContext 和本次 ContextWindow。使用 set_messages 的既有 usage/KV 更新语义，不直接篡改内部列表。
6. 最新周期过大时，超大工具结果整条保存，正文换成文件引用，保留 role、tool_call_id 及配对关系。仍放不下则停止请求。

处理器之后可能追加附件。最终出口发现预算仍超限时，第一版明确报错，不再返回业务 prepare，也不私自循环运行另一条压缩链。后续若优化附加内容的预算预留，应保持同一出口校验。

### 6.2 失败必须阻止模型请求

当前 ContextEngine 会捕获部分 Processor 和 window mutator 异常，单纯在 Processor 中 raise 不足以保证停止。新模式要在本次执行/请求绑定的记录状态中留下归档失败，并由最终请求出口检查、通过原生错误系统抛出。该状态不能跨 Session 混用，也不能因下一个回调成功就被清掉。

必须同时验证非流式和流式请求都没有到达 provider。保存失败不删除活跃原文；若先成功发布了文件、后续状态提交失败，可以留下未引用文件，但重试应复用稳定标识，不丢原文、不重复执行已完成工具。轮末保存发生异常时保留原执行错误及保存错误，不能以空回复掩盖。

### 6.3 原始轨迹与状态

```text
root_dir/
  sessions/<sha256(session_id)>/history/
    offload/<content_sha256>.jsonl          运行中先保存，再移除消息
    trajectories/<sha256(execution_id)>.jsonl  外层执行退出时保存完整轨迹
  state.db                                 示例采用的原生 SQLite checkpoint
```

路径中的 ID 编码防止目录穿越，JSONL 内仍保存原始 ID。checkpoint 位置由宿主管理，不强制位于上述 root_dir。临时文件 flush/fsync 后使用同目录 hard-link 原子发布；文件系统必须支持该语义。

- 记录字段采用现有设计：session_id、execution_id、step_id、seq、message_id、occurred_at、archived_at、完整 message。`step_id` 在本方案中标识 ReAct 周期，不复用 team 协议中表示原子动作的 Step；跨层适配明确映射。
- `message_id` 复用 context_message_id，群平台 message_id 单独作为来源标识。归档不因发生在后一轮就重写原始 execution_id/发生时间。
- 时间在消息产生或接收时采集，采用带时区 ISO 8601；并行工具按进入记录器的顺序分配 seq。未完成周期也保留在轨迹中，不被当作完整周期移除。
- 记录器保存改写前的不可变数据，不从最终活跃消息反推完整轨迹。AFTER_REACT_ITERATION 可辅助闭合标记，但不是唯一采集点。
- offload 与 trajectories 允许原文重复，按 `(session_id, message_id)` 识别重复；grep 不自动去重，不新增去重搜索服务。
- 轮末 save 先保存轨迹及引用，再提交原生状态。运行中的 offload 写入不调用业务 save；底层 checkpoint 不等于再次调用业务 ContextAssembler。
- load 只恢复 state，不扫描历史回填；旧 checkpoint 缺少新增字段时保持可读取。历史旧消息没有发生时间时不得用当前时间冒充，兼容迁移需明确标注未知来源时间。
- 第一版不自动清理被引用归档，不承诺仅凭轮末 save 恢复进程突然终止前的全部未落盘轨迹。

## 7. SDK 配置与调用边界

新能力采用显式配置启用，默认调用方保持当前行为。配置提供历史存储位置、输入预算所需参数和周期归档 Processor；用已有 ContextProcessorRail 的 `preset=False` 注册，不另建一条模型或工具执行循环。

调用方使用现有 `DeepAgent.invoke(inputs, session)` / `stream` 传入原生 Session。SDK 按 session_id 隔离活跃上下文、记录器和文件空间，提供本次执行轨迹导出及严格保存能力，并复用原生状态保存和 Session.commit。示例代码负责演示轮首恢复、一次 invoke 和轮末保存，不新增群平台依赖。

本分支明确保证：

- 两个不同 session_id 的消息、发生时间、归档路径和运行错误不会串用。
- 同一 Session 的顺序执行与重启恢复保留稳定状态和文件引用。
- 同一 Session 不支持多个调用方同时写入；示例和接入说明要求调用方串行执行，不声称目录隔离本身提供分布式锁。
- 历史存储只在绑定的 Session 根目录写入，不接受模型指定任意私有目录。
- SDK 的写入范围检查不等同于 Agent 文件工具的访问授权；工具沙箱/权限仍沿用现有运行环境，本分支不新建权限平台。

预留业务 `load/prepare/save` 可在以后封装这些 SDK 能力，签名和每轮调用边界保持不变。本提案不包含其它仓库的实现清单或联调阶段。

## 8. 测试、示例和文档清单

### 新增

| 路径 | 验证目标 |
|---|---|
| `tests/unit_tests/core/context_engine/test_react_cycle.py` | 完整周期、并行工具、未闭合调用、本轮用户输入、最新周期保护、最少前缀选择 |
| `tests/unit_tests/core/context_engine/test_session_history_store.py` | JSONL 原文一致、原子发布失败、重试幂等、路径范围、时间/ID 保留、已有引用链 |
| `tests/unit_tests/core/context_engine/test_session_history_recorder.py` | 改写前采集、执行边界、同一消息不因恢复重复记录、多 Session 隔离、旧快照缺字段 |
| `tests/unit_tests/core/context_engine/test_cycle_archive_processor.py` | 未超限不写盘、阈值触发、失败不移除、整条工具结果落盘、同步更新状态与请求窗口 |
| `tests/unit_tests/core/context_engine/test_cycle_archive_compatibility.py` | 未配置/显式关闭的基线对照、旧 checkpoint、第三方 ModelContext 兼容、新旧实例同进程运行、无额外归档 I/O |
| `tests/unit_tests/agent/react_agent/test_react_agent_cycle_archive.py` | mock provider 验证归档失败和最终超限时零模型调用；覆盖 invoke/stream、附件、工具循环和取消 |
| `tests/unit_tests/harness/test_deep_agent_session_history.py` | 一次外层 invoke 对应一份轨迹；多次工具及内层 task-loop 不重开执行；中断、失败、恢复和两 Session 隔离 |
| `examples/context_engine/session_cycle_archive.py` | 无需群平台的最小示例：两 Session、多周期、低预算触发卸载、轮末导出和下轮恢复 |
| `docs/zh/2.开发指南/API文档/openjiuwen.core/context_engine/processors/cycle_archive_processor.md` | 新配置、归档格式、错误、原生与业务生命周期区别 |
| `docs/en/2.Development Guide/API Docs/openjiuwen.core/context_engine/processors/cycle_archive_processor.md` | 对应英文 API 文档 |

### 已有回归与同步的设计说明

保留以下旧测试文件及其预期，直接运行回归；新断言集中在新增的测试文件，不将旧用例改为新行为。

- `tests/unit_tests/core/context_engine/test_context_engine.py`：默认关闭时保持原行为，保存恢复引用。
- `tests/unit_tests/harness/test_context_processor_rail.py`：preset=False 配置、新旧 Processor 互不污染、工具配对修复与新策略兼容。
- `tests/unit_tests/harness/test_deep_agent_session_state.py`：失败/中断后状态可恢复，不自动回填已卸载内容。
- `openjiuwen/harness/docs/specs/S_02_deep-agent-architecture.md`：实施时同步执行记录生命周期和异常保存契约。
- `openjiuwen/harness/docs/specs/S_04_rails-contract.md`：实施时同步 Processor 装配和最终检查的边界；优先不新增回调事件，若确需新增，必须同步 DeepAgent 路由集合及测试。
- `openjiuwen/harness/docs/features/F_05_session-cycle-archive.md`：实施时新增决策与验证记录；F_05 是当前可用下一编号，落地前重新确认，避免与并行开发冲突。

测试采用本地临时目录与 mock 模型，不依赖真实模型凭证。除了验证归档内容，必须断言失败时 provider 未被调用、消息未被移除、另一 Session 的状态与文件未变化。

### 原有功能回归与对照

在相同 mock 模型、工具结果、旧配置和旧快照下，对照开发基线与变更后的默认关闭模式，验证模型最终输入、工具定义、请求次数、工具调用顺序、返回结果和持久化副作用保持一致。已存在的动态 ID/时间做合理归一化；不能因此忽略正文、关联 ID、事件顺序或原文件格式的变化。

| 回归范围 | 已有测试位置 | 新增断言重点 |
|---|---|---|
| 原生消息与窗口 | `tests/unit_tests/core/context_engine/test_context_engine.py`、`tests/unit_tests/core/context_engine/test_context_window_diff.py` | 默认模式无新归档文件或专用字段，窗口选择与状态恢复一致 |
| 旧卸载与摘要链 | `tests/unit_tests/core/context_engine/test_message_offloader.py`、`tests/unit_tests/core/context_engine/test_forked_message_offloader.py`、`tests/unit_tests/core/context_engine/test_dialogue_compressor.py`、`tests/unit_tests/core/context_engine/test_compression_recall.py` | 原有阈值、卸载格式、摘要/召回、失败回退预期不变 |
| Rail 默认装配 | `tests/unit_tests/harness/test_context_processor_rail.py` | preset=True 仍使用原处理链；仅新实例明确选择 preset=False，注册表和配置对象互不污染 |
| 旧模型与中断路径 | `tests/unit_tests/agent/react_agent/test_react_agent_context_config.py`、`tests/unit_tests/agent/react_agent/test_react_agent_streaming.py`、`tests/unit_tests/agent/react_agent/test_react_agent_interrupt.py` | 未启用时模型请求、流式和中断恢复保持既有语义 |
| DeepAgent 状态与输出 | `tests/unit_tests/harness/test_deep_agent_session_state.py`、`tests/unit_tests/harness/test_deep_agent_terminal_stream.py`、`tests/unit_tests/harness/test_deep_agent_stream_aclose.py` | 保存恢复、最终 answer 时机与取消清理保持不变 |

专门构造同进程混用场景：默认 Agent A 与启用新模式的 Agent B 使用不同 Session；B 发生归档失败时，只阻断 B，A 的配置、模型请求、目录、状态和错误处理均不改变。旧模式本身的文件卸载失败仍按原机制处理，不能因为新守卫而获得新的失败语义。

验收结果见下一节。旧功能回归若失败，需要区分基线已存在的环境限制与新增回归，不修改原测试预期来隐藏失败。

## 9. 实施顺序与完成条件

| 阶段 | 工作 | 完成条件 |
|---|---|---|
| P1 记录与存储 | 数据结构、原始消息采集、Session 文件存储和执行范围绑定 | 轨迹完整、ID/时间稳定、两 Session 隔离、恢复不重录 |
| P2 周期归档 | 周期包装、预算选择、严格落盘、活跃状态与请求窗口同步替换 | 最早最少完整周期被移除，并行工具不拆分，写失败保留原文 |
| P3 请求和生命周期接入 | 最终模型请求检查、DeepAgent invoke/stream 记录范围、退出导出与状态处理 | 工具循环不回到业务 ContextAssembler；失败/取消有记录，超预算不调用模型 |
| P4 兼容与交付 | 默认关闭基线对照、原功能回归、新旧实例混用、两 Session 示例及设计同步 | 新功能验收与原功能回归同时通过，默认路径无新增持久化副作用，文档与代码一致 |

agent-core 分支的完成边界为 P1～P4，全部在本仓库内实现和验收；不包含业务群聊映射、公开消息投递或其它仓库升级。现有 agent_teams 的群聊通知规则保持原语义。

### 实现与提交

- P1 已提交并推送：`359864e3`，Session 原文记录与严格 JSONL 存储。
- P2 已提交并推送：`5f753140`，完整周期归档与最小预算选择。
- P3 的 core 请求守卫已提交并推送：`3596c079`；兼容性与隔离修复为 `49489be6`。
- 中断后继续执行的周期关联补充为 `bc1f8b1d`，已提交并推送。
- P3 的 DeepAgent 外层生命周期、P4 的示例、测试和中英 API 文档均已实现。harness 特性、测试、文档按目录约定组织为三个连续提交。目标仓库关闭 Issues，用户已明确授权本次提交不关联 issue。

### 验证记录

环境：Windows、Python 3.12.12、仓库 uv 依赖；未修改依赖锁文件。无真实模型请求，模型回复使用确定性脚本，其余 ContextEngine、DeepAgent、ReAct、工具、Session、SQLite 和文件写入均运行真实代码。

- 最小持久化验收：在两个独立 Python 进程依次运行 `examples/context_engine/session_cycle_archive.py --root <新的空目录> --phase seed` 和 `--phase resume`，均输出 PASS。两 Session 隔离，完整工具原文留在 JSONL，恢复后只将裁剪状态及引用提供给模型。
- 新测试验证最早最少完整周期、并行未闭合保护、写失败保留原文、零 provider 调用、外层一次轨迹、内层多次工具/task-loop、异常与取消、流关闭、中断恢复，以及旧快照兼容。
- 最终扩大回归：已有 ContextEngine、ReActAgent 和相关 harness 状态/流式/Processor 测试连同新增测试，762 passed、1 failed。唯一失败为既有 `test_recall_rejects_chunk_symlink_escape`，Windows 缺少创建符号链接权限（WinError 1314）；在未改动基线 `7f0993dc` 复现同一失败。工具等待确认与工作流中断恢复的新用例均通过。
- 默认关闭对照：相同脚本模型、工具和两轮输入，在未改动基线与当前代码运行，归一化动态 context_message_id / 访问时间后，模型输入、工具定义、调用次数、输出及原生状态一致。
- 14 个新增 Python 文件的 Ruff、格式检查、Pylint、codespell、mypy 通过。已有接入文件仍有基线 lint/type 诊断；对照后 Ruff 为 57 对 57、mypy 为 186 对 186，归一化行号偏移后无新增诊断，不宣称历史文件已整体清零。
- 本机未安装 make，直接运行 Makefile 对应的 Python 检查器，不能声称 `make check` 命令已运行通过。未运行需要真实外部服务的全仓库系统测试或远端 CI。

### 已知边界

同 Session 单写者；不提供分布式锁。存储必须支持原子 hard-link 发布。旧来源时间保持 null。offload 与完整轨迹可重复保存同一消息，引用文件不自动回收。硬终止进程可能丢失尚未导出的本轮记录。

DeepAgent 方法自身的 stream 关闭路径已测试；原生 BaseAgent 通用回调包装器对 instance-level aclose 的既有限制未在此特性中改写。显式 Session 的 pre_run、commit/post_run 仍由宿主管理。归档保存失败后先重试导出，不覆盖未保存执行进入下一轮。
