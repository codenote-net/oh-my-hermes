#!/usr/bin/env python3
"""Intent-to-add normalization and strict repository fingerprints for local reviews."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from worker_protocol import atomic_write_json, sha256_file, utc_now

NORMALIZATION_SCHEMA_VERSION = 1
NORMALIZATION_TYPE = "orchestrator_intent_to_add"
EMPTY_BLOB_OID = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
PR_FIELDS = (
    "number,url,title,body,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,"
    "mergeStateStatus,reviewDecision,labels,assignees,milestone,author,createdAt,updatedAt,"
    "closedAt,mergedAt"
)


def _run(command: list[str], *, cwd: Path | None = None) -> bytes:
    result = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(message or f"command failed ({result.returncode}): {command!r}")
    return result.stdout


def git(repository: Path, *arguments: str) -> bytes:
    return _run(["git", "-C", str(repository), *arguments])


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError(f"unsafe allowed path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe allowed path: {value!r}")
    return value


def load_allowed_paths(path: Path) -> list[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("paths")
    if not isinstance(value, list):
        raise ValueError("allowed paths JSON must be an array or an object with a paths array")
    paths = [_validate_relative_path(item) for item in value]
    if len(paths) != len(set(paths)):
        raise ValueError("allowed paths contain duplicates")
    return sorted(paths)


def status_entries(repository: Path) -> dict[str, dict[str, str]]:
    raw = git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tokens = raw.split(b"\0")
    entries: dict[str, dict[str, str]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise ValueError("could not safely classify porcelain status")
        xy = token[:2].decode("ascii")
        path = _decode(token[3:])
        entry = {"status": xy}
        if xy[0] in "RC" or xy[1] in "RC":
            if index >= len(tokens) or not tokens[index]:
                raise ValueError("could not safely classify renamed or copied path")
            entry["sourcePath"] = _decode(tokens[index])
            index += 1
        entries[path] = entry
    return dict(sorted(entries.items()))


def index_entries(repository: Path) -> dict[str, dict[str, str]]:
    raw = git(repository, "ls-files", "--stage", "-z")
    entries: dict[str, dict[str, str]] = {}
    for token in raw.split(b"\0"):
        if not token:
            continue
        metadata, separator, path_bytes = token.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ValueError("could not safely classify Git index entry")
        path = _decode(path_bytes)
        entries[path] = {
            "mode": fields[0].decode("ascii"),
            "oid": fields[1].decode("ascii"),
            "stage": fields[2].decode("ascii"),
        }
    return dict(sorted(entries.items()))


def _file_record(repository: Path, relative: str) -> dict[str, Any]:
    path = repository / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "kind": "missing", "mode": None, "sha256": None}
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        kind = "symlink"
    elif stat.S_ISREG(metadata.st_mode):
        content = path.read_bytes()
        kind = "file"
    else:
        raise ValueError(f"cannot safely hash non-file path: {relative}")
    return {"path": relative, "kind": kind, "mode": mode, "sha256": _sha256(content)}


def worktree_files(repository: Path) -> list[dict[str, Any]]:
    raw = git(repository, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    paths = sorted({_decode(item) for item in raw.split(b"\0") if item})
    return [_file_record(repository, path) for path in paths]


def _untracked_files(
    repository: Path, status: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    return [_file_record(repository, path) for path, entry in status.items() if entry["status"] == "??"]


def _intent_to_add_paths(
    status: dict[str, dict[str, str]], index: dict[str, dict[str, str]]
) -> list[str]:
    return sorted(
        path
        for path, entry in index.items()
        if entry["oid"] == EMPTY_BLOB_OID
        and entry["stage"] == "0"
        and status.get(path, {}).get("status") == " A"
    )


def _git_config_hash(repository: Path) -> str:
    listing = git(repository, "config", "--null", "--list", "--show-origin")
    paths = {
        _decode(git(repository, "rev-parse", "--git-path", "config")).strip(),
        _decode(git(repository, "rev-parse", "--git-path", "config.worktree")).strip(),
    }
    digest = hashlib.sha256(listing)
    for value in sorted(paths):
        path = Path(value)
        if not path.is_absolute():
            path = repository / path
        digest.update(value.encode(errors="surrogateescape") + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _remote_branch_oid(repository: Path, branch: str) -> str | None:
    output = git(repository, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    lines = [line for line in output.splitlines() if line]
    if not lines:
        return None
    if len(lines) != 1:
        raise ValueError("remote branch lookup was ambiguous")
    oid, separator, ref = lines[0].partition(b"\t")
    if not separator or _decode(ref) != f"refs/heads/{branch}":
        raise ValueError("remote branch lookup returned an unexpected ref")
    return oid.decode("ascii")


def _pull_request_snapshot(repository: Path, branch: str) -> list[dict[str, Any]]:
    listed = json.loads(
        _run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number"],
            cwd=repository,
        ).decode("utf-8")
    )
    if not isinstance(listed, list) or any(not isinstance(item, dict) or not isinstance(item.get("number"), int) for item in listed):
        raise ValueError("pull-request list has an unsafe shape")
    snapshots = []
    for number in sorted({item["number"] for item in listed}):
        value = json.loads(
            _run(
                ["gh", "pr", "view", str(number), "--json", PR_FIELDS],
                cwd=repository,
            ).decode("utf-8")
        )
        if not isinstance(value, dict) or value.get("number") != number:
            raise ValueError(f"pull-request snapshot {number} has an unsafe shape")
        snapshots.append(value)
    return snapshots


def capture_repository_state(repository: Path, issue_snapshot: Path) -> dict[str, Any]:
    repository = repository.resolve()
    head = _decode(git(repository, "rev-parse", "HEAD")).strip()
    branch = _decode(git(repository, "branch", "--show-current")).strip()
    if not branch:
        raise ValueError("review normalization requires an attached branch")
    status = status_entries(repository)
    index = index_entries(repository)
    return {
        "head": head,
        "objectFormat": _decode(git(repository, "rev-parse", "--show-object-format")).strip(),
        "branch": branch,
        "reflog": _decode(
            git(repository, "reflog", "show", "--format=%H%x00%gD%x00%gs", f"refs/heads/{branch}")
        ),
        "allReflogs": _decode(
            git(repository, "reflog", "show", "--all", "--format=%H%x00%gD%x00%gs")
        ),
        "refsSnapshot": _decode(
            git(repository, "for-each-ref", "--format=%(refname)%00%(objectname)")
        ),
        "statusPorcelain": _decode(
            git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        ),
        "statusEntries": status,
        "stagedDiffSha256": _sha256(
            git(repository, "diff", "--cached", "--binary", "--full-index")
        ),
        "unstagedDiffSha256": _sha256(
            git(repository, "diff", "--binary", "--full-index")
        ),
        "untrackedFiles": _untracked_files(repository, status),
        "intentToAddPaths": _intent_to_add_paths(status, index),
        "indexEntries": index,
        "worktreeFiles": worktree_files(repository),
        "gitConfigSha256": _git_config_hash(repository),
        "remoteBranchOid": _remote_branch_oid(repository, branch),
        "pullRequests": _pull_request_snapshot(repository, branch),
        "issueSnapshotSha256": sha256_file(issue_snapshot),
    }


def _outside_status_unchanged(
    before: dict[str, dict[str, str]], after: dict[str, dict[str, str]], paths: set[str]
) -> bool:
    keys = (set(before) | set(after)) - paths
    return all(before.get(path) == after.get(path) for path in keys)


def verification_errors(
    before: dict[str, Any], after: dict[str, Any], new_paths: list[str], all_paths: list[str]
) -> list[str]:
    errors: list[str] = []
    labels = {
        "head": "HEAD changed",
        "objectFormat": "Git object format changed",
        "branch": "branch changed",
        "reflog": "branch reflog changed",
        "allReflogs": "a Git reflog changed",
        "refsSnapshot": "a Git ref changed",
        "remoteBranchOid": "remote branch OID changed",
        "pullRequests": "pull-request snapshot changed",
        "issueSnapshotSha256": "immutable issue snapshot changed",
        "gitConfigSha256": "Git configuration changed",
    }
    for field, reason in labels.items():
        if before[field] != after[field]:
            errors.append(reason)
    if before["worktreeFiles"] != after["worktreeFiles"]:
        errors.append("working-tree content or file mode changed")
    if before["stagedDiffSha256"] != after["stagedDiffSha256"]:
        errors.append("staged diff changed or real staged content was introduced")
    if not _outside_status_unchanged(before["statusEntries"], after["statusEntries"], set(new_paths)):
        errors.append("status outside the normalization set changed")
    for path, entry in before["indexEntries"].items():
        if after["indexEntries"].get(path) != entry:
            errors.append(f"existing index entry changed: {path}")
    new_index_paths = set(after["indexEntries"]) - set(before["indexEntries"])
    if new_index_paths != set(new_paths):
        errors.append("index paths outside the normalization set changed")
    for path in all_paths:
        entry = after["indexEntries"].get(path)
        if entry is None or entry.get("oid") != EMPTY_BLOB_OID or entry.get("stage") != "0":
            errors.append(f"path is not an empty-blob intent-to-add entry: {path}")
        if after["statusEntries"].get(path, {}).get("status") != " A":
            errors.append(f"path has staged content instead of intent-to-add: {path}")
    if sorted(after["intentToAddPaths"]) != sorted(all_paths):
        errors.append("only validated paths may have intent-to-add entries")
    return errors


def _artifact_path(run_dir: Path, round_id: str) -> Path:
    safe = _validate_relative_path(round_id)
    if "/" in safe:
        raise ValueError("round ID must not contain path separators")
    return run_dir.resolve() / "review-normalizations" / f"{safe}.json"


def _publish_failure(path: Path, round_id: str, reasons: list[str], before: dict[str, Any] | None) -> None:
    if path.exists():
        return
    atomic_write_json(
        path,
        {
            "schemaVersion": NORMALIZATION_SCHEMA_VERSION,
            "normalizationType": NORMALIZATION_TYPE,
            "roundId": round_id,
            "classification": "failed",
            "reasons": reasons,
            "before": before,
            "publishedAt": utc_now(),
        },
    )


def normalize_review_candidate(
    *, run_dir: Path, repository: Path, allowed_paths_json: Path, round_id: str
) -> dict[str, Any]:
    repository = repository.resolve()
    issue_snapshot = run_dir.resolve() / "issue-snapshot.json"
    if not issue_snapshot.is_file():
        raise ValueError("existing immutable issue-snapshot.json is required")
    artifact_path = _artifact_path(run_dir, round_id)
    before_path = artifact_path.with_suffix(".before.json")
    lock_path = artifact_path.with_suffix(".lock")
    artifact_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        lock_path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ValueError(f"normalization round is already claimed: {round_id}") from error
    directory_fd = os.open(artifact_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if artifact_path.exists() or before_path.exists():
        raise ValueError(f"normalization evidence already exists for round: {round_id}")
    allowed_paths = load_allowed_paths(allowed_paths_json)
    before = capture_repository_state(repository, issue_snapshot)
    atomic_write_json(
        before_path,
        {
            "schemaVersion": NORMALIZATION_SCHEMA_VERSION,
            "normalizationType": NORMALIZATION_TYPE,
            "roundId": round_id,
            "classification": "pre_normalization_evidence",
            "repository": str(repository),
            "allowedPaths": allowed_paths,
            "before": before,
            "publishedAt": utc_now(),
        },
    )
    before_sha256 = sha256_file(before_path)
    if before["objectFormat"] != "sha1":
        reasons = [
            f"unsupported Git object format {before['objectFormat']!r}; expected sha1 empty-blob semantics"
        ]
        _publish_failure(artifact_path, round_id, reasons, before)
        return {
            "classification": "failed", "reasons": reasons,
            "artifactPath": str(artifact_path), "beforeEvidencePath": str(before_path),
            "beforeEvidenceSha256": before_sha256,
        }
    untracked_paths = [item["path"] for item in before["untrackedFiles"]]
    existing_intent = before["intentToAddPaths"]
    candidates = sorted(set(untracked_paths) | set(existing_intent))
    unknown = sorted(set(candidates) - set(allowed_paths))
    if unknown:
        reasons = [f"untracked or intent-to-add path is outside the complete issue allowlist: {path}" for path in unknown]
        _publish_failure(artifact_path, round_id, reasons, before)
        return {
            "classification": "failed", "reasons": reasons,
            "artifactPath": str(artifact_path), "beforeEvidencePath": str(before_path),
            "beforeEvidenceSha256": before_sha256,
        }
    new_paths = sorted(set(untracked_paths) - set(existing_intent))
    try:
        if new_paths:
            git(repository, "add", "-N", "--", *new_paths)
        after = capture_repository_state(repository, issue_snapshot)
    except Exception as error:
        reasons = [f"normalization or post-normalization capture failed: {error}"]
        _publish_failure(artifact_path, round_id, reasons, before)
        return {
            "classification": "failed",
            "reasons": reasons,
            "artifactPath": str(artifact_path),
            "beforeEvidencePath": str(before_path),
            "beforeEvidenceSha256": before_sha256,
        }
    errors = verification_errors(before, after, new_paths, candidates)
    if errors:
        _publish_failure(artifact_path, round_id, errors, before)
        return {
            "classification": "failed", "reasons": errors,
            "artifactPath": str(artifact_path), "beforeEvidencePath": str(before_path),
            "beforeEvidenceSha256": before_sha256,
        }
    artifact = {
        "schemaVersion": NORMALIZATION_SCHEMA_VERSION,
        "normalizationType": NORMALIZATION_TYPE,
        "roundId": round_id,
        "classification": "confirmed",
        "repository": str(repository),
        "head": before["head"],
        "branch": before["branch"],
        "paths": candidates,
        "newlyNormalizedPaths": new_paths,
        "emptyBlobOid": EMPTY_BLOB_OID,
        "allowedPaths": allowed_paths,
        "beforeEvidencePath": str(before_path),
        "beforeEvidenceSha256": before_sha256,
        "before": before,
        "after": after,
        "workingTreeContentUnchanged": True,
        "headUnchanged": True,
        "reflogUnchanged": True,
        "remoteUnchanged": True,
        "pullRequestsUnchanged": True,
        "publishedAt": utc_now(),
    }
    atomic_write_json(artifact_path, artifact)
    digest = sha256_file(artifact_path)
    return {
        "classification": "confirmed",
        "roundId": round_id,
        "artifactPath": str(artifact_path),
        "artifactSha256": digest,
        "beforeEvidencePath": str(before_path),
        "beforeEvidenceSha256": before_sha256,
        "paths": candidates,
        "newlyNormalizedPaths": new_paths,
        "beforeFingerprint": _sha256(json.dumps(before, sort_keys=True).encode()),
        "afterFingerprint": _sha256(json.dumps(after, sort_keys=True).encode()),
        "reasons": [],
    }


def validate_normalization_artifact(
    *,
    artifact_path: Path,
    expected_sha256: str,
    round_id: str,
    repository: Path,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError as error:
        return {}, [f"normalization artifact is missing or unreadable: {error}"]
    if hashlib.sha256(artifact_bytes).hexdigest() != expected_sha256:
        return {}, ["normalization artifact hash mismatch"]
    if not artifact_path.with_suffix(".lock").is_dir():
        return {}, ["normalization round claim is missing"]
    try:
        artifact = json.loads(artifact_bytes)
    except json.JSONDecodeError as error:
        return {}, [f"normalization artifact is unreadable: {error}"]
    required = {
        "schemaVersion", "normalizationType", "roundId", "classification", "repository",
        "head", "branch", "paths", "newlyNormalizedPaths", "emptyBlobOid", "allowedPaths",
        "beforeEvidencePath", "beforeEvidenceSha256", "before", "after",
        "workingTreeContentUnchanged", "headUnchanged", "reflogUnchanged",
        "remoteUnchanged", "pullRequestsUnchanged", "publishedAt",
    }
    if not isinstance(artifact, dict) or set(artifact) != required:
        errors.append("normalization artifact schema is invalid")
        return artifact if isinstance(artifact, dict) else {}, errors
    expected_values = {
        "schemaVersion": NORMALIZATION_SCHEMA_VERSION,
        "normalizationType": NORMALIZATION_TYPE,
        "roundId": round_id,
        "classification": "confirmed",
        "repository": str(repository.resolve()),
        "emptyBlobOid": EMPTY_BLOB_OID,
        "workingTreeContentUnchanged": True,
        "headUnchanged": True,
        "reflogUnchanged": True,
        "remoteUnchanged": True,
        "pullRequestsUnchanged": True,
    }
    for field, expected in expected_values.items():
        if artifact.get(field) != expected:
            errors.append(f"normalization artifact {field} mismatch")
    before_path = artifact.get("beforeEvidencePath")
    before_sha256 = artifact.get("beforeEvidenceSha256")
    if (
        not isinstance(before_path, str)
        or not isinstance(before_sha256, str)
        or not Path(before_path).is_file()
        or sha256_file(Path(before_path)) != before_sha256
    ):
        errors.append("pre-normalization evidence is missing or modified")
    issue_snapshot = artifact_path.resolve().parents[1] / "issue-snapshot.json"
    try:
        current = capture_repository_state(repository.resolve(), issue_snapshot)
    except Exception as error:
        errors.append(f"could not verify normalized repository state: {error}")
    else:
        if current != artifact.get("after"):
            errors.append("repository no longer matches the normalized baseline")
    return artifact, errors
