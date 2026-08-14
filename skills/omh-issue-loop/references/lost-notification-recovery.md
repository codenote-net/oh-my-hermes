# Lost Completion Notification Recovery

A background completion notification is a reconciliation hint, not proof of completion and not
the only condition that permits continuation. Durable artifacts are authoritative.

## Mandatory recovery sweep

At the beginning of every orchestrator turn, before replying or launching work:

1. Load `state.json`, the task list, immutable issue snapshot metadata, repository HEAD/status,
   and PR metadata when present.
2. Find every phase recorded as running, waiting, pending completion, or pending reconciliation.
3. Inspect its recorded PID, start token, command, known descendants, lifecycle record, logs,
   exit artifact, hashes, target SHA, and current process table.
4. Observe process state, output hashes, and the relevant tree fingerprint twice across the normal
   quiescence interval.
5. If the process tree is stopped and a valid exit artifact exists, reconcile it immediately.
   Never wait for a missing Hermes notification.
6. If `finishedAt` is more than five minutes old and state has not advanced beyond that phase,
   atomically record `recoveryReason: lost_or_unprocessed_completion_notification`,
   `artifactFinishedAt`, `recoveredAt`, and `resumedFromPhase`, then resume at the next incomplete
   phase without rerunning the operation.
7. Treat a zombie launcher as stopped when every recorded descendant is stopped and the artifact,
   side-effect, and quiescence checks pass. Do not wait indefinitely for it to be reaped.
8. If the process is dead and the required artifact is absent or invalid, apply the documented
   indeterminate or salvage rules. Never silently repeat a mutation.
9. Preserve an artifact whose target SHA differs from the current expected SHA as stale evidence,
   reject it, and rerun the required gate for the current SHA.

If the process is alive and no artifact exists, retain the running phase and prohibit a duplicate
launch. A hash mismatch is invalid and fail-closed. A stopped process without an artifact is
indeterminate unless an existing salvage protocol explicitly accepts it.

## Launch-time continuation record

Before every bounded background launch, atomically record the phase and attempt ID, exact command
and hash, target HEAD or PR head, artifact paths and schema, deadline, full process identity after
spawn, known descendants, and the exact `resumeAfterCompletion` action. Use one durable wrapper
that remains alive until its child exits and atomically publishes lifecycle, stdout, stderr, and
an exit artifact with numeric status, hashes, `startedAt`, and `finishedAt`, including filesystem
syncs before notification.

## Continuation invariant

After accepting an artifact, execute every currently unblocked phase in the same turn. A status
request is not a stop condition: sweep, continue unblocked work, then report the resulting state.
Stop only after durably launching another genuinely long operation, when Human input is required,
at a documented fail-closed condition, or after the verified checklist is complete.

## PR-phase recovery

On re-entry to a run with a PR, read the current PR head and reconcile all five review sources for
that exact SHA. Require numeric exits, substantive reports, side-effect checks, and verifier
command evidence. If all five are complete with zero high-priority findings, immediately update
the PR body, mark it ready, perform the preliminary Human handoff, and inspect CI. If CI is already
terminal green for the reviewed SHA, verify the complete check set and finish the green-CI handoff
without launching another poller. Never rerun a review merely because its notification was lost.

If a behavior verifier exited zero but lacks commands or numeric statuses, mark only that artifact
`incomplete_report` and rerun that verifier once with a narrower evidence contract in a new
standalone clone.
