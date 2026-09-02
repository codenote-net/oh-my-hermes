#!/usr/bin/env python3
"""Classify a durable ht-issue-loop worker run after quiescence observations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker_protocol import (
    any_identity_is_live,
    load_json,
    sha256_file,
    update_state,
    validate_baseline_schema,
    validate_exit_artifact,
    validate_lifecycle_schema,
    validate_state_schema,
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
        errors.extend(validate_baseline_schema(baseline))
        if baseline.get("runId") != state.get("runId"):
            errors.append("worker baseline run ID mismatch")
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
    state_errors = validate_state_schema(state)
    lifecycle_path = run_dir / "worker-lifecycle.json"
    lifecycle = load_json(lifecycle_path) if lifecycle_path.is_file() else None
    lifecycle_errors = validate_lifecycle_schema(lifecycle) if lifecycle else []
    if lifecycle and not lifecycle_errors and lifecycle["stage"] == "preflight_failed":
        result = {
            "completionStatus": "rejected",
            "completionMode": "preflight_rejected_worker_not_started",
            "workerExitStatus": "unknown",
            "workerStarted": False,
            "artifactPublished": False,
            "reasons": lifecycle["errors"],
        }
        # The preflight rejection may itself be caused by an invalid state schema.
        # The lifecycle artifact is authoritative proof that no child was started;
        # do not try to "repair" or rewrite invalid state here.
        if not validate_state_schema(state):
            update_state(
                run_dir,
                completionMode=result["completionMode"],
                workerExitStatus="unknown",
                reconciliationResult=result,
            )
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    identity = (
        lifecycle.get("processIdentity")
        if lifecycle and lifecycle.get("workerSpawned")
        else state.get("workerProcessIdentity")
    )
    descendants = (
        lifecycle.get("knownDescendantIdentities", [])
        if lifecycle and lifecycle.get("workerSpawned")
        else state.get("knownDescendantIdentities", [])
    )
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
        "workerStarted": bool(lifecycle and lifecycle.get("workerSpawned")),
        "artifactPublished": bool(lifecycle and lifecycle.get("artifactPublished")),
        "reasons": [],
    }
    if state_errors:
        result["reasons"].extend(state_errors)
    if lifecycle_errors:
        result["reasons"].extend(lifecycle_errors)
    if lifecycle is None:
        result["reasons"].append("durable lifecycle evidence is missing")
    elif lifecycle.get("stage") == "artifact_publish_failed":
        result["reasons"].append("worker started but exit artifact publication failed")
    if result["reasons"]:
        pass
    elif any_identity_is_live(identity, descendants):
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
        elif lifecycle and lifecycle.get("workerSpawned"):
            errors = salvage_errors(run_dir, state, arguments.salvage_evidence)
            if errors:
                result["reasons"].extend(errors)
            else:
                result.update(
                    completionStatus="salvageable",
                    completionMode="salvaged_without_exit_artifact",
                    workerExitStatus="unknown",
                )
        else:
            result["reasons"].append("insufficient evidence that a worker was started")

    recovery_updates: dict[str, Any] = {}
    if result["completionStatus"] == "confirmed":
        finished_at = artifact.get("finishedAt")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(finished_at.replace("Z", "+00:00"))).total_seconds()
        except (AttributeError, ValueError):
            age = 0
        if age > 300 and state.get("currentPhase") in {
            "worker_running", "worker_exit_published", "pending_completion", "pending_reconciliation"
        }:
            recovery_updates = {
                "recoveryReason": "lost_or_unprocessed_completion_notification",
                "artifactFinishedAt": finished_at,
                "recoveredAt": datetime.now(timezone.utc).isoformat(),
                "resumedFromPhase": state.get("currentPhase"),
            }
            result["recoveryReason"] = recovery_updates["recoveryReason"]
    update_state(
        run_dir,
        completionMode=result["completionMode"],
        workerExitStatus=result["workerExitStatus"],
        reconciliationResult=result,
        **recovery_updates,
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
