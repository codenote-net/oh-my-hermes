#!/usr/bin/env python3
"""Reconcile one durable reviewer artifact directory."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from operation_protocol import binding_errors, recovery_updates, state_errors, update_state
from reviewer_protocol import artifact_errors, load, process_state, report_errors, repository_observation
from worker_protocol import process_group_identities, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--require-command-evidence", action="store_true")
    parser.add_argument("--observations", type=int, default=2)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()
    directory, repository = args.artifact_dir.resolve(), args.repository.resolve()
    paths = {name: directory / name for name in ("metadata.json", "baseline.json", "stdout.log", "stderr.log", "exit.json")}
    state_path = directory / "operation-state.json"
    if not state_path.is_file():
        result = {"classification": "indeterminate", "reasons": ["missing operation-state.json"]}
        print(json.dumps(result, indent=2, sort_keys=True)); return 2
    state = load(state_path)
    errors = state_errors(state, "reviewer")
    if errors:
        result = {"classification": "invalid", "reasons": errors}
        print(json.dumps(result, indent=2, sort_keys=True)); return 2
    missing = [name for name, path in paths.items() if not path.is_file()]
    if "metadata.json" in missing or "baseline.json" in missing:
        result = {"classification": "indeterminate", "reasons": [f"missing {name}" for name in missing]}
        print(json.dumps(result, indent=2, sort_keys=True)); return 2
    metadata, baseline = load(paths["metadata.json"]), load(paths["baseline.json"])
    deadline, previous, stable = time.monotonic() + args.timeout, None, 0
    while time.monotonic() <= deadline:
        state = load(state_path)
        metadata = load(paths["metadata.json"])
        identity = metadata.get("processIdentity")
        descendants = metadata.get("knownDescendantIdentities", [])
        observed_group = process_group_identities(identity["pid"]) if isinstance(identity, dict) and isinstance(identity.get("pid"), int) else []
        states = [process_state(identity), *(process_state(item) for item in descendants), *(process_state(item) for item in observed_group)]
        live = states[0] == "live" or any(state in {"live", "zombie"} for state in states[1:])
        if live:
            stable, previous = 0, None
        else:
            observation = (sha256_file(paths["stdout.log"]), sha256_file(paths["stderr.log"]), repository_observation(repository))
            stable = stable + 1 if observation == previous else 1
            previous = observation
            if stable >= args.observations:
                break
        time.sleep(args.interval)
    bindings = binding_errors(
        state,
        metadata,
        target_sha=metadata.get("targetSha", ""),
        expected_artifact_paths={
            "metadata": str(paths["metadata.json"]),
            "baseline": str(paths["baseline.json"]),
            "stdout": str(paths["stdout.log"]),
            "stderr": str(paths["stderr.log"]),
            "exit": str(paths["exit.json"]),
        },
    )
    if bindings:
        result = {"classification": "invalid", "reasons": bindings}
        print(json.dumps(result, indent=2, sort_keys=True)); return 2
    if live:
        result = {"classification": "running", "reasons": ["reviewer process tree is still live; duplicate launch prohibited"]}
    elif stable < args.observations:
        result = {"classification": "indeterminate", "reasons": ["output or repository did not reach quiescence"]}
    elif not paths["exit.json"].is_file():
        result = {"classification": "indeterminate", "reasons": ["reviewer stopped without an exit artifact"]}
    else:
        artifact = load(paths["exit.json"])
        errors = artifact_errors(artifact, metadata, paths["stdout.log"], paths["stderr.log"])
        if errors:
            result = {"classification": "invalid", "reasons": errors}
        elif artifact["targetSha"] != args.expected_head or repository_observation(repository)[0] != args.expected_head:
            result = {"classification": "stale_target", "reasons": ["artifact or repository HEAD differs from expected HEAD"]}
        else:
            current = repository_observation(repository)
            changed = [label for label, value, expected in zip(("HEAD", "working tree", "common Git configuration"), current, (baseline.get("head"), baseline.get("statusPorcelain"), baseline.get("commonGitConfigSha256"))) if value != expected]
            if changed:
                result = {"classification": "side_effect_detected", "reasons": [f"unauthorized change: {item}" for item in changed]}
            elif artifact["exitCode"] != 0:
                result = {"classification": "indeterminate", "reasons": [f"reviewer exited with status {artifact['exitCode']}"]}
            else:
                errors = report_errors(paths["stdout.log"].read_text(encoding="utf-8", errors="replace"), args.require_command_evidence)
                result = {"classification": "incomplete_report" if errors else "confirmed", "reasons": errors}
        if result["classification"] == "confirmed":
            updates = recovery_updates(state, artifact.get("finishedAt"))
            update_state(state_path, "reviewer", phase="reviewer_reconciled", **updates)
            result["lostNotificationCandidate"] = bool(updates)
            if updates:
                result["recoveryReason"] = updates["recoveryReason"]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["classification"] == "confirmed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"reconcile-reviewer: {error}", file=sys.stderr)
        raise SystemExit(3)
