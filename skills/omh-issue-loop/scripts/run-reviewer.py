#!/usr/bin/env python3
"""Run one read-only reviewer and durably publish canonical review artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from reviewer_protocol import REVIEW_PROTOCOL_VERSION, common_git_config_hash
from worker_protocol import (
    atomic_write_json,
    command_hash,
    descendant_identities,
    git,
    process_identity,
    sha256_file,
    utc_now,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--review-kind", required=True)
    parser.add_argument("--require-command-evidence", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("reviewer command is required after --")

    directory, repository = args.artifact_dir.resolve(), args.repository.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("artifact directory must be absent or empty")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    head = git(repository, "rev-parse", "HEAD").decode().strip()
    if head != args.target_sha:
        raise ValueError(f"repository HEAD {head} does not match target {args.target_sha}")
    baseline = {
        "head": head,
        "statusPorcelain": git(repository, "status", "--porcelain=v1", "--untracked-files=all").decode(),
        "commonGitConfigSha256": common_git_config_hash(repository),
        "capturedAt": utc_now(),
    }
    atomic_write_json(directory / "baseline.json", baseline)

    stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
    digest = command_hash(args.command)
    started_at = utc_now()
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        worker = subprocess.Popen(args.command, cwd=repository, stdout=stdout, stderr=stderr)
        identity = process_identity(worker.pid)
        if identity is None:
            worker.terminate()
            worker.wait()
            raise RuntimeError("could not capture reviewer process identity")
        descendants: list[dict[str, object]] = []
        metadata = {
            "protocolVersion": REVIEW_PROTOCOL_VERSION,
            "commandHash": digest,
            "processIdentity": identity,
            "knownDescendantIdentities": descendants,
            "targetSha": args.target_sha,
            "reviewKind": args.review_kind,
            "requireCommandEvidence": args.require_command_evidence,
            "startedAt": started_at,
        }
        atomic_write_json(directory / "metadata.json", metadata)
        while worker.poll() is None:
            for item in descendant_identities(worker.pid):
                if item not in descendants:
                    descendants.append(item)
            metadata["knownDescendantIdentities"] = descendants
            atomic_write_json(directory / "metadata.json", metadata)
            time.sleep(1)
        exit_code = worker.returncode
        for item in descendant_identities(worker.pid):
            if item not in descendants:
                descendants.append(item)
        metadata["knownDescendantIdentities"] = descendants
        atomic_write_json(directory / "metadata.json", metadata)
        stdout.flush(); os.fsync(stdout.fileno())
        stderr.flush(); os.fsync(stderr.fileno())

    artifact = {
        "protocolVersion": REVIEW_PROTOCOL_VERSION,
        "commandHash": digest,
        "processIdentity": identity,
        "targetSha": args.target_sha,
        "exitCode": exit_code,
        "stdoutSha256": sha256_file(stdout_path),
        "stderrSha256": sha256_file(stderr_path),
        "startedAt": started_at,
        "finishedAt": utc_now(),
    }
    atomic_write_json(directory / "exit.json", artifact)
    print(json.dumps({"artifactDir": str(directory), "exitCode": exit_code}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"run-reviewer: {error}", file=sys.stderr)
        raise SystemExit(125)
