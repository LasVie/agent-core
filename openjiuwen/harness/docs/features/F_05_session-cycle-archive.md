# F_05 Session cycle archive

## Metadata

| Item | Value |
| --- | --- |
| Date | 2026-09-18 |
| Scope | Opt-in ContextEngine history and DeepAgent outer execution lifecycle |
| Specs | S_02, S_04 |
| Refs | Repository Issues disabled; user explicitly authorized this delivery without an issue association |

## Background

A persistent Agent needs original, searchable logs without an LLM summary. Tool
loops must keep native append semantics; business load/prepare/save run once per
outer execution. Existing processors fail open and may fall back to RAM, so they
cannot supply the strict write-before-remove contract.

## State and decisions

- One Session recorder binds one outer execution. Raw messages are captured before
  add processors. Native message IDs and original timestamps survive restoration.
- Only the explicit history configuration plus CycleArchiveProcessor is accepted.
  The preset, TTL, summary processors and default checkpoint schema are unchanged.
- Archive oldest complete ReAct cycles until the final input budget fits. Protect
  current user instructions, the latest complete cycle and incomplete calls.
  A huge tool result may be saved whole and replaced by a paired file reference.
- Strict JSONL files are published before changing live context. A failed write
  leaves live originals intact. Complete trajectories are exported at outer exit.
- A guard after window mutators prevents invoke/stream provider calls on failure.
  The core rail decorator bypasses retry/force-finish for the two new error codes;
  this narrow additional hook is necessary because the generic decorator otherwise
  permits exception rails to swallow the final guard. Old errors keep their policy.
- Caller-owned Sessions still require host commit. Restore reads native state,
  never scans logs to reconstruct the window. No new model-facing tools or events.

## Rejected alternatives

- Reassembling context at each tool call: duplicates the native ReAct loop.
- Summary, head/tail truncation, automatic reload: contradict lossless archival.
- Reconstructing a trajectory from the final window: loses removed messages.
- Making all processor failures fatal: changes existing core behavior.

## Verification

Targeted tests cover complete parallel cycles, protected input, write failure,
final attachment overflow, invoke/stream admission, Session isolation and restart.
HITL and workflow interruption/resume both export their outer executions; pending
tool results retain their originating cycle when a cached context resumes. The
native workflow replay creates its own assistant message and cycle as before.
The final regression run has 762 passing tests and one pre-existing Windows
symlink-privilege failure reproduced on the unchanged baseline. Separate-process
SQLite restart acceptance passes. Full results and limits are recorded in
docs/dev/session-cycle-archive-plan.md.

## Known boundaries

One writer per Session; filesystem hard-link atomic publication is required.
Occurrence times from legacy checkpoints are unknown, not fabricated. Offload
and trajectory files can duplicate an original message; deduplicate by Session
and message ID when needed. Referenced archives are not automatically deleted.
An abrupt process kill may lose unexported current-execution records.
