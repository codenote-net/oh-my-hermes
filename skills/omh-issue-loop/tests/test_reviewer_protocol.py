from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
RECONCILER = SCRIPTS / "reconcile-reviewer.py"
sys.path.insert(0, str(SCRIPTS))

from reviewer_protocol import common_git_config_hash  # noqa: E402
from worker_protocol import sha256_file  # noqa: E402


class ReviewerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.artifacts = self.root / "repo", self.root / "artifacts"
        self.repo.mkdir(); self.artifacts.mkdir()
        self.git("init", "-q"); self.git("config", "user.name", "Test"); self.git("config", "user.email", "test@example.com")
        (self.repo / "file").write_text("base\n"); self.git("add", "file"); self.git("commit", "-q", "-m", "base")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.stdout = self.artifacts / "stdout.log"; self.stderr = self.artifacts / "stderr.log"
        self.stdout.write_text("Review complete with adequate detail.\nHigh-priority findings: 0\n")
        self.stderr.write_text("")
        self.identity = {"pid": 999999, "startToken": "missing", "command": "reviewer"}
        self.write("metadata.json", {"commandHash": "a" * 64, "processIdentity": self.identity, "knownDescendantIdentities": [], "targetSha": self.head, "reviewKind": "code", "requireCommandEvidence": False})
        self.write("baseline.json", {"head": self.head, "statusPorcelain": "", "commonGitConfigSha256": common_git_config_hash(self.repo)})
        self.publish_exit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def write(self, name: str, value: dict) -> None:
        (self.artifacts / name).write_text(json.dumps(value))

    def publish_exit(self, **changes) -> None:
        value = {"protocolVersion": 1, "commandHash": "a" * 64, "processIdentity": self.identity, "targetSha": self.head, "exitCode": 0, "stdoutSha256": sha256_file(self.stdout), "stderrSha256": sha256_file(self.stderr), "startedAt": "2026-08-14T00:00:00+00:00", "finishedAt": datetime.now(timezone.utc).isoformat()}
        value.update(changes); self.write("exit.json", value)

    def reconcile(self, *extra: str):
        run = subprocess.run([sys.executable, str(RECONCILER), "--artifact-dir", str(self.artifacts), "--repository", str(self.repo), "--expected-head", self.head, "--interval", "0.01", "--timeout", "1.0", *extra], capture_output=True, text=True)
        return run, json.loads(run.stdout)

    def test_valid_artifact_confirms_without_notification(self):
        run, result = self.reconcile(); self.assertEqual(run.returncode, 0); self.assertEqual(result["classification"], "confirmed")

    def test_hash_mismatch_is_invalid(self):
        self.publish_exit(stdoutSha256="0" * 64); _, result = self.reconcile(); self.assertEqual(result["classification"], "invalid")

    def test_stale_sha_is_rejected(self):
        metadata = json.loads((self.artifacts / "metadata.json").read_text())
        metadata["targetSha"] = "0" * 40; self.write("metadata.json", metadata)
        self.publish_exit(targetSha="0" * 40); _, result = self.reconcile(); self.assertEqual(result["classification"], "stale_target")

    def test_alive_without_artifact_is_running(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            started = subprocess.run(["ps", "-p", str(sleeper.pid), "-o", "lstart=", "-o", "comm="], capture_output=True, text=True, check=True).stdout.split()
            metadata = json.loads((self.artifacts / "metadata.json").read_text())
            metadata["processIdentity"] = {"pid": sleeper.pid, "startToken": " ".join(started[:5]), "command": " ".join(started[5:])}
            self.write("metadata.json", metadata); (self.artifacts / "exit.json").unlink()
            _, result = self.reconcile(); self.assertEqual(result["classification"], "running")
        finally:
            sleeper.terminate(); sleeper.wait()

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process semantics")
    def test_zombie_launcher_with_valid_artifact_confirms(self):
        zombie_pid = os.fork()
        if zombie_pid == 0:
            os._exit(0)
        try:
            for _ in range(100):
                process = subprocess.run(
                    ["ps", "-p", str(zombie_pid), "-o", "state=", "-o", "lstart=", "-o", "comm="],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if process.stdout.lstrip().startswith("Z"):
                    break
                time.sleep(0.01)
            else:
                self.fail("child did not become a zombie")
            fields = process.stdout.split()
            self.identity = {
                "pid": zombie_pid,
                "startToken": " ".join(fields[1:6]),
                "command": " ".join(fields[6:]),
            }
            metadata = json.loads((self.artifacts / "metadata.json").read_text())
            metadata["processIdentity"] = self.identity
            self.write("metadata.json", metadata)
            self.publish_exit()
            run, result = self.reconcile()
            self.assertEqual(run.returncode, 0)
            self.assertEqual(result["classification"], "confirmed")
        finally:
            os.waitpid(zombie_pid, 0)

    def test_blank_report_is_incomplete(self):
        self.stdout.write_text("\n"); self.publish_exit(); _, result = self.reconcile(); self.assertEqual(result["classification"], "incomplete_report")

    def test_behavior_requires_commands_and_statuses(self):
        _, result = self.reconcile("--require-command-evidence"); self.assertEqual(result["classification"], "incomplete_report")

    def test_side_effect_is_detected(self):
        (self.repo / "new").write_text("x"); _, result = self.reconcile(); self.assertEqual(result["classification"], "side_effect_detected")

    def test_old_valid_artifact_marks_recovery_candidate(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(); self.publish_exit(finishedAt=old)
        _, result = self.reconcile(); self.assertTrue(result["lostNotificationCandidate"])

    def test_dead_without_artifact_is_indeterminate(self):
        (self.artifacts / "exit.json").unlink(); _, result = self.reconcile(); self.assertEqual(result["classification"], "indeterminate")


if __name__ == "__main__":
    unittest.main()
