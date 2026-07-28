#!/usr/bin/env python3
"""Shared durable-state helpers for omh-issue-loop worker processes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    if path.exists():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def command_hash(command: list[str]) -> str:
    digest = hashlib.sha256()
    for argument in command:
        digest.update(argument.encode())
        digest.update(b"\0")
    return digest.hexdigest()


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


def process_identity(pid: int) -> dict[str, Any] | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
        capture_output=True,
        text=True,
        check=False,
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    parts = line.split()
    return {"pid": pid, "startToken": " ".join(parts[:5]), "command": " ".join(parts[5:])}


def identity_is_live(identity: dict[str, Any] | None) -> bool:
    if not identity or not isinstance(identity.get("pid"), int):
        return False
    current = process_identity(identity["pid"])
    return bool(
        current
        and current.get("startToken") == identity.get("startToken")
        and current.get("command") == identity.get("command")
    )


def descendant_identities(root_pid: int) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart=,comm="],
        capture_output=True,
        text=True,
        check=False,
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
            "pid": pid,
            "startToken": " ".join(parts[2:7]),
            "command": " ".join(parts[7:]),
        }
    found: list[dict[str, Any]] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid in identities:
            found.append(identities[pid])
        pending.extend(children.get(pid, []))
    return sorted(found, key=lambda item: item["pid"])


def any_identity_is_live(
    worker_identity: dict[str, Any] | None, descendants: list[dict[str, Any]]
) -> bool:
    return identity_is_live(worker_identity) or any(identity_is_live(item) for item in descendants)


def git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
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
        digest.update(b"\0".join(part.encode() for part in arguments))
        digest.update(b"\0")
        digest.update(git(repository, *arguments))
    status = git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = status.split(b"\0")
    for entry in entries:
        if entry.startswith(b"?? "):
            relative = entry[3:].decode(errors="surrogateescape")
            path = repository / relative
            if path.is_file():
                digest.update(relative.encode(errors="surrogateescape"))
                digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def new_run_errors(repository: Path, branch: str) -> list[str]:
    errors: list[str] = []
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
    state = load_json(run_dir / "state.json")
    errors: list[str] = []
    snapshot = run_dir / "issue-snapshot.json"
    expected = {
        "repositoryRoot": str(repository.resolve()),
        "currentHead": git(repository, "rev-parse", "HEAD").decode().strip(),
        "expectedWorkingTreeFingerprint": working_tree_fingerprint(repository),
        "issueSnapshotHash": sha256_file(snapshot),
    }
    for field, actual in expected.items():
        if state.get(field) != actual:
            errors.append(f"resume state mismatch: {field}")
    branch = git(repository, "branch", "--show-current").decode().strip()
    if state.get("branch") != branch:
        errors.append("resume state mismatch: branch")
    return errors


def update_state(run_dir: Path, **updates: Any) -> dict[str, Any]:
    state_path = run_dir / "state.json"
    state = load_json(state_path)
    state.update(updates)
    state["latestStateUpdateTime"] = utc_now()
    atomic_write_json(state_path, state)
    return state


def validate_exit_artifact(
    artifact: dict[str, Any], state: dict[str, Any], output_path: Path
) -> list[str]:
    errors: list[str] = []
    required = {
        "protocolVersion",
        "runId",
        "pid",
        "processIdentity",
        "startedAt",
        "finishedAt",
        "exitCode",
        "commandHash",
        "outputSha256",
    }
    if not required.issubset(artifact):
        errors.append("exit artifact schema is incomplete")
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
    elif (
        identity.get("pid") != artifact.get("pid")
        or not isinstance(identity.get("startToken"), str)
        or not isinstance(identity.get("command"), str)
    ):
        errors.append("process identity fields are invalid")
    if not isinstance(artifact.get("exitCode"), int):
        errors.append("exit code is not an integer")
    if artifact.get("outputSha256") != sha256_file(output_path):
        errors.append("output hash mismatch")
    return errors
