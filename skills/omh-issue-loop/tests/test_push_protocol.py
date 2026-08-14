from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from push_protocol import (  # noqa: E402
    classify,
    command_safety_errors,
    repository_snapshot,
    retry_errors,
    validate_exit_artifact,
)
from worker_protocol import atomic_write_json, command_hash, load_json, sha256_file  # noqa: E402


class PushProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.remote = self.root / "repo", self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "topic", str(self.repo)], check=True)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "file").write_text("content\n")
        self.git("add", "file"); self.git("commit", "-q", "-m", "base")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.base = {
            "branch": "topic", "head": self.head, "statusPorcelain": "",
            "treeFingerprint": "tree", "upstreamRef": None, "upstreamOid": None,
            "remoteOid": None, "prState": [],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def classify(self, **changes):
        values = dict(artifact_errors=[], exit_code=0, process_tree_quiescent=True,
                      outputs_stable=True, status_porcelain="", head=self.head,
                      upstream_oid=self.head, remote_branch_oid=self.head, hook_bypassed=False)
        values.update(changes)
        return classify(**values)

    def test_01_confirmed_success_with_local_remote_and_hook(self) -> None:
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\necho hook-ran >&2\n")
        hook.chmod(0o755)
        attempt = self.root / "attempt"; attempt.mkdir()
        run = subprocess.run([
            sys.executable, str(SCRIPTS / "run-push.py"), "--run-dir", str(attempt),
            "--repository", str(self.repo), "--run-id", "push-1", "--",
            "git", "push", "--set-upstream", "origin", "topic",
        ], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        reconcile = subprocess.run([
            sys.executable, str(SCRIPTS / "reconcile-push.py"), "--run-dir", str(attempt),
            "--repository", str(self.repo), "--branch", "topic", "--interval", "0.01",
        ], capture_output=True, text=True)
        self.assertEqual(reconcile.returncode, 0, reconcile.stdout + reconcile.stderr)
        self.assertEqual(json.loads(reconcile.stdout)["status"], "confirmed")
        self.assertIn("hook-ran", (attempt / "push-stderr.log").read_text())
        state = load_json(attempt / "push-operation-state.json")
        self.assertEqual(state["phase"], "push_reconciled")
        self.assertEqual(
            state["resumeAfterCompletion"],
            "reconcile the push artifact and continue publication for the exact HEAD",
        )

    def test_01b_old_unprocessed_push_records_recovery(self) -> None:
        attempt = self.root / "attempt"; attempt.mkdir()
        run = subprocess.run([
            sys.executable, str(SCRIPTS / "run-push.py"), "--run-dir", str(attempt),
            "--repository", str(self.repo), "--run-id", "push-old", "--",
            "git", "push", "--set-upstream", "origin", "topic",
        ], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        artifact = load_json(attempt / "push-exit.json")
        artifact["finishedAt"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        atomic_write_json(attempt / "push-exit.json", artifact)
        state = load_json(attempt / "push-operation-state.json")
        state["phase"] = "push_running"
        atomic_write_json(attempt / "push-operation-state.json", state)
        reconcile = subprocess.run([
            sys.executable, str(SCRIPTS / "reconcile-push.py"), "--run-dir", str(attempt),
            "--repository", str(self.repo), "--branch", "topic", "--interval", "0.01",
        ], capture_output=True, text=True)
        self.assertEqual(reconcile.returncode, 0, reconcile.stdout + reconcile.stderr)
        result = json.loads(reconcile.stdout)
        self.assertEqual(result["recoveryReason"], "lost_or_unprocessed_completion_notification")
        state = load_json(attempt / "push-operation-state.json")
        self.assertEqual(state["phase"], "push_reconciled")
        self.assertEqual(state["resumedFromPhase"], "push_running")

    def test_02_null_notification_with_live_descendant_prohibits_retry(self) -> None:
        status, _ = self.classify(exit_code=None, process_tree_quiescent=False)
        self.assertEqual(status, "indeterminate")
        self.assertTrue(retry_errors(self.base, self.base, False))

    def test_03_zombie_or_exited_wrapper_with_detached_descendant_blocks_success(self) -> None:
        status, _ = self.classify(process_tree_quiescent=False)
        self.assertNotEqual(status, "confirmed")

    def test_04_timeout_absent_remote_stopped_and_unchanged_allows_retry(self) -> None:
        self.assertEqual(retry_errors(self.base, dict(self.base), True), [])

    def test_05_timeout_with_original_process_alive_prohibits_retry(self) -> None:
        self.assertIn("live or indeterminate", retry_errors(self.base, self.base, False)[0])

    def test_06_zero_exit_with_remote_oid_mismatch_fails(self) -> None:
        status, _ = self.classify(remote_branch_oid="0" * 40)
        self.assertEqual(status, "failed")

    def test_07_remote_exists_without_exit_artifact_is_not_success(self) -> None:
        status, _ = self.classify(artifact_errors=["durable exit artifact is missing"], exit_code=None)
        self.assertEqual(status, "indeterminate")

    def test_08_hook_numeric_nonzero_and_unchanged_remote_fails(self) -> None:
        status, reasons = self.classify(exit_code=9, upstream_oid=None, remote_branch_oid=None)
        self.assertEqual(status, "failed")
        self.assertIn("numeric status 9", reasons[0])

    def test_09_prohibited_commands_are_rejected_before_launch(self) -> None:
        commands = [
            ["git", "push", "--no-verify", "origin", "topic"],
            ["git", "push", "--force-with-lease", "origin", "topic"],
            ["git", "push", "origin", "+topic:topic"],
            ["git", "push", "--mirror", "origin"],
            ["nohup", "git", "push"], ["git", "push", "origin", "topic", "&"],
            ["git", "-c", "core.hooksPath=/dev/null", "push"],
        ]
        for command in commands:
            self.assertTrue(command_safety_errors(command), command)

    def test_10_any_baseline_or_remote_side_effect_change_prohibits_retry(self) -> None:
        for field, changed in (("head", "1" * 40), ("branch", "other"),
                               ("treeFingerprint", "dirty"), ("upstreamOid", self.head),
                               ("upstreamRef", "origin/topic"),
                               ("prState", [{"number": 1}]), ("remoteOid", self.head)):
            current = dict(self.base); current[field] = changed
            self.assertTrue(retry_errors(self.base, current, True), field)
        existing = dict(self.base, remoteOid=self.head)
        deleted = dict(existing, remoteOid=None)
        self.assertTrue(retry_errors(existing, deleted, True), "remote deletion")

    def test_11_zero_matching_state_waits_for_output_and_tree_quiescence(self) -> None:
        status, _ = self.classify(outputs_stable=False)
        self.assertEqual(status, "indeterminate")

    def test_12_mismatched_artifact_identity_and_hashes_is_rejected(self) -> None:
        stdout, stderr = self.root / "stdout", self.root / "stderr"
        stdout.write_text("out"); stderr.write_text("err")
        identity = {"pid": 1, "startToken": "token", "command": "git"}
        metadata = {"runId": "run", "commandHash": command_hash(["git", "push"]), "processIdentity": identity}
        artifact = {
            "protocolVersion": 1, "runId": "wrong", "commandHash": "0" * 64,
            "processIdentity": {"pid": 2}, "exitCode": 0,
            "stdoutSha256": "0" * 64, "stderrSha256": "1" * 64,
            "startedAt": "a", "finishedAt": "b",
        }
        errors = validate_exit_artifact(artifact, metadata, stdout, stderr)
        for expected in ("run id mismatch", "command hash mismatch", "process identity mismatch",
                         "stdout hash mismatch", "stderr hash mismatch"):
            self.assertIn(expected, errors)


if __name__ == "__main__":
    unittest.main()
