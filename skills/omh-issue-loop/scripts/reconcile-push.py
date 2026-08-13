#!/usr/bin/env python3
"""Reconcile a durable push attempt against process and Git postconditions."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from push_protocol import classify, remote_oid, upstream, validate_exit_artifact
from worker_protocol import atomic_write_json, identity_is_live, load_json, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--observations", type=int, default=2)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    metadata_path, exit_path = run_dir / "push-metadata.json", run_dir / "push-exit.json"
    stdout_path, stderr_path = run_dir / "push-stdout.log", run_dir / "push-stderr.log"
    if not metadata_path.is_file():
        result = {"status": "indeterminate", "reasons": ["push metadata is missing"]}
    else:
        metadata = load_json(metadata_path)
        stable = 0; previous = None; deadline = time.monotonic() + args.timeout
        while time.monotonic() <= deadline:
            identities = [metadata.get("processIdentity"), *metadata.get("knownDescendantIdentities", [])]
            live = any(identity_is_live(item) for item in identities)
            observation = (sha256_file(stdout_path), sha256_file(stderr_path))
            stable = stable + 1 if not live and observation == previous else (1 if not live else 0)
            previous = observation
            if stable >= args.observations: break
            time.sleep(args.interval)
            metadata = load_json(metadata_path)
        artifact_errors = []
        artifact = None
        if exit_path.is_file():
            artifact = load_json(exit_path)
            artifact_errors = validate_exit_artifact(artifact, metadata, stdout_path, stderr_path)
        else:
            artifact_errors = ["durable exit artifact is missing"]
        repo = args.repository.resolve()
        status = __import__("subprocess").run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
        head_result = __import__("subprocess").run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
        try:
            remote = remote_oid(repo, args.remote, args.branch)
        except RuntimeError as error:
            artifact_errors.append(str(error)); remote = None
        classification, reasons = classify(
            artifact_errors=artifact_errors,
            exit_code=artifact.get("exitCode") if artifact else None,
            process_tree_quiescent=stable >= args.observations,
            outputs_stable=stable >= args.observations,
            status_porcelain=status.stdout,
            head=head_result.stdout.strip(), upstream_oid=upstream(repo), remote_branch_oid=remote,
        )
        result = {"status": classification, "reasons": reasons, "exitCode": artifact.get("exitCode") if artifact else None,
                  "observed": {"head": head_result.stdout.strip(), "upstreamOid": upstream(repo), "remoteOid": remote}}
    atomic_write_json(run_dir / "push-reconciliation.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "confirmed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
