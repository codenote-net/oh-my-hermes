# Durable Worker Protocol

This is the canonical protocol-v2 contract. `scripts/worker_protocol.py` owns field names and
validation. All persisted fields use camelCase. Snake-case or partially migrated records are
legacy evidence and can never be confirmed as protocol v2.

## Official preparation sequence

The orchestrator performs the only `gh issue view`, saves its JSON outside the repository, and
then runs:

```bash
python3 scripts/init-run-state.py --run-dir "$run_dir" --repository "$repo" \
  --repository-identity owner/repo --issue-url "$issue_url" \
  --issue-snapshot "$snapshot_file" --signoff-required false
```

Before each worker, the orchestrator separately captures authenticated GitHub evidence. The
baseline helper never uses GitHub and never fetches the issue:

```bash
git ls-remote --heads origin "$branch"
gh pr list --repo owner/repo --head "$branch" --state all --json number,url,state,isDraft,title,body,headRefOid
python3 scripts/capture-worker-baseline.py --run-dir "$run_dir" --repository "$repo" \
  --remote-branch-oid absent --pull-requests-json "$pr_evidence"
python3 scripts/validate-run-state.py pre-launch --run-dir "$run_dir" --repository "$repo"
```

`run-worker.py` repeats the shared pre-launch validation while holding the run launch lock. Any
missing, unknown, mistyped, or inconsistent field causes a structured rejection with
`workerSpawned=false`; `subprocess.Popen` is not reached.

## Canonical `state.json`

The initializer owns identity, snapshot, repository, initial validation/review, fix-count, and
signoff fields. The runner owns worker command/process/lifecycle-related fields. The reconciler
owns `completionMode`, `workerExitStatus`, and `reconciliationResult`. The orchestrator owns later
phase, validation, review, and PR updates. Every update increments `stateGeneration`.

<!-- BEGIN CANONICAL STATE -->
```json
{
  "baseSha": "1111111111111111111111111111111111111111",
  "branch": "omh/issue-1-example",
  "completionMode": null,
  "currentHead": "1111111111111111111111111111111111111111",
  "currentPhase": "worker_baseline_captured",
  "expectedWorkingTreeFingerprint": "2222222222222222222222222222222222222222222222222222222222222222",
  "fixCount": 0,
  "issueSnapshotHash": "3333333333333333333333333333333333333333333333333333333333333333",
  "issueUrl": "https://github.com/example/example/issues/1",
  "knownDescendantIdentities": [],
  "latestStateUpdateTime": "2026-01-01T00:00:00+00:00",
  "prUrl": null,
  "protocolVersion": 2,
  "reconciliationResult": null,
  "repositoryIdentity": "example/example",
  "repositoryRoot": "/workspace/example",
  "reviewTargetSha": null,
  "runId": "run-example",
  "schemaVersion": 1,
  "signoffRequired": false,
  "stateGeneration": 2,
  "validationPlan": [],
  "validationResults": [],
  "workerCommandHash": null,
  "workerExitStatus": "unknown",
  "workerPid": null,
  "workerProcessIdentity": null,
  "workerStartTime": null
}
```
<!-- END CANONICAL STATE -->

## Canonical `worker-baseline.json`

<!-- BEGIN CANONICAL BASELINE -->
```json
{
  "baseSha": "1111111111111111111111111111111111111111",
  "branch": "omh/issue-1-example",
  "capturedAt": "2026-01-01T00:00:00+00:00",
  "commitRange": [],
  "head": "1111111111111111111111111111111111111111",
  "issueSnapshotHash": "3333333333333333333333333333333333333333333333333333333333333333",
  "protocolVersion": 2,
  "pullRequests": [],
  "reflog": [],
  "remoteBranchOid": null,
  "repositoryIdentity": "example/example",
  "repositoryRoot": "/workspace/example",
  "runId": "run-example",
  "schemaVersion": 1,
  "stagedDiffSha256": "4444444444444444444444444444444444444444444444444444444444444444",
  "statusShort": "## omh/issue-1-example\n",
  "unstagedDiffSha256": "5555555555555555555555555555555555555555555555555555555555555555",
  "untrackedFiles": []
}
```
<!-- END CANONICAL BASELINE -->

## Durable lifecycle and artifact failure

`worker-lifecycle.json` advances through `spawn_attempted`, `spawned`, `running`, `exited`,
`output_fsync_done`, and `artifact_published`. Failures are `preflight_failed`, `spawn_failed`, or
`artifact_publish_failed`. Each record says independently whether spawn was attempted, the worker
was spawned, and the exit artifact was published.

The runner keeps the validated run ID, command hash, and launch generation in local immutable
values. It never rereads run ID while publishing the exit artifact. A generation mismatch or
external state mutation is an orchestration failure.

If artifact publication fails after spawn, preserve lifecycle, output, process identity, and
working tree. Reconcile process death and quiescence, but do not classify the run as confirmed.
Only explicit legacy salvage with complete independent evidence can proceed; otherwise stop.

Legacy state is never silently migrated. A Human-authorized migration must create a new canonical
run and retain the old directory as evidence. A legacy record without the canonical schema,
baseline, lifecycle identity, and required salvage evidence remains `indeterminate`.
