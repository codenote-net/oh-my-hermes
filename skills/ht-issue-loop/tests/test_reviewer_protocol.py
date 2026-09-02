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
RUNNER = SCRIPTS / "run-reviewer.py"
NORMALIZER = SCRIPTS / "normalize-review-candidate.py"
sys.path.insert(0, str(SCRIPTS))

from reviewer_protocol import REVIEW_PROTOCOL_VERSION, common_git_config_hash  # noqa: E402
from operation_protocol import initial_state  # noqa: E402
from worker_protocol import process_identity, sha256_file  # noqa: E402


class ReviewerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.artifacts = self.root / "repo", self.root / "artifacts"
        self.repo.mkdir(); self.artifacts.mkdir()
        self.git("init", "-q"); self.git("config", "user.name", "Test"); self.git("config", "user.email", "test@example.com"); self.git("config", "commit.gpgsign", "false")
        (self.repo / "file").write_text("base\n"); self.git("add", "file"); self.git("commit", "-q", "-m", "base")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.remote = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        branch = self.git("branch", "--show-current").stdout.strip()
        self.git("push", "-q", "-u", "origin", branch)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "print(json.dumps([] if sys.argv[1:3] == ['pr', 'list'] else {}))\n"
        )
        gh.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        (self.run_dir / "issue-snapshot.json").write_text('{"issue": 1}\n')
        allowed = self.root / "allowed.json"
        allowed.write_text("[]\n")
        normalized = subprocess.run(
            [
                sys.executable, str(NORMALIZER),
                "--run-dir", str(self.run_dir),
                "--repository", str(self.repo),
                "--allowed-paths-json", str(allowed),
                "--round-id", "fixture-round",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        normalization = json.loads(normalized.stdout)
        self.stdout = self.artifacts / "stdout.log"; self.stderr = self.artifacts / "stderr.log"
        self.stdout.write_text("Review complete with adequate detail.\nHigh-priority findings: 0\n")
        self.stderr.write_text("")
        self.identity = {"pid": 999999, "startToken": "missing", "command": "reviewer"}
        self.write("baseline.json", {
            "schemaVersion": REVIEW_PROTOCOL_VERSION,
            "head": self.head,
            "statusPorcelain": "",
            "commonGitConfigSha256": common_git_config_hash(self.repo),
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "normalizationArtifact": normalization["artifactPath"],
            "normalizationSha256": normalization["artifactSha256"],
            "roundId": "fixture-round",
            "repositoryState": json.loads(Path(normalization["artifactPath"]).read_text())["after"],
        })
        self.baseline_sha256 = sha256_file(self.artifacts / "baseline.json")
        self.write("metadata.json", {"protocolVersion": REVIEW_PROTOCOL_VERSION, "commandHash": "a" * 64, "attemptId": "review-1", "deadline": None, "resumeAfterCompletion": "continue review gate", "processIdentity": self.identity, "knownDescendantIdentities": [], "targetSha": self.head, "baselineSha256": self.baseline_sha256, "reviewKind": "code", "requireCommandEvidence": False})
        state = initial_state(
            operation_kind="reviewer",
            attempt_id="review-1",
            command_hash="a" * 64,
            target_sha=self.head,
            expected_artifact_paths={
                "metadata": str(self.artifacts.resolve() / "metadata.json"),
                "baseline": str(self.artifacts.resolve() / "baseline.json"),
                "stdout": str(self.artifacts.resolve() / "stdout.log"),
                "stderr": str(self.artifacts.resolve() / "stderr.log"),
                "exit": str(self.artifacts.resolve() / "exit.json"),
            },
            deadline=None,
            resume_after_completion="continue review gate",
        )
        state["phase"] = "reviewer_artifact_published"
        state["processIdentity"] = self.identity
        self.write("operation-state.json", state)
        self.publish_exit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def write(self, name: str, value: dict) -> None:
        (self.artifacts / name).write_text(json.dumps(value))

    def publish_exit(self, **changes) -> None:
        value = {"protocolVersion": REVIEW_PROTOCOL_VERSION, "commandHash": "a" * 64, "processIdentity": self.identity, "targetSha": self.head, "baselineSha256": self.baseline_sha256, "exitCode": 0, "stdoutSha256": sha256_file(self.stdout), "stderrSha256": sha256_file(self.stderr), "startedAt": "2026-08-14T00:00:00+00:00", "finishedAt": datetime.now(timezone.utc).isoformat()}
        value.update(changes); self.write("exit.json", value)

    def reconcile(self, *extra: str):
        run = subprocess.run([sys.executable, str(RECONCILER), "--artifact-dir", str(self.artifacts), "--repository", str(self.repo), "--expected-head", self.head, "--interval", "0.01", "--timeout", "1.0", *extra], capture_output=True, text=True, env=self.env)
        return run, json.loads(run.stdout)

    def test_valid_artifact_confirms_without_notification(self):
        run, result = self.reconcile(); self.assertEqual(run.returncode, 0); self.assertEqual(result["classification"], "confirmed")

    def test_hash_mismatch_is_invalid(self):
        self.publish_exit(stdoutSha256="0" * 64); _, result = self.reconcile(); self.assertEqual(result["classification"], "invalid")

    def test_baseline_tampering_is_invalid(self):
        baseline = json.loads((self.artifacts / "baseline.json").read_text())
        baseline["statusPorcelain"] = "tampered"
        self.write("baseline.json", baseline)
        _, result = self.reconcile()
        self.assertEqual(result["classification"], "invalid")
        self.assertIn("baseline hash mismatch", result["reasons"])

    def test_protocol_v2_requires_complete_normalization_baseline(self):
        baseline = json.loads((self.artifacts / "baseline.json").read_text())
        for field in ("normalizationArtifact", "normalizationSha256", "roundId", "repositoryState"):
            baseline.pop(field)
        self.write("baseline.json", baseline)
        self.baseline_sha256 = sha256_file(self.artifacts / "baseline.json")
        metadata = json.loads((self.artifacts / "metadata.json").read_text())
        metadata["baselineSha256"] = self.baseline_sha256
        self.write("metadata.json", metadata)
        self.publish_exit()
        _, result = self.reconcile()
        self.assertEqual(result["classification"], "invalid")
        self.assertIn("reviewer baseline schema is invalid", result["reasons"])

    def test_reconciler_uses_post_quiescence_baseline_bytes(self):
        original_baseline = (self.artifacts / "baseline.json").read_bytes()
        (self.repo / "file").write_text("mutated\n")
        allowed = self.root / "attack-allowed.json"
        allowed.write_text("[]\n")
        normalized = subprocess.run(
            [
                sys.executable, str(NORMALIZER),
                "--run-dir", str(self.run_dir),
                "--repository", str(self.repo),
                "--allowed-paths-json", str(allowed),
                "--round-id", "attack-round",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        attack = json.loads(normalized.stdout)
        malicious_baseline = json.loads(original_baseline)
        malicious_baseline.update(
            statusPorcelain=self.git("status", "--porcelain=v1", "--untracked-files=all").stdout,
            normalizationArtifact=attack["artifactPath"],
            normalizationSha256=attack["artifactSha256"],
            roundId="attack-round",
            repositoryState=json.loads(Path(attack["artifactPath"]).read_text())["after"],
        )
        self.write("baseline.json", malicious_baseline)
        launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            identity = process_identity(launcher.pid)
            self.assertIsNotNone(identity)
            metadata = json.loads((self.artifacts / "metadata.json").read_text())
            metadata["processIdentity"] = identity
            self.write("metadata.json", metadata)
            state = json.loads((self.artifacts / "operation-state.json").read_text())
            state["processIdentity"] = identity
            state["phase"] = "reviewer_running"
            self.write("operation-state.json", state)
            self.identity = identity
            self.publish_exit()
            reconcile = subprocess.Popen(
                [
                    sys.executable, str(RECONCILER),
                    "--artifact-dir", str(self.artifacts),
                    "--repository", str(self.repo),
                    "--expected-head", self.head,
                    "--interval", "0.05",
                    "--timeout", "2.0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
            )
            time.sleep(0.15)
            (self.artifacts / "baseline.json").write_bytes(original_baseline)
            launcher.terminate()
            launcher.wait()
            stdout, stderr = reconcile.communicate(timeout=3)
            self.assertEqual(json.loads(stdout)["classification"], "side_effect_detected", stderr)
        finally:
            if launcher.poll() is None:
                launcher.terminate()
                launcher.wait()

    def test_operation_state_mismatch_is_invalid(self):
        state = json.loads((self.artifacts / "operation-state.json").read_text())
        state["commandHash"] = "b" * 64
        self.write("operation-state.json", state)
        _, result = self.reconcile()
        self.assertEqual(result["classification"], "invalid")
        self.assertIn("operation state command hash mismatch", result["reasons"])

    def test_stale_sha_is_rejected(self):
        metadata = json.loads((self.artifacts / "metadata.json").read_text())
        metadata["targetSha"] = "0" * 40; self.write("metadata.json", metadata)
        state = json.loads((self.artifacts / "operation-state.json").read_text())
        state["targetSha"] = "0" * 40; self.write("operation-state.json", state)
        self.publish_exit(targetSha="0" * 40); _, result = self.reconcile(); self.assertEqual(result["classification"], "stale_target")

    def test_alive_without_artifact_is_running(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            started = subprocess.run(["ps", "-p", str(sleeper.pid), "-o", "lstart=", "-o", "comm="], capture_output=True, text=True, check=True).stdout.split()
            metadata = json.loads((self.artifacts / "metadata.json").read_text())
            metadata["processIdentity"] = {"pid": sleeper.pid, "startToken": " ".join(started[:5]), "command": " ".join(started[5:])}
            self.write("metadata.json", metadata); (self.artifacts / "exit.json").unlink()
            state = json.loads((self.artifacts / "operation-state.json").read_text())
            state["processIdentity"] = metadata["processIdentity"]
            state["phase"] = "reviewer_running"
            self.write("operation-state.json", state)
            _, result = self.reconcile(); self.assertEqual(result["classification"], "running")
        finally:
            sleeper.terminate(); sleeper.wait()

    def test_reconciler_refreshes_late_descendant_identities(self):
        launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            launcher_identity = process_identity(launcher.pid)
            descendant_identity = process_identity(descendant.pid)
            self.assertIsNotNone(launcher_identity)
            self.assertIsNotNone(descendant_identity)
            self.identity = launcher_identity
            metadata = json.loads((self.artifacts / "metadata.json").read_text())
            metadata["processIdentity"] = launcher_identity
            self.write("metadata.json", metadata)
            state = json.loads((self.artifacts / "operation-state.json").read_text())
            state["processIdentity"] = launcher_identity
            state["phase"] = "reviewer_running"
            self.write("operation-state.json", state)
            self.publish_exit()
            reconcile = subprocess.Popen(
                [
                    sys.executable, str(RECONCILER), "--artifact-dir", str(self.artifacts),
                    "--repository", str(self.repo), "--expected-head", self.head,
                    "--interval", "0.05", "--timeout", "0.4",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.1)
            metadata["knownDescendantIdentities"] = [descendant_identity]
            self.write("metadata.json", metadata)
            state["knownDescendantIdentities"] = [descendant_identity]
            self.write("operation-state.json", state)
            launcher.terminate(); launcher.wait()
            stdout, stderr = reconcile.communicate(timeout=2)
            self.assertEqual(json.loads(stdout)["classification"], "running", stderr)
        finally:
            if launcher.poll() is None:
                launcher.terminate(); launcher.wait()
            descendant.terminate(); descendant.wait()

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
            state = json.loads((self.artifacts / "operation-state.json").read_text())
            state["processIdentity"] = self.identity
            self.write("operation-state.json", state)
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
        state = json.loads((self.artifacts / "operation-state.json").read_text())
        state["phase"] = "reviewer_running"; self.write("operation-state.json", state)
        _, result = self.reconcile(); self.assertTrue(result["lostNotificationCandidate"])
        state = json.loads((self.artifacts / "operation-state.json").read_text())
        self.assertEqual(state["phase"], "reviewer_reconciled")
        self.assertEqual(state["recoveryReason"], "lost_or_unprocessed_completion_notification")
        self.assertEqual(state["resumedFromPhase"], "reviewer_running")

    def test_dead_without_artifact_is_indeterminate(self):
        (self.artifacts / "exit.json").unlink(); _, result = self.reconcile(); self.assertEqual(result["classification"], "indeterminate")


class ReviewerRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "file").write_text("base\n")
        self.git("add", "file")
        self.git("commit", "-q", "-m", "base")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "HEAD")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        (self.run_dir / "issue-snapshot.json").write_text("{}\n")
        allowed = self.root / "allowed.json"
        allowed.write_text("[]\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text("#!/bin/sh\n[ \"$1 $2\" = \"pr list\" ] && printf '[]\\n' && exit 0\nexit 2\n")
        gh.chmod(0o700)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        normalized = subprocess.run(
            [
                sys.executable, str(NORMALIZER), "--run-dir", str(self.run_dir),
                "--repository", str(self.repo), "--allowed-paths-json", str(allowed),
                "--round-id", "round-1",
            ],
            capture_output=True,
            text=True,
            env=self.env,
            check=True,
        )
        self.normalization = json.loads(normalized.stdout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_reviewer(self, artifact_dir: Path, *extra: str):
        report = "Review completed successfully.\nHigh-priority findings: 0\n"
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--artifact-dir",
                str(artifact_dir),
                "--repository",
                str(self.repo),
                "--target-sha",
                self.head,
                "--review-kind",
                "code",
                "--normalization-artifact",
                self.normalization["artifactPath"],
                "--normalization-sha256",
                self.normalization["artifactSha256"],
                "--round-id",
                "round-1",
                *extra,
                "--",
                sys.executable,
                "-c",
                f"print({report!r}, end='')",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_runner_publishes_artifacts_that_reconcile(self):
        artifacts = self.root / "artifacts"
        run = self.run_reviewer(artifacts)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            {path.name for path in artifacts.iterdir()},
            {
                "baseline.json", "metadata.json", "operation-state.json", "stdout.log",
                "stderr.log", "exit.json",
            },
        )
        reconcile = subprocess.run(
            [
                sys.executable,
                str(RECONCILER),
                "--artifact-dir",
                str(artifacts),
                "--repository",
                str(self.repo),
                "--expected-head",
                self.head,
                "--interval",
                "0.01",
                "--timeout",
                "1.0",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(reconcile.returncode, 0, reconcile.stdout + reconcile.stderr)
        self.assertEqual(json.loads(reconcile.stdout)["classification"], "confirmed")
        state = json.loads((artifacts / "operation-state.json").read_text())
        self.assertEqual(state["phase"], "reviewer_reconciled")
        self.assertEqual(
            state["resumeAfterCompletion"],
            "reconcile the reviewer artifact and continue the exact-SHA review gate",
        )

    def test_runner_rejects_a_stale_target_before_launch(self):
        run = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--artifact-dir",
                str(self.root / "artifacts"),
                "--repository",
                str(self.repo),
                "--target-sha",
                "0" * 40,
                "--review-kind",
                "code",
                "--normalization-artifact",
                self.normalization["artifactPath"],
                "--normalization-sha256",
                self.normalization["artifactSha256"],
                "--round-id",
                "round-1",
                "--",
                sys.executable,
                "-c",
                "print('must not run')",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(run.returncode, 125)
        self.assertIn("does not match target", run.stderr)

    def test_runner_preserves_existing_artifacts(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        marker = artifacts / "keep"
        marker.write_text("existing\n")
        run = self.run_reviewer(artifacts)
        self.assertEqual(run.returncode, 125)
        self.assertEqual(marker.read_text(), "existing\n")
        self.assertEqual({path.name for path in artifacts.iterdir()}, {"keep"})


if __name__ == "__main__":
    unittest.main()
