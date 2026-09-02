#!/usr/bin/env python3
"""Durable, fail-closed push reconciliation primitives for ht-issue-loop."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from worker_protocol import command_hash, identity_is_live, sha256_file, working_tree_fingerprint

PUSH_PROTOCOL_VERSION = 1


def git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, text=True, check=False
    )


def remote_oid(repository: Path, remote: str, branch: str) -> str | None:
    result = git(repository, "ls-remote", "--heads", remote, branch)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git ls-remote failed")
    line = result.stdout.strip()
    return line.split()[0] if line else None


def upstream(repository: Path) -> str | None:
    result = git(repository, "rev-parse", "@{upstream}")
    return result.stdout.strip() if result.returncode == 0 else None


def upstream_ref(repository: Path) -> str | None:
    result = git(repository, "rev-parse", "--abbrev-ref", "@{upstream}")
    return result.stdout.strip() if result.returncode == 0 else None


def repository_snapshot(repository: Path, remote: str, branch: str, pr_state: Any) -> dict[str, Any]:
    status = git(repository, "status", "--porcelain")
    head = git(repository, "rev-parse", "HEAD")
    current_branch = git(repository, "branch", "--show-current")
    if any(item.returncode for item in (status, head, current_branch)):
        raise RuntimeError("could not capture local push baseline")
    return {
        "branch": current_branch.stdout.strip(),
        "head": head.stdout.strip(),
        "statusPorcelain": status.stdout,
        "treeFingerprint": working_tree_fingerprint(repository),
        "upstreamRef": upstream_ref(repository),
        "upstreamOid": upstream(repository),
        "remoteOid": remote_oid(repository, remote, branch),
        "prState": pr_state,
    }


def command_safety_errors(command: list[str]) -> list[str]:
    errors: list[str] = []
    if command[:2] != ["git", "push"]:
        errors.append("command must be a direct git push invocation")
    prohibited = {"--no-verify", "--force", "-f", "--force-with-lease", "nohup", "&"}
    for token in command:
        if token in prohibited or token.startswith("--force-with-lease="):
            errors.append(f"prohibited push token: {token}")
        if token.startswith("+") and token != "+":
            errors.append("leading + force refspec is prohibited")
        if token.endswith("&"):
            errors.append("nested or trailing background execution is prohibited")
    joined = " ".join(command).lower()
    for marker in ("core.hookspath", "hooks/pre-push", "pre-push.sample"):
        if marker in joined:
            errors.append("hook deletion, replacement, or configuration is prohibited")
    if command[:2] == ["git", "push"]:
        arguments = command[2:]
        positional = [item for item in arguments if item not in {"--set-upstream", "-u"}]
        unknown_options = [item for item in positional if item.startswith("-")]
        if unknown_options:
            errors.append("only --set-upstream/-u is allowed on an issue-loop push")
        elif len(positional) != 2 or positional[0] != "origin" or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]*", positional[1]
        ) or ":" in positional[1]:
            errors.append("push must target exactly origin and the current branch name")
    return sorted(set(errors))


def validate_exit_artifact(
    artifact: dict[str, Any], metadata: dict[str, Any], stdout_path: Path, stderr_path: Path
) -> list[str]:
    required = {
        "protocolVersion", "runId", "commandHash", "processIdentity", "exitCode",
        "stdoutSha256", "stderrSha256", "startedAt", "finishedAt",
    }
    errors: list[str] = []
    if set(artifact) != required:
        errors.append("exit artifact schema is invalid")
    for field in ("runId", "commandHash", "processIdentity"):
        if artifact.get(field) != metadata.get(field):
            errors.append(f"{field.replace('I', ' i').replace('H', ' h').lower()} mismatch")
    if artifact.get("protocolVersion") != PUSH_PROTOCOL_VERSION:
        errors.append("protocol version mismatch")
    if not isinstance(artifact.get("exitCode"), int):
        errors.append("exit code is not an integer")
    if artifact.get("stdoutSha256") != sha256_file(stdout_path):
        errors.append("stdout hash mismatch")
    if artifact.get("stderrSha256") != sha256_file(stderr_path):
        errors.append("stderr hash mismatch")
    return errors


def retry_errors(baseline: dict[str, Any], current: dict[str, Any], processes_quiescent: bool) -> list[str]:
    errors = []
    if not processes_quiescent:
        errors.append("previous push process tree is live or indeterminate")
    for field in ("branch", "head", "treeFingerprint", "upstreamRef", "upstreamOid", "prState"):
        if current.get(field) != baseline.get(field):
            errors.append(f"retry baseline changed: {field}")
    before, after = baseline.get("remoteOid"), current.get("remoteOid")
    if after != before:
        errors.append("retry baseline changed: remoteOid")
    return errors


def classify(
    *, artifact_errors: list[str], exit_code: int | None, process_tree_quiescent: bool,
    outputs_stable: bool, status_porcelain: str, head: str, upstream_oid: str | None,
    remote_branch_oid: str | None, hook_bypassed: bool = False,
) -> tuple[str, list[str]]:
    reasons = list(artifact_errors)
    if not process_tree_quiescent:
        reasons.append("push process tree has not reached termination and quiescence")
    if not outputs_stable:
        reasons.append("stdout/stderr observations are not stable")
    if exit_code is None:
        reasons.append("durable numeric exit result is unavailable")
    if hook_bypassed:
        reasons.append("pre-push hook was bypassed")
    if reasons:
        return "indeterminate", reasons
    if exit_code != 0:
        return "failed", [f"push exited with numeric status {exit_code}"]
    postcondition_errors = []
    if status_porcelain:
        postcondition_errors.append("working tree is not clean")
    if upstream_oid is None:
        postcondition_errors.append("upstream is not configured")
    elif upstream_oid != head:
        postcondition_errors.append("upstream OID differs from HEAD")
    if remote_branch_oid != head:
        postcondition_errors.append("remote branch OID differs from HEAD")
    return ("confirmed", []) if not postcondition_errors else ("failed", postcondition_errors)


def process_tree_quiescent(metadata: dict[str, Any]) -> bool:
    identities = [metadata.get("processIdentity"), *metadata.get("knownDescendantIdentities", [])]
    return all(not identity_is_live(item) for item in identities)
