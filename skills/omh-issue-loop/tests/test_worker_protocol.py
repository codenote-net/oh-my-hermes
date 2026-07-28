from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
RUNNER = SCRIPTS / "run-worker.py"
RECONCILER = SCRIPTS / "reconcile-worker.py"
sys.path.insert(0, str(SCRIPTS))

from worker_protocol import (  # noqa: E402
    atomic_write_json,
    load_json,
    new_run_errors,
    resume_state_errors,
    working_tree_fingerprint,
)


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.run_dir = self.root / "hermes" / "runs" / "omh-issue-loop" / "run-1"
        self.repository.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.repository / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "base")
        snapshot = {"number": 1, "title": "test", "body": "", "labels": [], "url": "https://github.com/o/r/issues/1"}
        atomic_write_json(self.run_dir / "issue-snapshot.json", snapshot)
        state = {
                "protocolVersion": 2,
                "runId": "run-1",
                "issueUrl": snapshot["url"],
                "issueSnapshotHash": hashlib.sha256(
                    (self.run_dir / "issue-snapshot.json").read_bytes()
                ).hexdigest(),
                "repositoryIdentity": "o/r",
                "branch": self.git("branch", "--show-current").stdout.strip(),
                "baseSha": self.git("rev-parse", "HEAD").stdout.strip(),
                "currentPhase": "implementation",
                "fixCount": 0,
                "repositoryRoot": str(self.repository.resolve()),
                "currentHead": self.git("rev-parse", "HEAD").stdout.strip(),
                "expectedWorkingTreeFingerprint": working_tree_fingerprint(self.repository),
            }
        atomic_write_json(self.run_dir / "state.json", state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_worker(self, code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--run-dir",
                str(self.run_dir),
                "--repository",
                str(self.repository),
                "--",
                sys.executable,
                "-c",
                code,
            ],
            capture_output=True,
            text=True,
        )

    def start_worker(self, code: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--run-dir",
                str(self.run_dir),
                "--repository",
                str(self.repository),
                "--",
                sys.executable,
                "-c",
                code,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def reconcile(
        self, *, timeout: float = 1, evidence: Path | None = None
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        command = [
            sys.executable,
            str(RECONCILER),
            "--run-dir",
            str(self.run_dir),
            "--repository",
            str(self.repository),
            "--observations",
            "2",
            "--interval",
            "0.02",
            "--timeout",
            str(timeout),
        ]
        if evidence:
            command += ["--salvage-evidence", str(evidence)]
        result = subprocess.run(command, capture_output=True, text=True)
        return result, json.loads(result.stdout)

    def complete_report(self) -> str:
        report = (
            "Files changed: tracked.txt\nValidation commands: test\n"
            "Exit status and result: 0 passed\nBlockers: None"
        )
        return f"print({report!r})"

    def make_salvage_evidence(self) -> Path:
        atomic_write_json(
            self.run_dir / "worker-baseline.json",
            {
                "head": self.git("rev-parse", "HEAD").stdout.strip(),
                "commitRange": [],
                "reflog": [],
                "remoteBranch": None,
                "pullRequests": [],
            },
        )
        evidence = self.run_dir / "salvage-evidence.json"
        atomic_write_json(
            evidence,
            {
                "baselineCompared": True,
                "sideEffectsClean": True,
                "reportComplete": True,
                "issueScopedDiff": True,
                "independentValidationPassed": True,
                "validationResults": [{"command": "git diff --check", "exitCode": 0}],
            },
        )
        return evidence

    def test_01_normal_exit_generates_valid_artifact(self) -> None:
        self.assertEqual(self.run_worker(self.complete_report()).returncode, 0)
        result, status = self.reconcile()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(status["completionStatus"], "confirmed")
        self.assertEqual(status["workerExitStatus"], 0)

    def test_02_nonzero_exit_is_preserved(self) -> None:
        self.assertEqual(self.run_worker("raise SystemExit(7)").returncode, 7)
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "confirmed")
        self.assertEqual(status["workerExitStatus"], 7)

    def test_03_exited_null_equivalent_while_worker_runs_is_indeterminate(self) -> None:
        worker = self.start_worker("import time; time.sleep(.35)")
        time.sleep(0.08)
        _, status = self.reconcile(timeout=0.05)
        self.assertEqual(status["completionStatus"], "indeterminate")
        worker.wait(timeout=2)

    def test_04_stable_output_does_not_override_live_process(self) -> None:
        worker = self.start_worker("import time; print('partial', flush=True); time.sleep(.35)")
        time.sleep(0.12)
        _, status = self.reconcile(timeout=0.05)
        self.assertIn("live", " ".join(status["reasons"]))
        worker.wait(timeout=2)

    def test_05_known_descendant_blocks_completion(self) -> None:
        code = "import subprocess,time; subprocess.Popen(['sleep','.5']); time.sleep(.3)"
        self.assertEqual(self.run_worker(code).returncode, 0)
        _, status = self.reconcile(timeout=0.05)
        self.assertEqual(status["completionStatus"], "indeterminate")
        time.sleep(0.55)

    def test_06_missing_artifact_is_not_confirmed(self) -> None:
        self.run_worker(self.complete_report())
        (self.run_dir / "worker-exit.json").unlink()
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "indeterminate")

    def test_07_complete_legacy_evidence_is_salvageable(self) -> None:
        self.run_worker(self.complete_report())
        (self.run_dir / "worker-exit.json").unlink()
        evidence = self.make_salvage_evidence()
        _, status = self.reconcile(evidence=evidence)
        self.assertEqual(status["completionStatus"], "salvageable")
        self.assertEqual(status["completionMode"], "salvaged_without_exit_artifact")
        self.assertEqual(status["workerExitStatus"], "unknown")

    def test_08_legacy_run_without_baseline_is_rejected(self) -> None:
        self.run_worker(self.complete_report())
        (self.run_dir / "worker-exit.json").unlink()
        evidence = self.make_salvage_evidence()
        (self.run_dir / "worker-baseline.json").unlink()
        _, status = self.reconcile(evidence=evidence)
        self.assertEqual(status["completionStatus"], "indeterminate")

    def test_09_late_working_tree_change_prevents_early_completion(self) -> None:
        code = "import pathlib,time; time.sleep(.15); pathlib.Path('tracked.txt').write_text('late\\n')"
        worker = self.start_worker(code)
        _, status = self.reconcile(timeout=0.05)
        self.assertEqual(status["completionStatus"], "indeterminate")
        worker.wait(timeout=2)
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "confirmed")

    def test_10_pid_reuse_identity_mismatch_rejects_artifact(self) -> None:
        self.run_worker(self.complete_report())
        state = load_json(self.run_dir / "state.json")
        state["workerProcessIdentity"]["startToken"] = "reused pid"
        atomic_write_json(self.run_dir / "state.json", state)
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "indeterminate")
        self.assertIn("process identity mismatch", status["reasons"])

    def test_11_new_reconciler_process_can_resume_durable_run(self) -> None:
        self.run_worker(self.complete_report())
        _, first = self.reconcile()
        _, second = self.reconcile()
        self.assertEqual(first["completionStatus"], second["completionStatus"])

    def test_12_resume_does_not_modify_issue_snapshot(self) -> None:
        self.run_worker(self.complete_report())
        before = (self.run_dir / "issue-snapshot.json").read_bytes()
        self.reconcile()
        self.assertEqual(before, (self.run_dir / "issue-snapshot.json").read_bytes())

    def test_13_resume_allows_exact_matching_dirty_tree(self) -> None:
        (self.repository / "tracked.txt").write_text("implementation\n")
        state = load_json(self.run_dir / "state.json")
        state["expectedWorkingTreeFingerprint"] = working_tree_fingerprint(self.repository)
        atomic_write_json(self.run_dir / "state.json", state)
        self.assertEqual(resume_state_errors(self.run_dir, self.repository), [])

    def test_14_new_run_rejects_dirty_tree_and_existing_branch(self) -> None:
        self.git("branch", "existing")
        (self.repository / "tracked.txt").write_text("dirty\n")
        errors = new_run_errors(self.repository, "existing")
        self.assertIn("new run requires a clean working tree", errors)
        self.assertIn("new run branch already exists", errors)

    def test_15_wrapper_remains_alive_until_foreground_worker_exits(self) -> None:
        wrapper = self.start_worker("import time; time.sleep(.25)")
        time.sleep(0.08)
        self.assertIsNone(wrapper.poll())
        wrapper.wait(timeout=2)
        self.assertTrue((self.run_dir / "worker-exit.json").is_file())


if __name__ == "__main__":
    unittest.main()
