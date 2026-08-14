# Reviewer Artifact Reconciliation

Launch every Codex, Claude, PR, security, and fresh-clone behavior review through
`scripts/run-reviewer.py`, then reconcile it with `scripts/reconcile-reviewer.py`. The artifact
directory must be absent or empty before launch. The runner writes `metadata.json`,
`baseline.json`, `operation-state.json`, `stdout.log`, `stderr.log`, and an atomically published
`exit.json`.

`metadata.json` records `commandHash`, the full process identity, known descendants, target SHA,
review kind, and whether command evidence is required. `baseline.json` records the repository
HEAD, porcelain status, and common Git configuration hash before launch. `exit.json` contains
exactly the matching command hash, process identity, target SHA, integer exit status, stdout and
stderr hashes, and timestamps.

Launch the reviewer as a foreground child of the durable wrapper:

```bash
python3 scripts/run-reviewer.py \
  --artifact-dir <review-artifact-dir> \
  --repository <standalone-clone> \
  --target-sha <sha> \
  --review-kind <kind> \
  --attempt-id <unique-attempt-id> \
  --deadline <iso-8601-deadline> \
  --resume-after-completion '<exact next action>' \
  --require-command-evidence \
  -- <reviewer-command> <arguments...>
```

Keep the wrapper itself in the single Hermes background task. Do not background the reviewer
command inside the wrapper, call `setsid`, or request a new session that escapes the wrapper's
supervised process group. Omit `--require-command-evidence` unless the review is behavior
verification. Treat evidence of a process-group escape as indeterminate and fail closed.

The runner transitions `operation-state.json` through `reviewer_launch_pending`,
`reviewer_running`, and `reviewer_artifact_published`. The reconciler writes
`reviewer_reconciled` after accepting the artifact and records the recovery reason and timestamps
when an artifact remained unprocessed for more than five minutes. The orchestrator writes
`review_gate_complete` to the main run state only after all five exact-SHA sources pass.

After the wrapper exits or its completion notification is lost, run:

```bash
python3 scripts/reconcile-reviewer.py \
  --artifact-dir <review-artifact-dir> \
  --repository <standalone-clone> \
  --expected-head <sha> \
  --require-command-evidence \
  --timeout 600
```

The reconciler observes logs and repository state twice. It emits `confirmed`, `running`,
`incomplete_report`, `side_effect_detected`, `stale_target`, `invalid`, or `indeterminate`.
`confirmed` requires exit zero, matching hashes and SHA, stopped process tree (a zombie launcher
does not block when descendants are stopped), stable output/repository state, unchanged HEAD,
status and common Git configuration, substantive nonblank output, and an explicit high-priority
count. Behavior verification additionally requires exact commands and numeric statuses.
