#!/usr/bin/env python3
"""Validate, run one foreground worker, and durably publish its lifecycle."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from worker_protocol import (
    PROTOCOL_VERSION,
    atomic_write_json,
    command_hash,
    descendant_identities,
    lifecycle_record,
    load_json,
    pre_launch_errors,
    process_identity,
    sha256_file,
    update_state,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--resume-after-completion",
        default="reconcile worker artifact, apply side-effect checks, and continue the issue loop",
    )
    parser.add_argument("--deadline")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("worker command is required after --")
    return arguments


def publish_lifecycle(path: Path, current: dict[str, Any]) -> None:
    atomic_write_json(path, current)


def structured_error(code: str, errors: list[str], *, spawned: bool, published: bool) -> None:
    json.dump(
        {
            "errorCode": code,
            "errors": errors,
            "workerSpawned": spawned,
            "artifactPublished": published,
        },
        sys.stderr,
        sort_keys=True,
    )
    sys.stderr.write("\n")


def main() -> int:
    arguments = parse_args()
    run_dir, repository = arguments.run_dir.resolve(), arguments.repository.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    lifecycle_path = run_dir / "worker-lifecycle.json"
    output_path = run_dir / "worker-output.log"
    exit_path = run_dir / "worker-exit.json"
    lock_path = run_dir / "worker-launch.lock"

    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            structured_error("launch_conflict", ["another runner owns the launch lock"], spawned=False, published=False)
            return 125

        try:
            state = load_json(run_dir / "state.json")
            run_id = state.get("runId") if isinstance(state.get("runId"), str) else None
        except Exception:
            state, run_id = {}, None
        errors = pre_launch_errors(run_dir, repository)
        if errors:
            lifecycle = lifecycle_record(
                run_id,
                "preflight_failed",
                errorCode="pre_launch_validation_failed",
                errors=errors,
            )
            publish_lifecycle(lifecycle_path, lifecycle)
            structured_error("pre_launch_validation_failed", errors, spawned=False, published=False)
            return 125

        # Keep immutable launch identity locally. Every later artifact uses these values,
        # never a state re-read.
        run_id = state["runId"]
        validated_generation = state["stateGeneration"]
        digest = command_hash(arguments.command)
        launch_state = update_state(
            run_dir,
            expected_generation=validated_generation,
            currentPhase="worker_launch_validated",
            workerCommandHash=digest,
            resumeAfterCompletion=arguments.resume_after_completion,
            launchDeadline=arguments.deadline,
            expectedArtifactPaths={
                "lifecycle": str(lifecycle_path),
                "stdout": str(output_path),
                "exit": str(exit_path),
            },
        )
        launch_generation = launch_state["stateGeneration"]
        launch_identity = {
            field: launch_state[field]
            for field in (
                "runId", "issueSnapshotHash", "repositoryIdentity", "repositoryRoot",
                "branch", "baseSha", "currentHead", "workerCommandHash",
            )
        }
        if load_json(run_dir / "state.json") != launch_state:
            errors = ["state changed after launch validation"]
            lifecycle = lifecycle_record(
                run_id, "preflight_failed", errorCode="state_mutated", errors=errors
            )
            publish_lifecycle(lifecycle_path, lifecycle)
            structured_error("state_mutated", errors, spawned=False, published=False)
            return 125

        lifecycle = lifecycle_record(
            run_id, "spawn_attempted", spawnAttempted=True
        )
        publish_lifecycle(lifecycle_path, lifecycle)
        started_at = utc_now()
        try:
            output = output_path.open("xb")
            try:
                worker = subprocess.Popen(
                    arguments.command,
                    cwd=repository,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=False,
                )
            except Exception:
                output.close()
                raise
        except Exception as error:
            lifecycle.update(
                stage="spawn_failed",
                errorCode="spawn_failed",
                errors=[str(error)],
                updatedAt=utc_now(),
            )
            publish_lifecycle(lifecycle_path, lifecycle)
            structured_error("spawn_failed", [str(error)], spawned=False, published=False)
            return 125

        identity = process_identity(worker.pid)
        if identity is None:
            worker.terminate()
            worker.wait()
            output.flush()
            os.fsync(output.fileno())
            output.close()
            lifecycle.update(
                stage="spawned",
                workerSpawned=True,
                workerPid=worker.pid,
                errorCode="identity_capture_failed",
                errors=["could not capture worker process identity"],
                updatedAt=utc_now(),
            )
            publish_lifecycle(lifecycle_path, lifecycle)
            structured_error("identity_capture_failed", lifecycle["errors"], spawned=True, published=False)
            return 125
        launch_identity["workerPid"] = worker.pid
        launch_identity["workerProcessIdentity"] = identity

        known_descendants: dict[tuple[int, str, str], dict[str, Any]] = {}
        lifecycle.update(
            stage="spawned",
            workerSpawned=True,
            workerPid=worker.pid,
            processIdentity=identity,
            updatedAt=utc_now(),
        )
        publish_lifecycle(lifecycle_path, lifecycle)
        state_error: str | None = None
        try:
            update_state(
                run_dir,
                expected_generation=launch_generation,
                currentPhase="worker_running",
                workerPid=worker.pid,
                workerStartTime=started_at,
                workerProcessIdentity=identity,
            )
        except Exception as error:
            # The child has started. Keep the wrapper alive until it really exits;
            # lifecycle evidence, not mutable state, records its identity.
            state_error = str(error)
        lifecycle.update(stage="running", updatedAt=utc_now())
        publish_lifecycle(lifecycle_path, lifecycle)
        while worker.poll() is None:
            for descendant in descendant_identities(worker.pid):
                key = (descendant["pid"], descendant["startToken"], descendant["command"])
                known_descendants[key] = descendant
            descendants = list(known_descendants.values())
            lifecycle.update(knownDescendantIdentities=descendants, updatedAt=utc_now())
            publish_lifecycle(lifecycle_path, lifecycle)
            if state_error is None:
                try:
                    update_state(run_dir, knownDescendantIdentities=descendants)
                except Exception as error:
                    state_error = str(error)
            time.sleep(0.2)
        worker_rc = worker.returncode
        lifecycle.update(
            stage="exited",
            workerExitStatus=worker_rc,
            knownDescendantIdentities=list(known_descendants.values()),
            updatedAt=utc_now(),
        )
        publish_lifecycle(lifecycle_path, lifecycle)
        output.flush()
        os.fsync(output.fileno())
        output.close()
        output_hash = sha256_file(output_path)
        lifecycle.update(stage="output_fsync_done", outputSha256=output_hash, updatedAt=utc_now())
        publish_lifecycle(lifecycle_path, lifecycle)

        try:
            final_state = load_json(run_dir / "state.json")
            mutated = [
                field for field, expected in launch_identity.items()
                if final_state.get(field) != expected
            ]
        except Exception as error:
            mutated = [f"unreadable state: {error}"]
        if state_error is not None or mutated:
            errors = ([f"state update failed: {state_error}"] if state_error else [])
            errors += [f"launch identity mutated: {field}" for field in mutated]
            lifecycle.update(
                stage="artifact_publish_failed",
                errorCode="state_mutated_after_spawn",
                errors=errors,
                updatedAt=utc_now(),
            )
            publish_lifecycle(lifecycle_path, lifecycle)
            structured_error("state_mutated_after_spawn", errors, spawned=True, published=False)
            return 125

        artifact = {
            "protocolVersion": PROTOCOL_VERSION,
            "runId": run_id,
            "pid": worker.pid,
            "processIdentity": identity,
            "startedAt": started_at,
            "finishedAt": utc_now(),
            "exitCode": worker_rc,
            "commandHash": digest,
            "outputSha256": output_hash,
            "launchStateGeneration": launch_generation,
        }
        try:
            atomic_write_json(exit_path, artifact)
        except Exception as error:
            lifecycle.update(
                stage="artifact_publish_failed",
                errorCode="artifact_publish_failed",
                errors=[str(error)],
                updatedAt=utc_now(),
            )
            publish_lifecycle(lifecycle_path, lifecycle)
            structured_error("artifact_publish_failed", [str(error)], spawned=True, published=False)
            return 125
        lifecycle.update(stage="artifact_published", artifactPublished=True, updatedAt=utc_now())
        publish_lifecycle(lifecycle_path, lifecycle)
        update_state(
            run_dir,
            currentPhase="worker_exit_published",
            workerExitStatus=worker_rc,
        )
        return worker_rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        structured_error("runner_internal_error", [str(error)], spawned=False, published=False)
        raise SystemExit(125)
