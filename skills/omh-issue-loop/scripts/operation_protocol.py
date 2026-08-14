#!/usr/bin/env python3
"""Durable state primitives shared by bounded push and reviewer operations."""

from __future__ import annotations

import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from worker_protocol import HASH_RE, SHA_RE, atomic_write_json, load_json, process_identity, utc_now

OPERATION_STATE_VERSION = 1
RECOVERY_REASON = "lost_or_unprocessed_completion_notification"
PHASES = {
    "push": {"push_launch_pending", "push_running", "push_artifact_published", "push_reconciled"},
    "reviewer": {
        "reviewer_launch_pending", "reviewer_running", "reviewer_artifact_published",
        "reviewer_reconciled",
    },
}
TRANSITIONS = {
    "push_launch_pending": {"push_launch_pending", "push_running"},
    "push_running": {"push_running", "push_artifact_published", "push_reconciled"},
    "push_artifact_published": {"push_artifact_published", "push_reconciled"},
    "push_reconciled": {"push_reconciled"},
    "reviewer_launch_pending": {"reviewer_launch_pending", "reviewer_running"},
    "reviewer_running": {
        "reviewer_running", "reviewer_artifact_published", "reviewer_reconciled",
    },
    "reviewer_artifact_published": {"reviewer_artifact_published", "reviewer_reconciled"},
    "reviewer_reconciled": {"reviewer_reconciled"},
}


class GatedProcess:
    """Minimal Popen-compatible handle for a forked child released after identity publication."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        if waited:
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self) -> int:
        if self.returncode is None:
            _, status = os.waitpid(self.pid, 0)
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def terminate(self) -> None:
        os.killpg(self.pid, signal.SIGTERM)


def spawn_gated(
    command: list[str],
    *,
    cwd: Path,
    stdout: BinaryIO,
    stderr: BinaryIO,
    publish_identity: Callable[[dict[str, Any]], None],
) -> GatedProcess:
    """Fork a blocked child, publish its identity durably, then permit exec."""
    read_fd, write_fd = os.pipe()
    ready_read_fd, ready_write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(write_fd)
            os.close(ready_read_fd)
            os.setsid()
            os.write(ready_write_fd, b"1")
            os.close(ready_write_fd)
            token = os.read(read_fd, 1)
            os.close(read_fd)
            if token != b"1":
                os._exit(125)
            os.chdir(cwd)
            os.dup2(stdout.fileno(), 1)
            os.dup2(stderr.fileno(), 2)
            os.execvp(command[0], command)
        except BaseException:
            os._exit(126)
    os.close(read_fd)
    os.close(ready_write_fd)
    process = GatedProcess(pid)
    try:
        if os.read(ready_read_fd, 1) != b"1":
            raise RuntimeError("gated process did not establish its process group")
        os.close(ready_read_fd)
        identity = process_identity(pid)
        if identity is None:
            raise RuntimeError("could not capture gated process identity")
        publish_identity(identity)
        os.write(write_fd, b"1")
    except BaseException:
        try:
            os.close(ready_read_fd)
        except OSError:
            pass
        os.close(write_fd)
        process.wait()
        raise
    os.close(write_fd)
    return process


def initial_state(
    *,
    operation_kind: str,
    attempt_id: str,
    command_hash: str,
    target_sha: str,
    expected_artifact_paths: dict[str, str],
    deadline: str | None,
    resume_after_completion: str,
) -> dict[str, Any]:
    state = {
        "schemaVersion": OPERATION_STATE_VERSION,
        "operationKind": operation_kind,
        "attemptId": attempt_id,
        "phase": f"{operation_kind}_launch_pending",
        "commandHash": command_hash,
        "targetSha": target_sha,
        "expectedArtifactPaths": expected_artifact_paths,
        "deadline": deadline,
        "resumeAfterCompletion": resume_after_completion,
        "processIdentity": None,
        "knownDescendantIdentities": [],
        "artifactFinishedAt": None,
        "recoveryReason": None,
        "recoveredAt": None,
        "resumedFromPhase": None,
        "updatedAt": utc_now(),
    }
    errors = state_errors(state, operation_kind)
    if errors:
        raise ValueError("; ".join(errors))
    return state


def state_errors(state: dict[str, Any], operation_kind: str) -> list[str]:
    required = {
        "schemaVersion", "operationKind", "attemptId", "phase", "commandHash", "targetSha",
        "expectedArtifactPaths", "deadline", "resumeAfterCompletion", "processIdentity",
        "knownDescendantIdentities", "artifactFinishedAt", "recoveryReason", "recoveredAt",
        "resumedFromPhase", "updatedAt",
    }
    errors = []
    if set(state) != required:
        errors.append("operation state schema fields do not match")
    if state.get("schemaVersion") != OPERATION_STATE_VERSION:
        errors.append("operation state schema version mismatch")
    if state.get("operationKind") != operation_kind:
        errors.append("operation kind mismatch")
    if not isinstance(state.get("attemptId"), str) or not state.get("attemptId"):
        errors.append("attempt ID is missing")
    if not isinstance(state.get("expectedArtifactPaths"), dict):
        errors.append("expected artifact paths are invalid")
    if not isinstance(state.get("commandHash"), str) or not HASH_RE.fullmatch(state.get("commandHash", "")):
        errors.append("operation command hash is invalid")
    if not isinstance(state.get("targetSha"), str) or not SHA_RE.fullmatch(state.get("targetSha", "")):
        errors.append("operation target SHA is invalid")
    if not isinstance(state.get("resumeAfterCompletion"), str) or not state.get("resumeAfterCompletion"):
        errors.append("resume-after-completion action is missing")
    if not isinstance(state.get("knownDescendantIdentities"), list):
        errors.append("known descendant identities are invalid")
    if state.get("phase") not in PHASES.get(operation_kind, set()):
        errors.append("operation phase is invalid")
    for field in ("deadline", "artifactFinishedAt", "recoveredAt", "updatedAt"):
        value = state.get(field)
        if value is not None:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                errors.append(f"operation {field} timestamp is invalid")
    return errors


def binding_errors(
    state: dict[str, Any],
    metadata: dict[str, Any],
    *,
    target_sha: str,
    expected_artifact_paths: dict[str, str],
) -> list[str]:
    errors = state_errors(state, state.get("operationKind", ""))
    if state.get("commandHash") != metadata.get("commandHash"):
        errors.append("operation state command hash mismatch")
    for state_field, metadata_field in (
        ("attemptId", "attemptId"),
        ("deadline", "deadline"),
        ("resumeAfterCompletion", "resumeAfterCompletion"),
    ):
        if state.get(state_field) != metadata.get(metadata_field):
            errors.append(f"operation state {state_field} mismatch")
    if state.get("processIdentity") != metadata.get("processIdentity"):
        errors.append("operation state process identity mismatch")
    state_descendants = state.get("knownDescendantIdentities", [])
    metadata_descendants = metadata.get("knownDescendantIdentities", [])
    if any(item not in metadata_descendants for item in state_descendants):
        errors.append("operation state has an unbound descendant identity")
    if state.get("targetSha") != target_sha:
        errors.append("operation state target SHA mismatch")
    if state.get("expectedArtifactPaths") != expected_artifact_paths:
        errors.append("operation state artifact paths mismatch")
    return errors


def update_state(path: Path, operation_kind: str, **changes: Any) -> dict[str, Any]:
    state = load_json(path)
    errors = state_errors(state, operation_kind)
    if errors:
        raise ValueError("; ".join(errors))
    next_phase = changes.get("phase", state["phase"])
    if next_phase not in TRANSITIONS.get(state["phase"], set()):
        raise ValueError(f"invalid operation phase transition: {state['phase']} -> {next_phase}")
    state.update(changes, updatedAt=utc_now())
    errors = state_errors(state, operation_kind)
    if errors:
        raise ValueError("; ".join(errors))
    atomic_write_json(path, state)
    return state


def recovery_updates(state: dict[str, Any], finished_at: str, *, threshold: float = 300) -> dict[str, Any]:
    try:
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        ).total_seconds()
    except (AttributeError, ValueError):
        return {}
    operation_kind = state.get("operationKind")
    recoverable_phases = {
        f"{operation_kind}_running",
        f"{operation_kind}_artifact_published",
        "pending_completion",
        "pending_reconciliation",
    }
    if age <= threshold or state.get("phase") not in recoverable_phases:
        return {}
    return {
        "recoveryReason": RECOVERY_REASON,
        "artifactFinishedAt": finished_at,
        "recoveredAt": utc_now(),
        "resumedFromPhase": state["phase"],
    }
