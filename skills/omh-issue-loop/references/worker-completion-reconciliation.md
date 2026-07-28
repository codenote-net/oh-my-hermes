# Worker Completion Reconciliation

Apply this protocol to every background Codex implementation, fix, or report-repair worker. Use
the bundled scripts rather than reconstructing the protocol in a prompt.

## Single-layer launch

Resolve `$HERMES_HOME` from the Hermes runtime and create the durable run state described in
[resume-and-salvage.md](resume-and-salvage.md). Launch the wrapper itself with Hermes:

```text
terminal(
  command="python3 <skill>/scripts/run-worker.py --run-dir <durable-run-dir> \
    --repository <repo-root> -- codex --yolo exec ...",
  background=true,
  notify_on_complete=true
)
```

`run-worker.py` starts Codex as a foreground child, records its process identity and known
descendants, waits for it, and then publishes `worker-exit.json`. Never add shell `(...) &`, a
trailing `&`, `nohup`, or another background layer inside the command. Hermes backgrounds exactly
one tracked wrapper, and the wrapper must remain alive until Codex exits.

## Protocol v2 artifact

The wrapper writes output to `worker-output.log` and atomically publishes a versioned JSON artifact
only after the worker returns:

```json
{
  "protocolVersion": 2,
  "runId": "...",
  "pid": 12345,
  "processIdentity": {
    "pid": 12345,
    "startToken": "...",
    "command": "..."
  },
  "startedAt": "...",
  "finishedAt": "...",
  "exitCode": 0,
  "commandHash": "...",
  "outputSha256": "..."
}
```

The helper writes JSON to a same-directory temporary file, flushes and `fsync`s it, atomically
renames it, then `fsync`s the directory. Validate the schema, protocol version, run ID, command
hash, full process identity, integer exit code, and output hash. PID alone is never identity.

## Reconciliation

Run:

```bash
python3 <skill>/scripts/reconcile-worker.py \
  --run-dir <durable-run-dir> --repository <repo-root>
```

The reconciler observes process identity, known descendants, output hash, and a fingerprint of
HEAD, staged and unstaged diffs, status, and untracked content. Defaults are two identical
observations, five seconds apart, within ten minutes.

Classify completion as exactly one of:

- `confirmed`: valid matching protocol-v2 artifact, numeric exit code, no live matching process or
  known descendant, and stable output and working tree. This confirms the recorded exit code; a
  nonzero code still fails the worker gate.
- `salvageable`: no artifact, but the legacy salvage gates in
  [resume-and-salvage.md](resume-and-salvage.md) all passed, including independent validation.
  Record `completion_mode: salvaged_without_exit_artifact` and
  `worker_exit_status: unknown`; never represent it as exit zero.
- `indeterminate`: a process may live, identity or baseline is missing, comparison is impossible,
  output/tree is unstable, scope is uncertain, or run/snapshot identity is incomplete.

`status=exited` with `exit_code=null` from the process API is only a reconciliation trigger. It is
not a terminal result. Do not begin post-worker checks, inspect a supposedly final diff, or make a
definitive report until reconciliation returns `confirmed` or an authorized `salvageable` result.

## Timeout and safe stop

If reconciliation times out, terminate only the recorded and identity-matched worker processes:
request graceful termination, wait 30 seconds, then force termination only if still necessary.
Continue observations until process death and quiescence are confirmed. A terminated worker is a
timeout failure, not success.

If identity-matched process death cannot be confirmed, stop as `indeterminate`. Preserve durable
state and report PIDs, identities, artifact state, output metadata, and the last tree fingerprint.
Explicitly warn that further mutations may occur. Never claim `no changes`, a final changed-file
list, or settled validation results.

Reconciliation and salvage do not consume a fix iteration. A later fix uses a fresh protocol-v2
worker record and artifact.
