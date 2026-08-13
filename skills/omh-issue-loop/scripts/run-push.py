#!/usr/bin/env python3
"""Run one normal push as a foreground child and durably preserve its result."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from push_protocol import PUSH_PROTOCOL_VERSION, command_safety_errors
from worker_protocol import atomic_write_json, command_hash, descendant_identities, process_identity, sha256_file, utc_now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
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
    if any(path.exists() for path in paths.values()):
        print("push attempt artifacts already exist; preserve them and use a new attempt directory", file=sys.stderr)
        return 125
    with (run_dir / "push-launch.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another push wrapper owns the launch lock", file=sys.stderr)
            return 125
        started = utc_now()
        with paths["stdout.log"].open("xb") as stdout, paths["stderr.log"].open("xb") as stderr:
            child = subprocess.Popen(command, cwd=args.repository, stdout=stdout, stderr=stderr)
            identity = process_identity(child.pid)
            if identity is None:
                child.terminate(); child.wait()
                return 125
            metadata = {
                "protocolVersion": PUSH_PROTOCOL_VERSION, "runId": args.run_id,
                "command": command, "commandHash": command_hash(command),
                "processIdentity": identity, "knownDescendantIdentities": [], "startedAt": started,
            }
            atomic_write_json(paths["metadata.json"], metadata)
            known = {}
            while child.poll() is None:
                for item in descendant_identities(child.pid):
                    known[(item["pid"], item["startToken"], item["command"])] = item
                metadata["knownDescendantIdentities"] = list(known.values())
                atomic_write_json(paths["metadata.json"], metadata)
                time.sleep(0.2)
            stdout.flush(); stderr.flush(); os.fsync(stdout.fileno()); os.fsync(stderr.fileno())
        artifact = {
            "protocolVersion": PUSH_PROTOCOL_VERSION, "runId": args.run_id,
            "commandHash": metadata["commandHash"], "processIdentity": identity,
            "exitCode": child.returncode, "stdoutSha256": sha256_file(paths["stdout.log"]),
            "stderrSha256": sha256_file(paths["stderr.log"]), "startedAt": started, "finishedAt": utc_now(),
        }
        atomic_write_json(paths["exit.json"], artifact)
        return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
