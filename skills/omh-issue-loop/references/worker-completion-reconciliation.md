# Worker Completion Reconciliation

Use this protocol for every background Codex implementation, fix, or report-repair worker. Its
purpose is to distinguish a runner's transient status from actual process completion. Fail closed,
but never inspect or report a supposedly final working tree while a worker may still mutate it.

## Launch contract

1. Create a unique run directory outside the repository. Store the worker PID, combined output,
   and exit artifact there.
2. Start the worker through a wrapper that runs Codex in the foreground, captures stdout and
   stderr, writes the numeric exit code to a temporary file, and atomically renames that file to
   its final name only after Codex returns. Record the wrapper PID and, where supported, its
   process group. Never write the artifact directly to its final path.
3. Treat the final atomic exit artifact as the exit-code authority. A runner response such as
   `status=exited` with `exit_code=null`, a missing artifact, or a live wrapper/descendant is
   indeterminate rather than success or failure.

Equivalent wrapper shape:

```bash
run_dir=<unique-directory-outside-repository>
exit_tmp="$run_dir/exit-code.tmp"
exit_final="$run_dir/exit-code"
output="$run_dir/output.log"

(
  set +e
  codex --yolo exec --ephemeral -c model='"gpt-5.6-sol"' \
    -c model_reasoning_effort='"low"' -c service_tier='"fast"' '<PROMPT>' \
    >"$output" 2>&1
  worker_rc=$?
  printf '%s\n' "$worker_rc" >"$exit_tmp"
  mv "$exit_tmp" "$exit_final"
) &
worker_pid=$!
printf '%s\n' "$worker_pid" >"$run_dir/wrapper.pid"
```

Use the host's safe equivalent when this exact shell shape is unavailable. Do not place run
artifacts in the repository or include them in issue-scoped diffs.

## Bounded reconciliation

When the normal wait returns any ambiguous state, including `exited/null`, enter reconciliation
instead of post-worker checks:

1. Poll for up to 10 minutes, no more frequently than every 5 seconds.
2. On every poll, check:
   - whether the wrapper PID, process group, or known descendants are still alive;
   - whether the atomic exit artifact exists and contains exactly one valid integer;
   - output file size, modification time, and preferably a content hash;
   - a working-tree fingerprint covering `HEAD`, staged and unstaged diffs, status, and untracked
     file names and content hashes.
3. Completion is confirmed only when all are true:
   - the atomic exit artifact is valid;
   - the wrapper and every known worker descendant have stopped;
   - output metadata or hash is unchanged across two consecutive polls;
   - the working-tree fingerprint is unchanged across the same two consecutive polls.
4. Read and preserve the complete output only after completion is confirmed. Use the artifact's
   numeric value as the exit code, then begin the existing post-worker side-effect and semantic
   report checks.

Do not mistake a temporarily stable output or working tree for completion while any worker
process is alive.

## Reconciliation timeout

If the 10-minute reconciliation deadline expires:

1. Request graceful termination of only the recorded wrapper/process group, wait up to 30 seconds,
   then use forceful termination only for the same validated worker processes when necessary.
2. Continue polling until process death is confirmed and output plus working tree are stable
   across two consecutive polls, using the same five-second interval. Do not proceed to reviews
   or Git/GitHub mutations.
3. If process death still cannot be confirmed, stop the workflow in an indeterminate
   orchestration-failure state. Report the recorded PIDs, artifact state, last output metadata,
   and last working-tree fingerprint. Explicitly warn that further worker mutations may occur.
   Never claim `no changes`, a final changed-file list, or a settled validation result.
4. If process death and quiescence are confirmed after termination, preserve the evidence and stop
   as a worker-timeout failure. Do not treat a late or forced exit as a successful worker run.

Apply the same protocol independently to the one-time report-repair worker. Reconciliation does
not consume a fix iteration.
