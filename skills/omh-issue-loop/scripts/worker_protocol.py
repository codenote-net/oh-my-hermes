#!/usr/bin/env python3
"""Canonical durable worker protocol for omh-issue-loop."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 2
STATE_SCHEMA_VERSION = 1
BASELINE_SCHEMA_VERSION = 1
LIFECYCLE_SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

STATE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schemaVersion": int,
    "protocolVersion": int,
    "stateGeneration": int,
    "runId": str,
    "issueUrl": str,
    "issueSnapshotHash": str,
    "repositoryIdentity": str,
    "repositoryRoot": str,
    "branch": str,
    "baseSha": str,
    "currentHead": str,
    "expectedWorkingTreeFingerprint": str,
    "currentPhase": str,
    "resumeAfterCompletion": (str, type(None)),
    "expectedArtifactPaths": dict,
    "launchDeadline": (str, type(None)),
    "recoveryReason": (str, type(None)),
    "artifactFinishedAt": (str, type(None)),
    "recoveredAt": (str, type(None)),
    "resumedFromPhase": (str, type(None)),
    "workerCommandHash": (str, type(None)),
    "workerPid": (int, type(None)),
    "workerStartTime": (str, type(None)),
    "workerProcessIdentity": (dict, type(None)),
    "knownDescendantIdentities": list,
    "completionMode": (str, type(None)),
    "workerExitStatus": (int, str, type(None)),
    "validationPlan": list,
    "validationResults": list,
    "fixCount": int,
    "reviewTargetSha": (str, type(None)),
    "prUrl": (str, type(None)),
    "signoffRequired": bool,
    "reconciliationResult": (dict, type(None)),
    "latestStateUpdateTime": str,
}

BASELINE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schemaVersion": int,
    "protocolVersion": int,
    "runId": str,
    "issueSnapshotHash": str,
    "repositoryIdentity": str,
    "repositoryRoot": str,
    "branch": str,
    "baseSha": str,
    "head": str,
    "commitRange": list,
    "reflog": list,
    "statusShort": str,
    "stagedDiffSha256": str,
    "unstagedDiffSha256": str,
    "untrackedFiles": list,
    "remoteBranchOid": (str, type(None)),
    "pullRequests": list,
    "capturedAt": str,
}

LIFECYCLE_STAGES = {
    "preflight_failed",
    "spawn_attempted",
    "spawn_failed",
    "spawned",
    "running",
    "exited",
    "output_fsync_done",
    "artifact_publish_failed",
    "artifact_published",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    if path.exists():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def command_hash(command: list[str]) -> str:
    return sha256_bytes(b"\0".join(argument.encode() for argument in command))


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _schema_errors(value: dict[str, Any], fields: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    missing = sorted(set(fields) - set(value))
    unknown = sorted(set(value) - set(fields))
    if missing:
        errors.append(f"{label}: missing fields: {', '.join(missing)}")
    if unknown:
        errors.append(f"{label}: unknown fields: {', '.join(unknown)}")
    for field, expected_type in fields.items():
        if field in value and not isinstance(value[field], expected_type):
            errors.append(f"{label}: {field} has invalid type")
    return errors


def validate_state_schema(state: dict[str, Any]) -> list[str]:
    errors = _schema_errors(state, STATE_FIELDS, "state")
    if state.get("schemaVersion") != STATE_SCHEMA_VERSION:
        errors.append("state: unsupported schemaVersion")
    if state.get("protocolVersion") != PROTOCOL_VERSION:
        errors.append("state: unsupported protocolVersion")
    for field in ("runId", "issueUrl", "repositoryIdentity", "repositoryRoot", "branch", "currentPhase"):
        if field in state and not state[field]:
            errors.append(f"state: {field} must not be empty")
    if isinstance(state.get("stateGeneration"), int) and state["stateGeneration"] < 1:
        errors.append("state: stateGeneration must be positive")
    if isinstance(state.get("fixCount"), int) and state["fixCount"] < 0:
        errors.append("state: fixCount must not be negative")
    for field in ("baseSha", "currentHead"):
        if isinstance(state.get(field), str) and not SHA_RE.fullmatch(state[field]):
            errors.append(f"state: {field} is not a Git object ID")
    for field in ("issueSnapshotHash", "expectedWorkingTreeFingerprint"):
        if isinstance(state.get(field), str) and not HASH_RE.fullmatch(state[field]):
            errors.append(f"state: {field} is not a SHA-256")
    command_digest = state.get("workerCommandHash")
    if command_digest is not None and (
        not isinstance(command_digest, str) or not HASH_RE.fullmatch(command_digest)
    ):
        errors.append("state: workerCommandHash is not a SHA-256 or null")
    return errors


def validate_baseline_schema(baseline: dict[str, Any]) -> list[str]:
    errors = _schema_errors(baseline, BASELINE_FIELDS, "baseline")
    if baseline.get("schemaVersion") != BASELINE_SCHEMA_VERSION:
        errors.append("baseline: unsupported schemaVersion")
    if baseline.get("protocolVersion") != PROTOCOL_VERSION:
        errors.append("baseline: unsupported protocolVersion")
    for field in ("runId", "repositoryIdentity", "repositoryRoot", "branch"):
        if field in baseline and not baseline[field]:
            errors.append(f"baseline: {field} must not be empty")
    for field in ("baseSha", "head"):
        if isinstance(baseline.get(field), str) and not SHA_RE.fullmatch(baseline[field]):
            errors.append(f"baseline: {field} is not a Git object ID")
    for field in ("issueSnapshotHash", "stagedDiffSha256", "unstagedDiffSha256"):
        if isinstance(baseline.get(field), str) and not HASH_RE.fullmatch(baseline[field]):
            errors.append(f"baseline: {field} is not a SHA-256")
    oid = baseline.get("remoteBranchOid")
    if oid is not None and (not isinstance(oid, str) or not SHA_RE.fullmatch(oid)):
        errors.append("baseline: remoteBranchOid is not a Git object ID or null")
    for item in baseline.get("untrackedFiles", []):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            errors.append("baseline: untrackedFiles entry has invalid schema")
            break
    return errors


def validate_lifecycle_schema(lifecycle: dict[str, Any]) -> list[str]:
    required = {
        "schemaVersion", "protocolVersion", "runId", "stage", "spawnAttempted",
        "workerSpawned", "artifactPublished", "workerPid", "processIdentity",
        "knownDescendantIdentities", "workerExitStatus", "outputSha256",
        "errorCode", "errors", "updatedAt",
    }
    errors: list[str] = []
    if set(lifecycle) != required:
        errors.append("lifecycle: schema fields do not match")
    if lifecycle.get("schemaVersion") != LIFECYCLE_SCHEMA_VERSION:
        errors.append("lifecycle: unsupported schemaVersion")
    if lifecycle.get("protocolVersion") != PROTOCOL_VERSION:
        errors.append("lifecycle: unsupported protocolVersion")
    if lifecycle.get("stage") not in LIFECYCLE_STAGES:
        errors.append("lifecycle: invalid stage")
    return errors


def process_identity(pid: int) -> dict[str, Any] | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
        capture_output=True, text=True, check=False,
    )
    parts = result.stdout.strip().split()
    if result.returncode != 0 or len(parts) < 6:
        return None
    return {"pid": pid, "startToken": " ".join(parts[:5]), "command": " ".join(parts[5:])}


def identity_is_live(identity: dict[str, Any] | None) -> bool:
    if not identity or not isinstance(identity.get("pid"), int):
        return False
    current = process_identity(identity["pid"])
    return bool(current and current == identity)


def descendant_identities(root_pid: int) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart=,comm="],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    children: dict[int, list[int]] = {}
    identities: dict[int, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        pid, parent = int(parts[0]), int(parts[1])
        children.setdefault(parent, []).append(pid)
        identities[pid] = {
            "pid": pid, "startToken": " ".join(parts[2:7]), "command": " ".join(parts[7:])
        }
    found, pending = [], list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid in identities:
            found.append(identities[pid])
        pending.extend(children.get(pid, []))
    return sorted(found, key=lambda item: item["pid"])


def any_identity_is_live(identity: dict[str, Any] | None, descendants: list[dict[str, Any]]) -> bool:
    return identity_is_live(identity) or any(identity_is_live(item) for item in descendants)


def git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def working_tree_fingerprint(repository: Path) -> str:
    digest = hashlib.sha256()
    for arguments in (
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("diff", "--binary"),
        ("diff", "--cached", "--binary"),
    ):
        digest.update(b"\0".join(part.encode() for part in arguments) + b"\0")
        digest.update(git(repository, *arguments))
    status = git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    for entry in status.split(b"\0"):
        if entry.startswith(b"?? "):
            relative = entry[3:].decode(errors="surrogateescape")
            path = repository / relative
            if path.is_file():
                digest.update(relative.encode(errors="surrogateescape"))
                digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def new_run_errors(repository: Path, branch: str) -> list[str]:
    errors = []
    if git(repository, "status", "--porcelain=v1", "-z"):
        errors.append("new run requires a clean working tree")
    local = subprocess.run(
        ["git", "-C", str(repository), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    if local.returncode == 0:
        errors.append("new run branch already exists")
    return errors


def resume_state_errors(run_dir: Path, repository: Path) -> list[str]:
    try:
        state = load_json(run_dir / "state.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"state: cannot load: {error}"]
    errors = validate_state_schema(state)
    snapshot = run_dir / "issue-snapshot.json"
    expected = {
        "repositoryRoot": str(repository.resolve()),
        "currentHead": git(repository, "rev-parse", "HEAD").decode().strip(),
        "expectedWorkingTreeFingerprint": working_tree_fingerprint(repository),
        "issueSnapshotHash": sha256_file(snapshot),
        "branch": git(repository, "branch", "--show-current").decode().strip(),
    }
    for field, actual in expected.items():
        if state.get(field) != actual:
            errors.append(f"resume state mismatch: {field}")
    return errors


def pre_launch_errors(run_dir: Path, repository: Path) -> list[str]:
    errors: list[str] = []
    try:
        state = load_json(run_dir / "state.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"state: cannot load: {error}"]
    errors.extend(validate_state_schema(state))
    snapshot_path = run_dir / "issue-snapshot.json"
    if not snapshot_path.is_file():
        errors.append("snapshot: issue-snapshot.json is missing")
    elif state.get("issueSnapshotHash") != sha256_file(snapshot_path):
        errors.append("snapshot: issueSnapshotHash mismatch")
    baseline_path = run_dir / "worker-baseline.json"
    try:
        baseline = load_json(baseline_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"baseline: cannot load: {error}")
        baseline = {}
    errors.extend(validate_baseline_schema(baseline))
    for field in ("runId", "issueSnapshotHash", "repositoryIdentity", "repositoryRoot", "branch", "baseSha"):
        if baseline.get(field) != state.get(field):
            errors.append(f"baseline/state mismatch: {field}")
    current = {
        "repositoryRoot": str(repository.resolve()),
        "branch": git(repository, "branch", "--show-current").decode().strip(),
        "currentHead": git(repository, "rev-parse", "HEAD").decode().strip(),
        "expectedWorkingTreeFingerprint": working_tree_fingerprint(repository),
    }
    for field, actual in current.items():
        if state.get(field) != actual:
            errors.append(f"pre-launch repository mismatch: {field}")
    if baseline.get("head") != current["currentHead"]:
        errors.append("baseline/repository mismatch: head")
    if state.get("workerProcessIdentity") is not None and identity_is_live(state["workerProcessIdentity"]):
        errors.append("conflict: recorded worker is still live")
    if any(identity_is_live(item) for item in state.get("knownDescendantIdentities", [])):
        errors.append("conflict: recorded descendant is still live")
    for name in ("worker-exit.json", "worker-output.log", "worker-lifecycle.json"):
        if (run_dir / name).exists():
            errors.append(f"conflict: {name} already exists")
    return errors


def update_state(
    run_dir: Path, *, expected_generation: int | None = None, **updates: Any
) -> dict[str, Any]:
    state_path = run_dir / "state.json"
    state = load_json(state_path)
    if expected_generation is not None and state.get("stateGeneration") != expected_generation:
        raise RuntimeError("state generation changed unexpectedly")
    state.update(updates)
    state["stateGeneration"] = int(state.get("stateGeneration", 0)) + 1
    state["latestStateUpdateTime"] = utc_now()
    errors = validate_state_schema(state)
    if errors:
        raise ValueError("; ".join(errors))
    atomic_write_json(state_path, state)
    return state


def lifecycle_record(
    run_id: str | None, stage: str, **updates: Any
) -> dict[str, Any]:
    value = {
        "schemaVersion": LIFECYCLE_SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "runId": run_id,
        "stage": stage,
        "spawnAttempted": False,
        "workerSpawned": False,
        "artifactPublished": False,
        "workerPid": None,
        "processIdentity": None,
        "knownDescendantIdentities": [],
        "workerExitStatus": "unknown",
        "outputSha256": None,
        "errorCode": None,
        "errors": [],
        "updatedAt": utc_now(),
    }
    value.update(updates)
    value["updatedAt"] = utc_now()
    errors = validate_lifecycle_schema(value)
    if errors:
        raise ValueError("; ".join(errors))
    return value


def validate_exit_artifact(
    artifact: dict[str, Any], state: dict[str, Any], output_path: Path
) -> list[str]:
    errors: list[str] = []
    required = {
        "protocolVersion", "runId", "pid", "processIdentity", "startedAt", "finishedAt",
        "exitCode", "commandHash", "outputSha256", "launchStateGeneration",
    }
    if set(artifact) != required:
        errors.append("exit artifact schema is invalid")
    if artifact.get("protocolVersion") != PROTOCOL_VERSION:
        errors.append("protocol version mismatch")
    if artifact.get("runId") != state.get("runId"):
        errors.append("run ID mismatch")
    if artifact.get("commandHash") != state.get("workerCommandHash"):
        errors.append("command hash mismatch")
    if artifact.get("processIdentity") != state.get("workerProcessIdentity"):
        errors.append("process identity mismatch")
    identity = artifact.get("processIdentity")
    if not isinstance(artifact.get("pid"), int) or not isinstance(identity, dict):
        errors.append("process identity schema is invalid")
    elif identity.get("pid") != artifact.get("pid"):
        errors.append("process identity fields are invalid")
    if not isinstance(artifact.get("exitCode"), int):
        errors.append("exit code is not an integer")
    if artifact.get("outputSha256") != sha256_file(output_path):
        errors.append("output hash mismatch")
    return errors
