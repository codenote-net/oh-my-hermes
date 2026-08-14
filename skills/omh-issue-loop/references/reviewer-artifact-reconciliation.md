# Reviewer Artifact Reconciliation

Use `scripts/reconcile-reviewer.py` for every Codex, Claude, PR, security, and fresh-clone behavior
review. The artifact directory must contain `metadata.json`, `baseline.json`, `stdout.log`,
`stderr.log`, and an atomically published `exit.json`.

`metadata.json` records `commandHash`, the full process identity, known descendants, target SHA,
review kind, and whether command evidence is required. `baseline.json` records the repository
HEAD, porcelain status, and common Git configuration hash before launch. `exit.json` contains
exactly the matching command hash, process identity, target SHA, integer exit status, stdout and
stderr hashes, and timestamps.

Run:

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
