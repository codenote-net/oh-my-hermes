#!/usr/bin/env python3
"""Classify a durable omh-issue-loop worker run after quiescence observations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from worker_protocol import (
    any_identity_is_live,
    load_json,
    sha256_file,
    update_state,
    validate_exit_artifact,
    working_tree_fingerprint,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--observations", type=int, default=2)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--salvage-evidence", type=Path)
    return parser.parse_args()


def salvage_errors(run_dir: Path, state: dict[str, Any], evidence_path: Path | None) -> list[str]:
    errors: list[str] = []
    for name in ("issue-snapshot.json", "worker-baseline.json", "worker-output.log"):
        if not (run_dir / name).is_file():
            errors.append(f"missing {name}")
    required_state = ("runId", "issueUrl", "repositoryIdentity", "branch", "baseSha")
    if any(not state.get(field) for field in required_state):
        errors.append("run identity is incomplete")
    if not isinstance(state.get("workerProcessIdentity"), dict):
        errors.append("worker process identity is missing")
    snapshot_path = run_dir / "issue-snapshot.json"
    if snapshot_path.is_file() and state.get("issueSnapshotHash") != sha256_file(snapshot_path):
        errors.append("issue snapshot hash mismatch")
    baseline_path = run_dir / "worker-baseline.json"
    if baseline_path.is_file():
        baseline = load_json(baseline_path)
        if not {
            "head",
            "commitRange",
            "reflog",
            "remoteBranch",
            "pullRequests",
        }.issubset(baseline):
            errors.append("worker baseline schema is incomplete")
    output_path = run_dir / "worker-output.log"
    if output_path.is_file() and output_path.stat().st_size == 0:
        errors.append("worker output is empty")
    if evidence_path is None or not evidence_path.is_file():
        errors.append("missing orchestrator salvage evidence")
        return errors
    evidence = load_json(evidence_path)
    for gate in (
        "baselineCompared",
        "sideEffectsClean",
        "reportComplete",
        "issueScopedDiff",
        "independentValidationPassed",
    ):
        if evidence.get(gate) is not True:
            errors.append(f"salvage gate failed: {gate}")
    validation_results = evidence.get("validationResults")
    if not isinstance(validation_results, list) or not validation_results:
        errors.append("independent validation results are missing")
    elif any(
        not isinstance(item, dict)
        or not item.get("command")
        or item.get("exitCode") != 0
        for item in validation_results
    ):
        errors.append("independent validation did not pass")
    return errors


def main() -> int:
    arguments = parse_args()
    run_dir = arguments.run_dir.resolve()
    repository = arguments.repository.resolve()
    state = load_json(run_dir / "state.json")
    identity = state.get("workerProcessIdentity")
    descendants = state.get("knownDescendantIdentities", [])
    if not isinstance(descendants, list):
        descendants = []
    deadline = time.monotonic() + arguments.timeout
    stable_count = 0
    previous: tuple[str, str] | None = None

    while time.monotonic() <= deadline:
        if any_identity_is_live(identity, descendants):
            stable_count = 0
            previous = None
        else:
            observation = (
                sha256_file(run_dir / "worker-output.log"),
                working_tree_fingerprint(repository),
            )
            stable_count = stable_count + 1 if observation == previous else 1
            previous = observation
            if stable_count >= arguments.observations:
                break
        time.sleep(arguments.interval)

    result: dict[str, Any] = {
        "completionStatus": "indeterminate",
        "completionMode": None,
        "workerExitStatus": "unknown",
        "reasons": [],
    }
    if any_identity_is_live(identity, descendants):
        result["reasons"].append("worker or known descendant process identity is still live")
    elif stable_count < arguments.observations:
        result["reasons"].append("output or working tree did not reach quiescence")
    else:
        exit_path = run_dir / "worker-exit.json"
        if exit_path.is_file():
            artifact = load_json(exit_path)
            errors = validate_exit_artifact(artifact, state, run_dir / "worker-output.log")
            if errors:
                result["reasons"].extend(errors)
            else:
                result.update(
                    completionStatus="confirmed",
                    completionMode="confirmed_with_exit_artifact",
                    workerExitStatus=artifact["exitCode"],
                )
        else:
            errors = salvage_errors(run_dir, state, arguments.salvage_evidence)
            if errors:
                result["reasons"].extend(errors)
            else:
                result.update(
                    completionStatus="salvageable",
                    completionMode="salvaged_without_exit_artifact",
                    workerExitStatus="unknown",
                )

    update_state(
        run_dir,
        completionMode=result["completionMode"],
        workerExitStatus=result["workerExitStatus"],
        reconciliationResult=result,
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["completionStatus"] in {"confirmed", "salvageable"} else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"reconcile-worker: {error}", file=sys.stderr)
        raise SystemExit(3)
