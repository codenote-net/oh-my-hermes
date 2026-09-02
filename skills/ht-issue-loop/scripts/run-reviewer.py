#!/usr/bin/env python3
"""Run one read-only reviewer and durably publish canonical review artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from operation_protocol import initial_state, spawn_gated, update_state
from normalization_protocol import validate_normalization_artifact
from reviewer_protocol import REVIEW_PROTOCOL_VERSION, common_git_config_hash
from worker_protocol import (
    atomic_write_json,
    command_hash,
    git,
    process_group_identities,
    sha256_file,
    utc_now,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--review-kind", required=True)
    parser.add_argument("--attempt-id")
    parser.add_argument("--deadline")
    parser.add_argument(
        "--resume-after-completion",
        default="reconcile the reviewer artifact and continue the exact-SHA review gate",
    )
    parser.add_argument("--require-command-evidence", action="store_true")
    parser.add_argument("--normalization-artifact", type=Path, required=True)
    parser.add_argument("--normalization-sha256", required=True)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("reviewer command is required after --")

    directory, repository = args.artifact_dir.resolve(), args.repository.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("artifact directory must be absent or empty")
    head = git(repository, "rev-parse", "HEAD").decode().strip()
    if head != args.target_sha:
        raise ValueError(f"repository HEAD {head} does not match target {args.target_sha}")
    normalization, normalization_errors = validate_normalization_artifact(
        artifact_path=args.normalization_artifact.resolve(),
        expected_sha256=args.normalization_sha256,
        round_id=args.round_id,
        repository=repository,
    )
    if normalization_errors:
        raise ValueError("; ".join(normalization_errors))
    if normalization["head"] != args.target_sha:
        raise ValueError("normalization artifact HEAD does not match reviewer target")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    baseline = {
        "schemaVersion": REVIEW_PROTOCOL_VERSION,
        "head": head,
        "statusPorcelain": git(repository, "status", "--porcelain=v1", "--untracked-files=all").decode(),
        "commonGitConfigSha256": common_git_config_hash(repository),
        "capturedAt": utc_now(),
    }
    baseline.update(
        normalizationArtifact=str(args.normalization_artifact.resolve()),
        normalizationSha256=args.normalization_sha256,
        roundId=args.round_id,
        repositoryState=normalization["after"],
    )
    atomic_write_json(directory / "baseline.json", baseline)
    baseline_sha256 = sha256_file(directory / "baseline.json")

    stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
    state_path = directory / "operation-state.json"
    digest = command_hash(args.command)
    state = initial_state(
        operation_kind="reviewer",
        attempt_id=args.attempt_id or str(uuid.uuid4()),
        command_hash=digest,
        target_sha=args.target_sha,
        expected_artifact_paths={
            "metadata": str(directory / "metadata.json"),
            "baseline": str(directory / "baseline.json"),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "exit": str(directory / "exit.json"),
        },
        deadline=args.deadline,
        resume_after_completion=args.resume_after_completion,
    )
    atomic_write_json(state_path, state)
    started_at = utc_now()
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        descendants: list[dict[str, object]] = []
        metadata: dict[str, object] = {}

        def publish_identity(identity_value: dict[str, object]) -> None:
            metadata.update(
                protocolVersion=REVIEW_PROTOCOL_VERSION,
                commandHash=digest,
                attemptId=state["attemptId"],
                deadline=args.deadline,
                resumeAfterCompletion=args.resume_after_completion,
                processIdentity=identity_value,
                knownDescendantIdentities=descendants,
                targetSha=args.target_sha,
                baselineSha256=baseline_sha256,
                reviewKind=args.review_kind,
                requireCommandEvidence=args.require_command_evidence,
                startedAt=started_at,
            )
            atomic_write_json(directory / "metadata.json", metadata)
            update_state(
                state_path,
                "reviewer",
                phase="reviewer_running",
                processIdentity=identity_value,
            )

        def validate_before_release() -> None:
            if sha256_file(directory / "baseline.json") != baseline_sha256:
                raise ValueError("reviewer baseline changed before reviewer release")
            _, errors = validate_normalization_artifact(
                artifact_path=args.normalization_artifact.resolve(),
                expected_sha256=args.normalization_sha256,
                round_id=args.round_id,
                repository=repository,
            )
            if errors:
                raise ValueError(
                    "normalized repository changed before reviewer release: " + "; ".join(errors)
                )

        worker = spawn_gated(
            args.command,
            cwd=repository,
            stdout=stdout,
            stderr=stderr,
            publish_identity=publish_identity,
            pre_release_check=validate_before_release,
        )
        identity = metadata["processIdentity"]
        while worker.poll() is None:
            for item in process_group_identities(worker.pid):
                if item not in descendants:
                    descendants.append(item)
            metadata["knownDescendantIdentities"] = descendants
            atomic_write_json(directory / "metadata.json", metadata)
            update_state(
                state_path,
                "reviewer",
                knownDescendantIdentities=descendants,
            )
            time.sleep(1)
        exit_code = worker.returncode
        for item in process_group_identities(worker.pid):
            if item not in descendants:
                descendants.append(item)
        metadata["knownDescendantIdentities"] = descendants
        atomic_write_json(directory / "metadata.json", metadata)
        update_state(
            state_path,
            "reviewer",
            knownDescendantIdentities=descendants,
        )
        stdout.flush(); os.fsync(stdout.fileno())
        stderr.flush(); os.fsync(stderr.fileno())

    artifact = {
        "protocolVersion": REVIEW_PROTOCOL_VERSION,
        "commandHash": digest,
        "processIdentity": identity,
        "targetSha": args.target_sha,
        "baselineSha256": baseline_sha256,
        "exitCode": exit_code,
        "stdoutSha256": sha256_file(stdout_path),
        "stderrSha256": sha256_file(stderr_path),
        "startedAt": started_at,
        "finishedAt": utc_now(),
    }
    atomic_write_json(directory / "exit.json", artifact)
    update_state(
        state_path,
        "reviewer",
        phase="reviewer_artifact_published",
        artifactFinishedAt=artifact["finishedAt"],
        knownDescendantIdentities=descendants,
    )
    print(json.dumps({"artifactDir": str(directory), "exitCode": exit_code}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"run-reviewer: {error}", file=sys.stderr)
        raise SystemExit(125)
