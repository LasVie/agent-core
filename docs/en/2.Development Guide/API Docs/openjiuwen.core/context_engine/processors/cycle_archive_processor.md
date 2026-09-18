# CycleArchiveProcessor

An opt-in processor that archives the minimum oldest complete ReAct cycles needed
to fit the input budget. It preserves full originals: no summary model, character
truncation, TTL or automatic rehydration.

## Configuration

```python
from openjiuwen.core.context_engine import (
    ContextEngineConfig, SessionHistoryConfig, CycleArchiveProcessorConfig,
)
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail

context_config = ContextEngineConfig(
    context_window_tokens=64000,
    session_history=SessionHistoryConfig(
        enabled=True, root_dir="/host/agent-data",
        output_reserve_tokens=4096, safety_margin_tokens=256,
    ),
)
rail = ContextProcessorRail(
    preset=False, processors=("CycleArchiveProcessor", CycleArchiveProcessorConfig()),
)
# Pass context_config and rail through the existing create_deep_agent configuration.
```

History is disabled by default. The host supplies root_dir; the model cannot choose
it. Explicit context_window_tokens must exceed the nonnegative reserves (defaults:
4096 output, 256 safety). Reserve at least the model's maximum output. The example
input budget is 59,648 tokens; a smaller native selected-model limit wins. Counts
include system messages, tool schemas, active messages, attachment representations
and references, with the native TokenCounter's accuracy or fallback estimate.

Only CycleArchiveProcessor is accepted in this mode. Message-count slicing, default
window slicing, reload, compression recall and other processors conflict. History
mode/storage cannot be hot-switched on an existing engine. Old defaults and other
Agent instances retain their behavior.

## Execution and persistence

DeepAgent binds one execution_id around each outer invoke/stream. Assistant tool
calls and tool results follow the native append loop. Native per-request window
processing is distinct from business ContextAssembler load/prepare/save, which run
once per outer execution. A complete raw trajectory is exported at normal exit,
interrupt, failure, cancellation or DeepAgent method-level stream close, before the
active state is saved and a successful final answer is emitted.

Caller-owned Sessions still require host pre_run and commit/post_run. Direct
ContextEngine/ReActAgent hosts can use these additive methods (DeepAgent already
calls them, so do not bind twice):

```python
engine.begin_history_execution(session, execution_id=None)
# create_context(..., processors=[("CycleArchiveProcessor", config)]) and normal execution
execution = await engine.finish_history_execution(session, status="completed")
await session.commit()
```

begin_history_execution generates an ID by default and rejects overlapping writers.
finish_history_execution accepts completed/interrupted/failed and returns an
ExecutionRecord(execution_id, status, trajectory). Both return None when disabled.
Business Agent/group routing and SessionManager remain the host's responsibility.

## Archive contract

A cycle consists of one assistant message and all matching tool results (including
parallel calls), or one assistant without tools. Protect current-execution user
instructions, steering, the latest complete cycle and incomplete calls. Publish all
files before updating active state and the outbound window. A huge result in a
complete protected cycle may be stored whole and replaced by a paired file reference;
incomplete cycles are never offloaded. If protected input still exceeds the budget,
stop before the provider request.

```text
root_dir/sessions/<sha256(session_id)>/history/
  offload/<content_sha256>.jsonl
  trajectories/<sha256(execution_id)>.jsonl
Native checkpoint location is host-owned (the example uses root_dir/state.db).
```

Each UTF-8 JSONL record contains session_id, execution_id, step_id, seq, message_id,
occurred_at, archived_at and the full message. IDs are hashed only for safe paths.
step_id identifies a ReAct cycle; seq orders messages within an execution. Capture
precedes add processors. message_id reuses metadata.context_message_id. Restoration
does not re-record old messages or fabricate legacy occurrence times (null).
ArchiveRef contains the file path/ID, message IDs and first/last occurrence times.

Restore native active messages, provenance and references only; do not scan logs or
reload removed originals into the model window. Offload and trajectory copies may
overlap; identity is (session_id, message_id). grep does not deduplicate. Referenced
archives are retained. Use existing grep/read_file with host-authorized paths; storage
isolation does not grant file-tool access permissions.

## Failure semantics and limits

- CONTEXT_ARCHIVE_EXECUTION_ERROR (153004): ARCHIVE_FAILED; strict publication or
  validation failure, with no memory fallback and no removal of live originals.
- CONTEXT_BUDGET_EXECUTION_ERROR (153005): CONTEXT_BUDGET_EXCEEDED; the final guard
  follows all window mutators and precedes invoke/stream provider calls. Neither
  error can be hidden by model retries, compression recovery or force-finish rails.
- If execution and trajectory save both fail, keep the execution error with the save
  error as its cause/note. A failed export retains its execution for a host retry;
  do not start a new execution over it.
- One writer per Session. The filesystem must support same-directory temporary
  files, fsync and atomic hard-link publication. No distributed locks or remote
  storage platform are added. Abrupt process termination can lose unexported data.
- The pre-existing generic BaseAgent instance-level aclose limitation is unchanged;
  DeepAgent method-level stream close is tested.

## Offline acceptance example

Run these in separate processes against a fresh directory:

```sh
python examples/context_engine/session_cycle_archive.py --root /tmp/agent-demo --phase seed
python examples/context_engine/session_cycle_archive.py --root /tmp/agent-demo --phase resume
```

The example uses real DeepAgent/ReAct, real tools, JSONL and native SQLite persistence;
only provider responses are scripted. Both Sessions must restore their own memory
and references without another Session's content or rehydrated full tool output.
