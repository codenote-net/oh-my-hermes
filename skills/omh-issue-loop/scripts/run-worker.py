#!/usr/bin/env python3
"""Run one worker in the foreground and atomically publish its exit artifact."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from worker_protocol import (
    PROTOCOL_VERSION,
    atomic_write_json,
    command_hash,
    descendant_identities,
    process_identity,
    sha256_file,
    update_state,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("worker command is required after --")
    return arguments


def main() -> int:
    arguments = parse_args()
    run_dir = arguments.run_dir.resolve()
    repository = arguments.repository.resolve()
    output_path = run_dir / "worker-output.log"
    exit_path = run_dir / "worker-exit.json"
    started_at = utc_now()
    digest = command_hash(arguments.command)

    with output_path.open("wb") as output:
        worker = subprocess.Popen(
            arguments.command,
            cwd=repository,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=False,
        )
        identity = process_identity(worker.pid)
        if identity is None:
            worker.terminate()
            worker.wait()
            raise RuntimeError("could not capture worker process identity")
        update_state(
            run_dir,
            currentPhase="worker_running",
            workerPid=worker.pid,
            workerStartTime=started_at,
            workerCommandHash=digest,
            workerProcessIdentity=identity,
            knownDescendantIdentities=[],
            completionMode=None,
        )
        known_descendants: dict[tuple[int, str, str], dict] = {}
        while worker.poll() is None:
            for descendant in descendant_identities(worker.pid):
                key = (
                    descendant["pid"],
                    descendant["startToken"],
                    descendant["command"],
                )
                known_descendants[key] = descendant
            update_state(
                run_dir,
                knownDescendantIdentities=list(known_descendants.values()),
            )
            time.sleep(0.2)
        worker_rc = worker.returncode
        output.flush()
        os.fsync(output.fileno())

    artifact = {
        "protocolVersion": PROTOCOL_VERSION,
        "runId": update_state(run_dir)["runId"],
        "pid": worker.pid,
        "processIdentity": identity,
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "exitCode": worker_rc,
        "commandHash": digest,
        "outputSha256": sha256_file(output_path),
    }
    atomic_write_json(exit_path, artifact)
    update_state(run_dir, currentPhase="worker_exit_published")
    return worker_rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"run-worker: {error}", file=sys.stderr)
        raise SystemExit(125)
