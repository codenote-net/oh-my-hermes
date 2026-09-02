#!/usr/bin/env python3
"""Create canonical ht-issue-loop state and immutable issue snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

from worker_protocol import (
    PROTOCOL_VERSION,
    STATE_SCHEMA_VERSION,
    atomic_write_json,
    git,
    sha256_file,
    utc_now,
    validate_state_schema,
    working_tree_fingerprint,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--repository-identity", required=True)
    parser.add_argument("--issue-url", required=True)
    parser.add_argument("--issue-snapshot", type=Path, required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--run-id")
    parser.add_argument("--signoff-required", choices=("true", "false"), required=True)
    args = parser.parse_args()
    run_dir, repository = args.run_dir.resolve(), args.repository.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("run directory must be absent or empty")
    with args.issue_snapshot.open(encoding="utf-8") as stream:
        snapshot = json.load(stream)
    if not isinstance(snapshot, dict) or snapshot.get("url") != args.issue_url:
        raise ValueError("issue snapshot must be an object with the matching url")
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    snapshot_path = run_dir / "issue-snapshot.json"
    atomic_write_json(snapshot_path, snapshot)
    head = git(repository, "rev-parse", "HEAD").decode().strip()
    state = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "stateGeneration": 1,
        "runId": args.run_id or str(uuid.uuid4()),
        "issueUrl": args.issue_url,
        "issueSnapshotHash": sha256_file(snapshot_path),
        "repositoryIdentity": args.repository_identity,
        "repositoryRoot": str(repository),
        "branch": git(repository, "branch", "--show-current").decode().strip(),
        "baseSha": args.base_sha or head,
        "currentHead": head,
        "expectedWorkingTreeFingerprint": working_tree_fingerprint(repository),
        "currentPhase": "initialized",
        "resumeAfterCompletion": None,
        "expectedArtifactPaths": {},
        "launchDeadline": None,
        "recoveryReason": None,
        "artifactFinishedAt": None,
        "recoveredAt": None,
        "resumedFromPhase": None,
        "workerCommandHash": None,
        "workerPid": None,
        "workerStartTime": None,
        "workerProcessIdentity": None,
        "knownDescendantIdentities": [],
        "completionMode": None,
        "workerExitStatus": "unknown",
        "validationPlan": [],
        "validationResults": [],
        "fixCount": 0,
        "reviewTargetSha": None,
        "prUrl": None,
        "signoffRequired": args.signoff_required == "true",
        "reconciliationResult": None,
        "latestStateUpdateTime": utc_now(),
    }
    errors = validate_state_schema(state)
    if errors:
        shutil.rmtree(run_dir)
        raise ValueError("; ".join(errors))
    atomic_write_json(run_dir / "state.json", state)
    print(json.dumps({"runDir": str(run_dir), "runId": state["runId"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
