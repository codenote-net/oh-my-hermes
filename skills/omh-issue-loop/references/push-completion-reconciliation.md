# Push Completion Reconciliation

Apply this fail-closed protocol to every first or subsequent issue-loop push. Preserve every
failed or indeterminate attempt in a separate durable attempt directory.

## Capture the baseline and validate the command

Before launch, durably capture the current branch, `HEAD`, porcelain status and tree fingerprint,
upstream (including absence), `git ls-remote --heads origin <branch>` OID (including absence), and
complete PR state for the head branch. Record the intended direct argument vector and its hash.
Use `push_protocol.command_safety_errors` or equivalent validation before spawn.

Reject anything except a normal direct `git push`, including `--no-verify`, `--force`, `-f`,
`--force-with-lease`, a leading `+` refspec, hook deletion/renaming/configuration/bypass, `nohup`,
a trailing `&`, or any nested background/detached wrapper. Never change global Git configuration,
disable signing, replace hooks, or substitute separately rerun validation for the hook execution.

## Launch exactly one background layer

Give each attempt a fresh directory and launch this wrapper with a generous bounded Hermes task
allowance (at least ten minutes, or longer when the discovered repository validation requires it):

```text
terminal(
  command="python3 <skill>/scripts/run-push.py --run-dir <attempt-dir> \
    --repository <repo-root> --run-id <run-id> --attempt-id <unique-attempt-id> \
    --deadline <iso-8601-deadline> \
    --resume-after-completion '<exact next action>' -- \
    git push --set-upstream origin <branch>",
  background=true,
  notify_on_complete=true
)
```

Do not add a shell background operator. The wrapper keeps `git push` in the foreground, permits
the normal pre-push hook (including heavy bounded validation such as a package-manager pre-push
check), and remains alive until Git returns. It durably preserves `push-stdout.log`,
`push-stderr.log`, `push-metadata.json`, and an atomically published `push-exit.json` with numeric
exit status and output hashes. The metadata retains every descendant identity observed while the
push runs, including `git remote-https`, the pre-push hook, and validation descendants.

The wrapper also persists `push-operation-state.json` with the command hash, target SHA, expected
artifact paths, deadline, process tree, and exact continuation. It transitions through
`push_launch_pending`, `push_running`, and `push_artifact_published`. Successful reconciliation
writes `push_reconciled`; when a valid artifact remained unprocessed for more than five minutes,
it also records `lost_or_unprocessed_completion_notification` and the recovery timestamps.

## Reconcile completion

Run `scripts/reconcile-push.py` after any notification, timeout, anomaly, or apparent exit.
`status=exited, exit_code=null` is only a reconciliation trigger. It is never completion.

Require at least two stable stdout/stderr observations and confirmed death of the full recorded
process identity and every known descendant identity. Identity is PID plus process start token and
command, never PID alone. An exited or zombie launcher does not release this gate while a recorded
descendant remains alive. A reparented descendant that remains in the supervised process group
stays part of the known tree. The push and its hooks must not create a new session or otherwise
escape that group. If any identity cannot be checked, any output remains unstable, or complete
termination cannot be proved, classify the attempt `indeterminate`, preserve evidence, warn that
side effects may still occur, and stop.

Validate `push-exit.json` protocol, run identity, command hash, full process identity, integer exit
code, and both output hashes. A missing or mismatched artifact is `indeterminate`, even when the
remote branch happens to match. A valid numeric nonzero exit is a push failure after quiescence;
do not relabel a Hermes foreground timeout as Git, GitHub, or hook failure.

For numeric exit zero, execute and preserve the numeric status of:

```sh
git status --porcelain
git rev-parse HEAD
git rev-parse '@{upstream}'
git ls-remote --heads origin <branch>
```

Confirm success only when the tree is clean, upstream exists, local `HEAD` equals the upstream
SHA, the remote branch OID equals the same `HEAD`, the hook was not bypassed, the artifact is valid
with exit zero, and process/output quiescence is proven. Observed remote state without process
completion is only an observation. Do not proceed to signoff, PR creation, PR mutation, or review
until success is confirmed for the exact `HEAD`.

## Timeout and retry

On a foreground timeout, null-exit notification, or missing exit code, inspect the prior wrapper,
Git process, all known descendants, remote OID, upstream, and relevant PR state. Continue
reconciliation while anything lives. Never launch a second push concurrently and never use a
hook bypass to recover.

Permit one new normal push attempt only after process death and quiescence are proven and a full
comparison shows all of the following unchanged from the captured baseline: working tree,
branch, `HEAD`, upstream, PR state, and remote OID. The remote must remain absent or exactly at its
baseline OID, and no PR or other remote side effect may have occurred. Retry without force,
history rewriting, hook changes, or validation bypass. If any comparison is unavailable or
changed, retry is prohibited; stop as `indeterminate` or reconcile the observed completed side
effect without issuing another push.
