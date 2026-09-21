# F_06 Session history recovery

## Metadata

| Item | Value |
| --- | --- |
| Date | 2026-09-21 |
| Scope | Lossless history commit ownership, aborted calls and archive recovery |
| Specs | S_02, S_04 |
| Refs | Continuation of F_05; repository Issues disabled |

## Background

Review found that single-round streams committed caller-owned Sessions before
outer trajectory export, aborted tool calls reached the next request without
results, and workflow replay IDs disabled archival of the entire context.

## Decisions

- In history mode, inner ReAct never commits the Session. Export precedes the
  owner's commit, including streamed execution and failed export.
- An aborted execution retains original messages and appends explicit aborted
  results only for outstanding calls. Recoverable HITL/workflow interrupts retain
  their normal resume path without synthetic completion.
- Validate tool pairing per native cycle occurrence, including adjacent excess
  results. Repeated IDs in different cycles are permitted; malformed cycles stay
  in context without disabling unrelated complete cycles. Protect the latest
  complete range directly, without reconnecting IDs to older occurrences.
- Export retry uses the same engine and Session without running tools again.
  Freeze terminal status at the first finish attempt and return the same record
  on repeated finish. A host keeps its lock and any buffered business result
  until export and checkpoint commit succeed.

## Rejected alternatives

- Deleting incomplete originals: loses the evidence needed to diagnose an abort.
- Enabling the old Rail rewrite: also rewrites original arguments and messages.
- Requiring Session-wide unique tool IDs: native workflow replay can reuse IDs.
- Re-running invoke after storage failure: can repeat completed tool side effects.
- Changing old-mode commit or repair behavior: exceeds this opt-in repair scope.

## Verification

Regression coverage includes real SQLite restoration after failed stream export,
host commit ordering, cancellation/failure followed by another request, partial
parallel results, native workflow resume followed by budget-triggered archival,
and export retry without changing execution identity or status.

The selected ContextEngine/ReAct and harness regression suite passes 774 tests.
One previously verified Windows symlink-privilege test is deselected. A short,
fresh pytest temporary directory avoids Windows path-length failures in legacy
recall tests. Small changed files pass Ruff, formatting and source Pylint; codespell
passes. Targeted mypy with --follow-imports=skip passes for the five small changed
files. ReAct retains the same pre-existing summary redefinition diagnostic;
unrestricted import-graph type checking was not completed. Existing ReAct lint
diagnostics remain unchanged apart from its line count.
Regression scope, baseline comparisons and limits are recorded in
docs/dev/session-cycle-archive-plan.md.

## Known boundaries

Recovery of a failed export requires retaining the live Agent and Session; this
does not add crash recovery of unexported executions. Host commit retries and
delivery retries remain host responsibilities. Generic streaming-wrapper close
limitations and filesystem requirements from F_05 remain unchanged.
