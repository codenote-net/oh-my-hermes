# Durable Runs, Resume, and Legacy Salvage

Use this reference during preflight, any explicit “resume” request, and recovery from a worker
without a protocol-v2 exit artifact.

## Durable state

Resolve `$HERMES_HOME` from the Hermes runtime; never hardcode `~/.hermes`. Create a unique,
owner-only directory:

```text
$HERMES_HOME/runs/ht-issue-loop/<owner>-<repo>-<issue>-<run-id>/
├── state.json
├── issue-snapshot.json
├── worker-baseline.json
├── worker-lifecycle.json
├── worker-launch.lock
├── worker-output.log
├── worker-exit.json
├── salvage-evidence.json
└── review-artifacts/
```

Keep run artifacts outside the repository. Write every JSON state change to a same-directory
temporary file, flush and `fsync`, atomically rename it, and `fsync` the directory.

`state.json` must contain:

- protocol version and unique run ID;
- issue URL and immutable snapshot path plus SHA-256;
- repository identity and absolute repository root;
- branch, base SHA, current HEAD, and expected working-tree fingerprint;
- current phase and latest state update time;
- worker PID, start time, command hash, full process identity, and known descendant identities;
- completion mode and worker exit status;
- validation plan and results, shared fix count, and review target SHA;
- PR URL or number after creation.

Persist state before and after every phase transition. Never rely only on conversation context or
temporary runner metadata.

Do not hand-author these files. Follow [durable-worker-protocol.md](durable-worker-protocol.md),
initialize with `init-run-state.py`, and capture each baseline with
`capture-worker-baseline.py`. Baseline capture consumes separately saved GitHub evidence and never
accesses or refetches the issue.

## Select preflight mode

Use `scripts/validate-run-state.py new`, `resume`, or `pre-launch` for the local branch, HEAD, snapshot, and
working-tree checks below. Remote-branch, PR, and GitHub identity comparisons remain orchestrator
checks because they require authenticated `gh` access.

Immediately before every worker, `pre-launch` validates the complete state and baseline schemas,
snapshot hash, repository root, branch, HEAD, tree fingerprint, and absence of a live/conflicting
worker or artifact. The runner repeats it under the launch lock. Failure records
`preflight_failed` with `workerSpawned=false`.

### New run

Use this mode unless the Human explicitly asks to resume a saved run.

1. Require a clean working tree.
2. Fetch the issue exactly once and atomically save the immutable snapshot and hash.
3. Fetch the latest default branch and create a new issue branch from its remote tip.
4. Stop if the local or remote branch already exists; do not reset or reuse it.
5. Save the repository, branch, base SHA, issue snapshot, baseline, validation plan, and initial
   phase before launching a worker.

### Resume run

Use only a specifically selected durable run directory. Do not search for or guess a “close
enough” run.

1. Load `state.json` and `issue-snapshot.json`; do not run `gh issue view` or otherwise refetch the
   issue.
2. Verify protocol/run ID, issue URL, snapshot hash, repository identity/root, base SHA, branch,
   HEAD, PR identity when present, fix count, and last completed phase.
3. Allow the saved branch to exist. Allow a dirty tree only when its complete fingerprint matches
   the saved worker result/fingerprint. The new-run branch-exists and clean-tree rejection rules
   do not apply to this exact matching resume.
4. Stop if state, snapshot, repository, branch, HEAD, PR, or working tree does not match. Never
   stash, reset, clean, or absorb differences.
5. If a worker phase is incomplete, reconcile it before any post-worker check. Otherwise continue
   from the last atomically completed phase; do not repeat issue fetching or already completed
   Git/GitHub mutations.

An abandoned branch without matching durable state is not resumable. Existing cleanup remains
explicitly Human-authorized.

## Legacy salvage without an exit artifact

Absence of `worker-exit.json` is never success. It may become `salvageable` only when every gate
below is evidenced:

A lifecycle showing `artifact_publish_failed` proves the worker started but not that its exit
artifact was published. Preserve it and apply these fail-closed gates. `preflight_failed` instead
proves no worker started and is a rejected launch, not salvage. Snake-case, partial, or
noncanonical records are legacy and are never silently upgraded or confirmed.

1. Full worker identity and every known descendant are confirmed stopped. Missing process identity
   is `indeterminate`; PID alone is insufficient.
2. The immutable issue snapshot and run identity are present and match.
3. A pre-worker baseline exists and can be compared for HEAD, commit range, reflog, remote branch,
   complete PR state, status, staged/unstaged changes, and untracked files.
4. Comparison proves no prohibited commit, reset, push, PR/issue mutation, CI monitoring, review,
   or out-of-scope change.
5. Output and working-tree fingerprints are stable across the reconciliation observation window.
6. Worker output semantically contains changed files, exact validation commands, each result and
   exit status, and blockers/concerns.
7. The diff is issue-scoped.
8. The orchestrator reconstructs validation from repository instructions and scope, independently
   runs it, and records commands plus exit codes in `salvage-evidence.json`. At minimum run:
   - `git diff --check`;
   - affected tests, lint, typecheck, and build;
   - repository-required final validation;
   - applicable documentation validation;
   - an applicable security review for security-sensitive changes.

Do not trust the worker’s validation claim as salvage evidence. Pass the completed evidence file
to `reconcile-worker.py --salvage-evidence <path>`.

If the Human explicitly said “resume”, continue after all gates pass. Otherwise show the salvage
evidence and ask before continuing. Record:

```text
completion_mode: salvaged_without_exit_artifact
worker_exit_status: unknown
```

After authorized salvage, enter the normal independent review loop. If independent validation
fails, salvage has not succeeded. When a fix is appropriate, invoke the normal restricted fix
worker with protocol v2; never alter the legacy output to manufacture a successful exit.

## Fail-closed stop conditions

Stop as `indeterminate` when process death, identity, baseline, snapshot/run identity, side-effect
comparison, stable output/tree, report completeness, scope, or independent validation cannot be
established. Do not proceed to reviews, commits, pushes, PR/issue changes, CI, or ready state.

If a worker might still run, say the working tree may continue changing. Do not report `no
changes`, a definitive changed-file list, or a settled validation result.
