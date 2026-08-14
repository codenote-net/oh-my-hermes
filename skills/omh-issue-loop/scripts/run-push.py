#!/usr/bin/env python3
"""Run one normal push as a foreground child and durably preserve its result."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from operation_protocol import initial_state, spawn_gated, update_state
from push_protocol import PUSH_PROTOCOL_VERSION, command_safety_errors
from worker_protocol import atomic_write_json, command_hash, load_json, process_group_identities, sha256_file, utc_now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id")
    parser.add_argument("--deadline")
    parser.add_argument(
        "--resume-after-completion",
        default="reconcile the push artifact and continue publication for the exact HEAD",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    errors = command_safety_errors(command)
    if errors:
        print("; ".join(errors), file=sys.stderr)
        return 125
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: run_dir / f"push-{name}" for name in ("metadata.json", "exit.json", "stdout.log", "stderr.log")}
    state_path = run_dir / "push-operation-state.json"
    if state_path.exists() or any(path.exists() for path in paths.values()):
        print("push attempt artifacts already exist; preserve them and use a new attempt directory", file=sys.stderr)
        return 125
    with (run_dir / "push-launch.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another push wrapper owns the launch lock", file=sys.stderr)
            return 125
        target_sha = subprocess.run(
            ["git", "-C", str(args.repository.resolve()), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        digest = command_hash(command)
        atomic_write_json(
            state_path,
            initial_state(
                operation_kind="push",
                attempt_id=args.attempt_id or str(uuid.uuid4()),
                command_hash=digest,
                target_sha=target_sha,
                expected_artifact_paths={name: str(path) for name, path in paths.items()},
                deadline=args.deadline,
                resume_after_completion=args.resume_after_completion,
            ),
        )
        started = utc_now()
        with paths["stdout.log"].open("xb") as stdout, paths["stderr.log"].open("xb") as stderr:
            metadata = {}

            def publish_identity(identity_value):
                metadata.update({
                    "protocolVersion": PUSH_PROTOCOL_VERSION, "runId": args.run_id,
                    "command": command, "commandHash": digest,
                    "attemptId": load_json(state_path)["attemptId"],
                    "deadline": args.deadline,
                    "resumeAfterCompletion": args.resume_after_completion,
                    "processIdentity": identity_value, "knownDescendantIdentities": [],
                    "startedAt": started,
                })
                atomic_write_json(paths["metadata.json"], metadata)
                update_state(
                    state_path,
                    "push",
                    phase="push_running",
                    processIdentity=identity_value,
                )

            child = spawn_gated(
                command,
                cwd=args.repository.resolve(),
                stdout=stdout,
                stderr=stderr,
                publish_identity=publish_identity,
            )
            identity = metadata["processIdentity"]
            known = {}
            while child.poll() is None:
                for item in process_group_identities(child.pid):
                    known[(item["pid"], item["startToken"], item["command"])] = item
                metadata["knownDescendantIdentities"] = list(known.values())
                atomic_write_json(paths["metadata.json"], metadata)
                update_state(
                    state_path,
                    "push",
                    knownDescendantIdentities=list(known.values()),
                )
                time.sleep(0.2)
            stdout.flush(); stderr.flush(); os.fsync(stdout.fileno()); os.fsync(stderr.fileno())
        artifact = {
            "protocolVersion": PUSH_PROTOCOL_VERSION, "runId": args.run_id,
            "commandHash": metadata["commandHash"], "processIdentity": identity,
            "exitCode": child.returncode, "stdoutSha256": sha256_file(paths["stdout.log"]),
            "stderrSha256": sha256_file(paths["stderr.log"]), "startedAt": started, "finishedAt": utc_now(),
        }
        atomic_write_json(paths["exit.json"], artifact)
        update_state(
            state_path,
            "push",
            phase="push_artifact_published",
            artifactFinishedAt=artifact["finishedAt"],
            knownDescendantIdentities=metadata["knownDescendantIdentities"],
        )
        return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
