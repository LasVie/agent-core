# CycleArchiveProcessor

显式启用的完整 ReAct 周期归档。超出输入预算时，从最早的可移除周期开始，归档满足预算所需的最少周期；不调用摘要模型，不截短原文，不自动回填历史。

## 配置

```python
from openjiuwen.core.context_engine import (
    ContextEngineConfig, SessionHistoryConfig, CycleArchiveProcessorConfig,
)
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail

context_config = ContextEngineConfig(
    context_window_tokens=64000,
    session_history=SessionHistoryConfig(
        enabled=True,
        root_dir="/host/agent-data",
        output_reserve_tokens=4096,
        safety_margin_tokens=256,
    ),
)
rail = ContextProcessorRail(
    preset=False,
    processors=("CycleArchiveProcessor", CycleArchiveProcessorConfig()),
)
# 通过 create_deep_agent 的现有 context_engine_config、rails 参数传入。
```

`SessionHistoryConfig.enabled` 默认 False；未配置时保留旧行为。`root_dir` 在启用时必填，由宿主提供，不来自模型参数。输出预留默认 4096、安全余量默认 256，均可设为非负整数；输出预留应覆盖实际模型的最大输出。

必须显式配置有效的 `context_window_tokens`，且大于两项预留之和。输入预算为有效上下文上限减去两项预留，上例是 59,648 tokens；若原生选中模型还有更小上限，采用更小值。统计 system、工具定义、活跃消息、附件表示及归档引用。精度取决于原生 TokenCounter；未配置可用 tokenizer 时沿用原生估算。

新模式仅允许 CycleArchiveProcessor，不与消息数上限、默认窗口裁剪、reload、compression recall 或其它 Processor 混用。已有实例不能热切换历史模式或存储根。默认 preset、旧摘要/卸载和其它 Agent 的配置不会改变。

## 执行边界

一次外层 `DeepAgent.invoke` / `stream` 绑定一个 execution_id。内部 ReAct 的 assistant tool call 与 tool message 正常 append；每次模型请求使用原生 ContextEngine 的窗口处理，不调用业务 ContextAssembler。轮末导出完整轨迹，再更新原生 Session 状态；流式最终 answer 在导出成功之后发送。异常、取消、中断和 DeepAgent 流关闭同样导出已收集的消息。

显式传入的 Session 由宿主调用 `pre_run` 恢复、`commit` / `post_run` 持久提交。预留业务 load/prepare/save 可包装这些操作，每次 outer invoke 各执行一次。业务 SessionManager 和 Agent × 群映射不在本 SDK 内实现。

直接使用 ContextEngine / ReActAgent 的宿主可调用：

```python
engine.begin_history_execution(session, execution_id=None)
# create_context(..., processors=[("CycleArchiveProcessor", config)])
# 普通消息追加、ReAct invoke / 工具执行
execution = await engine.finish_history_execution(session, status="completed")
await session.commit()
```

`begin_history_execution` 默认生成执行 ID，拒绝同一 Session 重叠执行。`finish_history_execution` 接受 completed/interrupted/failed，返回 `ExecutionRecord(execution_id, status, trajectory)`；未启用时两者返回 None。DeepAgent 已负责调用它们，宿主不要重复绑定。

## 归档与恢复

- 完整周期包含一条 assistant 消息及全部对应工具结果；并行工具必须全部闭合。不含工具调用的 assistant 也是一个完整周期。
- 保护当前执行的所有用户指令（含 steering）、最新完整周期和未闭合工具调用。
- 先发布 JSONL 文件，再同时替换 ModelContext 活跃消息和本次请求窗口。写失败保留活跃原文。
- 最新完整周期中的超大工具结果可整条落盘，正文换成文件引用，保留 tool_call_id；未闭合周期不作此替换。受保护内容仍放不下则报错。
- 消息在 add Processor 之前冻结；时间是接收/产生时间，message_id 复用 metadata.context_message_id。跨轮归档不改原 execution_id 或时间；旧 checkpoint 的未知发生时间为 null。
- load 只读原生 checkpoint。恢复截断后的 messages、原文来源记录及引用，不扫描历史目录，也不把归档正文重新装入模型窗口。

```text
root_dir/
  sessions/<sha256(session_id)>/history/
    offload/<content_sha256>.jsonl
    trajectories/<sha256(execution_id)>.jsonl
宿主管理的原生 checkpoint：示例使用 root_dir/state.db
```

对 ID 做 SHA-256 编码用于路径安全，JSONL 内仍保留原 session_id / execution_id。每行包括 session_id、execution_id、step_id、seq、message_id、occurred_at、archived_at、完整 message。step_id 标识 ReAct 周期，seq 是执行内顺序。文件无摘要或正文预览。

`ArchiveRef` 提供 path、archive_id、message_ids 和首尾发生时间。offload 与 trajectories 允许重复保存同一原文，以 `(session_id, message_id)` 识别；grep 不自动去重。引用链不自动删除。按路径查询复用现有 grep/read_file，工具授权仍由宿主管理；存储路径隔离不等于工具权限隔离。

## 错误与限制

- `CONTEXT_ARCHIVE_EXECUTION_ERROR` / 153004：ARCHIVE_FAILED。文件写入、校验或发布失败，不回退内存。
- `CONTEXT_BUDGET_EXECUTION_ERROR` / 153005：CONTEXT_BUDGET_EXCEEDED。最后的模型请求守卫在所有 window mutator 之后执行；上述两种错误不进入模型重试或压缩兜底，也不能被异常 Rail 变成成功回复。
- 轨迹保存失败和执行异常同时发生时，原执行异常继续抛出，保存错误保留在异常链和 note 中。保存失败后记录器保留当前执行供宿主重试导出；不要直接开始下一轮覆盖它。
- 要求同一 Session 串行写入、可持久化的宿主目录，以及支持同目录临时文件、fsync 和原子 hard-link 发布的文件系统。不提供分布式锁或远程存储平台。
- 不保证进程被强杀前尚未导出的轨迹已经落盘。原生 BaseAgent 通用回调包装器的 instance-level aclose 限制沿用原行为；DeepAgent 方法自身的关闭路径已验证。

## 最小持久化 Agent 验收

`examples/context_engine/session_cycle_archive.py` 使用真实 DeepAgent/ReAct、真实工具、严格 JSONL 与原生 SQLite checkpoint；仅模型回复为离线脚本，无需模型凭证。对一个新的空目录，在两个进程依次执行：

```sh
python examples/context_engine/session_cycle_archive.py --root /tmp/agent-demo --phase seed
python examples/context_engine/session_cycle_archive.py --root /tmp/agent-demo --phase resume
```

两个 Session 都应输出 PASS；恢复后的模型输入包含对应 Session 的记忆和文件引用，不包含另一 Session 的内容或重新加载的完整工具原文。
