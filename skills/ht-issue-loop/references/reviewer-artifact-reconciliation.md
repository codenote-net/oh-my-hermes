# Reviewer Artifact Reconciliation

Launch every Codex, Claude, PR, security, and fresh-clone behavior review through
`scripts/run-reviewer.py`, then reconcile it with `scripts/reconcile-reviewer.py`. The artifact
directory must be absent or empty before launch. The runner writes `metadata.json`,
`baseline.json`, `operation-state.json`, `stdout.log`, `stderr.log`, and an atomically published
`exit.json`.

`metadata.json` records `commandHash`, the full process identity, known descendants, target SHA,
review kind, baseline SHA-256, and whether command evidence is required. `baseline.json` records
the verified normalization evidence and normalized repository state before launch. `exit.json`
contains exactly the matching command hash, process identity, target SHA, baseline SHA-256, integer
exit status, stdout and stderr hashes, and timestamps. Reconciliation hashes `baseline.json` again
and rejects any mismatch before trusting its content. It reads the baseline only after process-tree
quiescence and computes the hash and JSON object from the same in-memory bytes. Protocol v2 requires
the complete normalization artifact path, normalization hash, round ID, and repository state.

## Primary-worktree normalization before local review

After worker completion reconciliation, the worker side-effect check, and exact issue-scope
validation—but before any reviewer baseline—write the complete issue-scoped path allowlist as JSON
and run:

```bash
python3 scripts/normalize-review-candidate.py \
  --run-dir <durable-run-dir> \
  --repository <primary-worktree> \
  --allowed-paths-json <complete-exact-path-allowlist.json> \
  --round-id <unique-review-round-id>
```

The helper enumerates porcelain-v1 untracked files exactly, includes already-existing
intent-to-add entries in the candidate, and fails without running `git add` when any path is
unknown or absent from the allowlist. It invokes only `git add -N -- <validated-paths>` as an
argument array. Broad staging, shell-concatenated path strings, independent sandboxes, repository
copies, and private Git indexes are prohibited.

Before normalization it durably captures the immutable issue snapshot hash, HEAD, branch, all Git
refs and reflogs (including the branch reflog), complete porcelain status, staged and unstaged diff
hashes, complete untracked file list with SHA-256 and modes, complete allowlist, full index entries,
working-tree file hashes and modes, Git configuration hash, remote branch OID, and the complete
branch PR snapshot. After
normalization it captures the same state and requires all non-index content and external state to
remain identical, status outside the new-path set to remain identical, every old index entry to
remain identical, and the staged diff to remain identical. Each candidate must be a stage-zero
` A` entry with empty blob OID `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`.

The helper publishes one unique artifact per round with an atomic write, file flush and `fsync`,
atomic rename, and parent-directory `fsync`; it never overwrites prior evidence. Preserve its path
and reported SHA-256. An exclusive durable round claim prevents concurrent or repeated invocation
from replacing evidence. The required empty-blob OID is SHA-1-specific, so another repository
object format is rejected before intent-to-add changes the index. A failed normalization, missing
artifact, hash mismatch, changed normalized state, or reused round identifier blocks every
reviewer. Later fix workers require a new scope validation and artifact; newly untracked files are
normalized then, while existing intent-to-add files are content-hashed and revalidated without
staging real content.

Pass the same normalization evidence to Codex local review, Claude local code review, and Claude
local security review in the round. `run-reviewer.py` accepts it only after verifying the artifact
hash and current repository state, then captures the reviewer baseline from the artifact's
normalized `after` state. Artifact verification reads one byte sequence once, checks its SHA-256,
and parses that same sequence; it never hashes one file version and parses another.

## Durable reviewer launch

Launch the reviewer as a foreground child of the durable wrapper:

```bash
python3 scripts/run-reviewer.py \
  --artifact-dir <review-artifact-dir> \
  --repository <primary-worktree-or-standalone-clone> \
  --target-sha <sha> \
  --review-kind <kind> \
  --attempt-id <unique-attempt-id> \
  --deadline <iso-8601-deadline> \
  --resume-after-completion '<exact next action>' \
  --normalization-artifact <normalization-artifact.json> \
  --normalization-sha256 <artifact-sha256> \
  --round-id <same-review-round-id> \
  --require-command-evidence \
  -- <reviewer-command> <arguments...>
```

Keep the wrapper itself in the single Hermes background task. Do not background the reviewer
command inside the wrapper, call `setsid`, or request a new session that escapes the wrapper's
supervised process group. Omit `--require-command-evidence` unless the review is behavior
verification. After the blocked child identity and metadata are durably published, the runner
strictly revalidates the normalization artifact and current repository once more immediately before
releasing the child. Treat validation failure or evidence of a process-group escape as
indeterminate and fail closed without executing the reviewer command.

The runner transitions `operation-state.json` through `reviewer_launch_pending`,
`reviewer_running`, and `reviewer_artifact_published`. The reconciler writes
`reviewer_reconciled` after accepting the artifact and records the recovery reason and timestamps
when an artifact remained unprocessed for more than five minutes. The orchestrator writes
`review_gate_complete` to the main run state only after all five exact-SHA sources pass.

After the wrapper exits or its completion notification is lost, run:

```bash
python3 scripts/reconcile-reviewer.py \
  --artifact-dir <review-artifact-dir> \
  --repository <primary-worktree-or-standalone-clone> \
  --expected-head <sha> \
  --require-command-evidence \
  --timeout 600
```

The reconciler observes logs and repository state twice. It emits `confirmed`, `running`,
`incomplete_report`, `side_effect_detected`, `stale_target`, `invalid`, or `indeterminate`.
`confirmed` requires exit zero, matching hashes and SHA, stopped process tree (a zombie launcher
does not block when descendants are stopped), stable output/repository state, unchanged HEAD,
status and Git configuration, unchanged branch, refs, and reflogs, identical full index and working-tree
content/modes, unchanged remote branch OID and PR snapshot, intact immutable issue snapshot and
normalization evidence, substantive nonblank output, and an explicit high-priority count.

A reviewer repeating `git add -N` on the same normalized paths is idempotent and may reconcile as
`confirmed`. Intent-to-add on another path, ordinary staging, tracked-index changes, edits,
deletions, commit/amend/reset/checkout, ref or reflog changes, Git configuration changes, remote or
PR changes, or content changes on normalized paths remain `side_effect_detected`; never restore
the index or promote a failed artifact. Behavior verification additionally requires exact commands
and numeric statuses.

## Publication

After all validation and review gates pass, publication replaces intent-to-add entries using only
ordinary exact-path `git add -- <issue-scoped-paths>`. Verify that each empty-blob entry now names
its real content blob and that the staged path set is exactly within the allowlist. `git add .`,
`git add -A`, `git add --all`, out-of-scope staging, hook bypasses, `--no-verify`, force pushes, and
history rewrites remain prohibited.

Independent sandboxes and private Git indexes are deliberately out of scope for this change. They
remain potential future measures only if reviewers are observed performing side effects other than
intent-to-add.
