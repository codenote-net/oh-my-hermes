#!/usr/bin/env python3
"""Capture local baseline plus orchestrator-provided GitHub evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from worker_protocol import (
    BASELINE_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    atomic_write_json,
    git,
    load_json,
    sha256_bytes,
    sha256_file,
    update_state,
    utc_now,
    validate_baseline_schema,
    validate_state_schema,
    working_tree_fingerprint,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--remote-branch-oid", required=True, help="Git OID or 'absent'")
    parser.add_argument("--pull-requests-json", type=Path, required=True)
    args = parser.parse_args()
    run_dir, repository = args.run_dir.resolve(), args.repository.resolve()
    state = load_json(run_dir / "state.json")
    errors = validate_state_schema(state)
    if errors:
        raise ValueError("; ".join(errors))
    with args.pull_requests_json.open(encoding="utf-8") as stream:
        pull_requests = json.load(stream)
    if not isinstance(pull_requests, list):
        raise ValueError("pull request evidence must be a JSON array")
    status = git(repository, "status", "--short", "--branch").decode(errors="replace")
    porcelain = git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    untracked = []
    for entry in porcelain.split(b"\0"):
        if entry.startswith(b"?? "):
            relative = entry[3:].decode(errors="surrogateescape")
            path = repository / relative
            untracked.append({"path": relative, "sha256": sha256_file(path)})
    head = git(repository, "rev-parse", "HEAD").decode().strip()
    baseline = {
        "schemaVersion": BASELINE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "runId": state["runId"],
        "issueSnapshotHash": state["issueSnapshotHash"],
        "repositoryIdentity": state["repositoryIdentity"],
        "repositoryRoot": str(repository),
        "branch": git(repository, "branch", "--show-current").decode().strip(),
        "baseSha": state["baseSha"],
        "head": head,
        "commitRange": git(repository, "log", "--format=%H", f"{state['baseSha']}..HEAD").decode().splitlines(),
        "reflog": git(repository, "reflog", "--format=%H %gs", state["branch"]).decode(errors="replace").splitlines(),
        "statusShort": status,
        "stagedDiffSha256": sha256_bytes(git(repository, "diff", "--cached", "--binary")),
        "unstagedDiffSha256": sha256_bytes(git(repository, "diff", "--binary")),
        "untrackedFiles": sorted(untracked, key=lambda item: item["path"]),
        "remoteBranchOid": None if args.remote_branch_oid == "absent" else args.remote_branch_oid,
        "pullRequests": pull_requests,
        "capturedAt": utc_now(),
    }
    errors = validate_baseline_schema(baseline)
    if errors:
        raise ValueError("; ".join(errors))
    atomic_write_json(run_dir / "worker-baseline.json", baseline)
    update_state(
        run_dir,
        currentHead=head,
        expectedWorkingTreeFingerprint=working_tree_fingerprint(repository),
        currentPhase="worker_baseline_captured",
        workerCommandHash=None,
        workerPid=None,
        workerStartTime=None,
        workerProcessIdentity=None,
        knownDescendantIdentities=[],
        completionMode=None,
        workerExitStatus="unknown",
    )
    print(json.dumps({"valid": True, "baseline": str(run_dir / "worker-baseline.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
