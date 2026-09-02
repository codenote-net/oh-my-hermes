#!/usr/bin/env python3
"""Fail-closed reviewer artifact reconciliation primitives."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from worker_protocol import identity_is_live, sha256_file

REVIEW_PROTOCOL_VERSION = 2
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
HIGH_COUNT_RE = re.compile(r"(?im)^\s*(?:high(?:[- ]priority)?(?:\s+findings?)?|critical\s+findings?)\s*[:=]\s*(\d+)\s*$")
COMMAND_RE = re.compile(r"(?im)^\s*(?:command|cmd)\s*[:=]\s*(\S.*)$")
STATUS_RE = re.compile(r"(?im)^\s*(?:exit(?:\s+status|\s+code)?|status)\s*[:=]\s*(-?\d+)\s*$")


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def common_git_config_hash(repository: Path) -> str:
    values = []
    for key in ("user.name", "user.email", "commit.gpgsign", "gpg.format", "core.hookspath", "remote.origin.url"):
        result = subprocess.run(
            ["git", "-C", str(repository), "config", "--local", "--get-all", key],
            capture_output=True, text=True,
        )
        values.append(f"{key}\0{result.returncode}\0{result.stdout}")
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def repository_observation(repository: Path) -> tuple[str, str, str]:
    return (
        git(repository, "rev-parse", "HEAD").strip(),
        git(repository, "status", "--porcelain=v1", "--untracked-files=all"),
        common_git_config_hash(repository),
    )


def process_state(identity: dict[str, Any] | None) -> str:
    if not identity or not isinstance(identity.get("pid"), int):
        return "missing"
    result = subprocess.run(
        ["ps", "-p", str(identity["pid"]), "-o", "state=", "-o", "lstart=", "-o", "comm="],
        capture_output=True, text=True,
    )
    if result.returncode or not result.stdout.strip():
        return "stopped"
    if result.stdout.lstrip().startswith("Z"):
        return "zombie"
    return "live" if identity_is_live(identity) else "stopped"


def artifact_errors(
    artifact: dict[str, Any],
    metadata: dict[str, Any],
    baseline_sha256: str,
    stdout: Path,
    stderr: Path,
) -> list[str]:
    required = {"protocolVersion", "commandHash", "processIdentity", "targetSha", "baselineSha256", "exitCode", "stdoutSha256", "stderrSha256", "startedAt", "finishedAt"}
    errors = []
    if set(artifact) != required:
        errors.append("exit artifact schema is invalid")
    if artifact.get("protocolVersion") != REVIEW_PROTOCOL_VERSION:
        errors.append("protocol version mismatch")
    for field in ("commandHash", "processIdentity", "targetSha", "baselineSha256"):
        if artifact.get(field) != metadata.get(field):
            errors.append(f"{field} mismatch")
    if artifact.get("baselineSha256") != baseline_sha256:
        errors.append("baseline hash mismatch")
    if not isinstance(artifact.get("exitCode"), int):
        errors.append("exit status is not an integer")
    if artifact.get("stdoutSha256") != sha256_file(stdout):
        errors.append("stdout hash mismatch")
    if artifact.get("stderrSha256") != sha256_file(stderr):
        errors.append("stderr hash mismatch")
    return errors


def report_errors(report: str, require_command_evidence: bool) -> list[str]:
    errors = []
    if len(report.strip()) < 20:
        errors.append("review report is blank or not substantive")
    if not HIGH_COUNT_RE.search(report):
        errors.append("explicit high-priority finding count is missing")
    if require_command_evidence and (not COMMAND_RE.search(report) or not STATUS_RE.search(report)):
        errors.append("exact command and numeric status evidence is missing")
    return errors
