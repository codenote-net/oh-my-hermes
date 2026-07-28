from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
INIT = SCRIPTS / "init-run-state.py"
CAPTURE = SCRIPTS / "capture-worker-baseline.py"
VALIDATE = SCRIPTS / "validate-run-state.py"
RUNNER = SCRIPTS / "run-worker.py"
RECONCILER = SCRIPTS / "reconcile-worker.py"
sys.path.insert(0, str(SCRIPTS))

from worker_protocol import (  # noqa: E402
    atomic_write_json,
    load_json,
    new_run_errors,
    resume_state_errors,
    validate_baseline_schema,
    validate_state_schema,
    working_tree_fingerprint,
)


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.run_dir = self.root / "runs" / "run-1"
        self.repository.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.repository / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "base")
        self.snapshot_source = self.root / "snapshot.json"
        atomic_write_json(
            self.snapshot_source,
            {
                "number": 1,
                "title": "test",
                "body": "",
                "labels": [],
                "url": "https://github.com/o/r/issues/1",
            },
        )
        self.pr_evidence = self.root / "prs.json"
        self.pr_evidence.write_text("[]\n")
        self.initialize()
        self.capture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def command(self, script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *arguments], capture_output=True, text=True
        )

    def initialize(self) -> subprocess.CompletedProcess[str]:
        return self.command(
            INIT,
            "--run-dir", str(self.run_dir),
            "--repository", str(self.repository),
            "--repository-identity", "o/r",
            "--issue-url", "https://github.com/o/r/issues/1",
            "--issue-snapshot", str(self.snapshot_source),
            "--run-id", "run-1",
            "--signoff-required", "false",
        )

    def capture(self) -> subprocess.CompletedProcess[str]:
        return self.command(
            CAPTURE,
            "--run-dir", str(self.run_dir),
            "--repository", str(self.repository),
            "--remote-branch-oid", "absent",
            "--pull-requests-json", str(self.pr_evidence),
        )

    def run_worker(self, code: str) -> subprocess.CompletedProcess[str]:
        return self.command(
            RUNNER,
            "--run-dir", str(self.run_dir),
            "--repository", str(self.repository),
            "--", sys.executable, "-c", code,
        )

    def start_worker(self, code: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable, str(RUNNER),
                "--run-dir", str(self.run_dir),
                "--repository", str(self.repository),
                "--", sys.executable, "-c", code,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def reconcile(self, timeout: float = 1, evidence: Path | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
        args = [
            "--run-dir", str(self.run_dir),
            "--repository", str(self.repository),
            "--observations", "2",
            "--interval", "0.02",
            "--timeout", str(timeout),
        ]
        if evidence:
            args += ["--salvage-evidence", str(evidence)]
        result = self.command(RECONCILER, *args)
        return result, json.loads(result.stdout)

    def make_salvage_evidence(self) -> Path:
        path = self.run_dir / "salvage-evidence.json"
        atomic_write_json(
            path,
            {
                "baselineCompared": True,
                "sideEffectsClean": True,
                "reportComplete": True,
                "issueScopedDiff": True,
                "independentValidationPassed": True,
                "validationResults": [{"command": "git diff --check", "exitCode": 0}],
            },
        )
        return path

    def test_01_initializer_and_baseline_are_canonical(self) -> None:
        self.assertEqual(validate_state_schema(load_json(self.run_dir / "state.json")), [])
        self.assertEqual(validate_baseline_schema(load_json(self.run_dir / "worker-baseline.json")), [])

    def test_02_pre_launch_cli_accepts_complete_run(self) -> None:
        result = self.command(
            VALIDATE, "pre-launch", "--repository", str(self.repository), "--run-dir", str(self.run_dir)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_03_missing_run_id_never_starts_child(self) -> None:
        state = load_json(self.run_dir / "state.json")
        del state["runId"]
        atomic_write_json(self.run_dir / "state.json", state)
        marker = self.root / "child-started"
        result = self.run_worker(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.assertEqual(result.returncode, 125)
        self.assertFalse(marker.exists())
        lifecycle = load_json(self.run_dir / "worker-lifecycle.json")
        self.assertEqual(lifecycle["stage"], "preflight_failed")
        self.assertFalse(lifecycle["workerSpawned"])

    def test_04_snapshot_hash_mismatch_never_starts_child(self) -> None:
        (self.run_dir / "issue-snapshot.json").write_text("{}\n")
        marker = self.root / "child-started"
        self.assertEqual(self.run_worker(f"open({str(marker)!r},'w').close()").returncode, 125)
        self.assertFalse(marker.exists())

    def test_05_repository_head_mismatch_never_starts_child(self) -> None:
        (self.repository / "second.txt").write_text("x")
        self.git("add", "second.txt")
        self.git("commit", "-q", "-m", "second")
        marker = self.root / "child-started"
        self.assertEqual(self.run_worker(f"open({str(marker)!r},'w').close()").returncode, 125)
        self.assertFalse(marker.exists())

    def test_06_missing_baseline_never_starts_child(self) -> None:
        (self.run_dir / "worker-baseline.json").unlink()
        marker = self.root / "child-started"
        self.assertEqual(self.run_worker(f"open({str(marker)!r},'w').close()").returncode, 125)
        self.assertFalse(marker.exists())

    def test_07_normal_exit_publishes_lifecycle_and_artifact(self) -> None:
        self.assertEqual(self.run_worker("print('done')").returncode, 0)
        lifecycle = load_json(self.run_dir / "worker-lifecycle.json")
        self.assertEqual(lifecycle["stage"], "artifact_published")
        self.assertTrue(lifecycle["workerSpawned"])
        self.assertTrue(lifecycle["artifactPublished"])

    def test_08_nonzero_exit_is_preserved(self) -> None:
        self.assertEqual(self.run_worker("raise SystemExit(7)").returncode, 7)
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "confirmed")
        self.assertEqual(status["workerExitStatus"], 7)

    def test_09_live_worker_is_indeterminate(self) -> None:
        worker = self.start_worker("import time; time.sleep(.35)")
        time.sleep(0.08)
        _, status = self.reconcile(timeout=0.05)
        self.assertEqual(status["completionStatus"], "indeterminate")
        worker.wait(timeout=2)

    def test_10_known_live_descendant_blocks_completion(self) -> None:
        self.assertEqual(
            self.run_worker("import subprocess; subprocess.Popen(['sleep','.4'])").returncode, 0
        )
        _, status = self.reconcile(timeout=0.05)
        self.assertEqual(status["completionStatus"], "indeterminate")
        time.sleep(0.45)

    def test_11_confirmed_exit_artifact(self) -> None:
        self.assertEqual(self.run_worker("print('ok')").returncode, 0)
        result, status = self.reconcile()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(status["completionStatus"], "confirmed")

    def test_12_missing_artifact_never_becomes_confirmed(self) -> None:
        self.run_worker("print('ok')")
        (self.run_dir / "worker-exit.json").unlink()
        _, status = self.reconcile()
        self.assertNotEqual(status["completionStatus"], "confirmed")

    def test_13_complete_legacy_evidence_is_salvageable_not_confirmed(self) -> None:
        self.run_worker("print('Files changed: None; validations: None; blockers: None')")
        (self.run_dir / "worker-exit.json").unlink()
        _, status = self.reconcile(evidence=self.make_salvage_evidence())
        self.assertEqual(status["completionStatus"], "salvageable")
        self.assertEqual(status["workerExitStatus"], "unknown")

    def test_14_legacy_without_evidence_is_indeterminate(self) -> None:
        self.run_worker("print('ok')")
        (self.run_dir / "worker-exit.json").unlink()
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "indeterminate")

    def test_15_artifact_publish_failure_has_durable_started_evidence(self) -> None:
        exit_path = self.run_dir / "worker-exit.json"
        code = f"from pathlib import Path; Path({str(exit_path)!r}).mkdir()"
        self.assertEqual(self.run_worker(code).returncode, 125)
        lifecycle = load_json(self.run_dir / "worker-lifecycle.json")
        self.assertEqual(lifecycle["stage"], "artifact_publish_failed")
        self.assertTrue(lifecycle["workerSpawned"])
        self.assertFalse(lifecycle["artifactPublished"])

    def test_16_resume_preserves_snapshot_and_exact_dirty_tree(self) -> None:
        before = (self.run_dir / "issue-snapshot.json").read_bytes()
        (self.repository / "tracked.txt").write_text("implementation\n")
        state = load_json(self.run_dir / "state.json")
        state["expectedWorkingTreeFingerprint"] = working_tree_fingerprint(self.repository)
        atomic_write_json(self.run_dir / "state.json", state)
        self.assertEqual(resume_state_errors(self.run_dir, self.repository), [])
        self.assertEqual(before, (self.run_dir / "issue-snapshot.json").read_bytes())

    def test_17_new_run_rejects_dirty_tree_and_existing_branch(self) -> None:
        self.git("branch", "existing")
        (self.repository / "tracked.txt").write_text("dirty\n")
        errors = new_run_errors(self.repository, "existing")
        self.assertIn("new run requires a clean working tree", errors)
        self.assertIn("new run branch already exists", errors)

    def test_18_wrapper_remains_alive_until_worker_exits(self) -> None:
        wrapper = self.start_worker("import time; time.sleep(.2)")
        time.sleep(0.06)
        self.assertIsNone(wrapper.poll())
        wrapper.wait(timeout=2)

    def test_19_external_state_mutation_invalidates_artifact(self) -> None:
        self.run_worker("print('ok')")
        state = load_json(self.run_dir / "state.json")
        state["workerCommandHash"] = "0" * 64
        atomic_write_json(self.run_dir / "state.json", state)
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "indeterminate")
        self.assertIn("command hash mismatch", status["reasons"])

    def test_20_preflight_rejection_is_reported_as_not_started(self) -> None:
        state = load_json(self.run_dir / "state.json")
        state["runId"] = ""
        atomic_write_json(self.run_dir / "state.json", state)
        self.run_worker("raise AssertionError('must not run')")
        _, status = self.reconcile()
        self.assertEqual(status["completionStatus"], "rejected")
        self.assertFalse(status["workerStarted"])

    def test_21_documented_canonical_examples_match_fixtures(self) -> None:
        document = (SKILL_DIR / "references" / "durable-worker-protocol.md").read_text()

        def example(begin: str, end: str) -> dict:
            block = document.split(begin, 1)[1].split(end, 1)[0]
            return json.loads(block.split("```json", 1)[1].split("```", 1)[0])

        fixtures = SKILL_DIR / "tests" / "fixtures"
        self.assertEqual(
            example("<!-- BEGIN CANONICAL STATE -->", "<!-- END CANONICAL STATE -->"),
            json.loads((fixtures / "canonical-state.json").read_text()),
        )
        self.assertEqual(
            example("<!-- BEGIN CANONICAL BASELINE -->", "<!-- END CANONICAL BASELINE -->"),
            json.loads((fixtures / "canonical-worker-baseline.json").read_text()),
        )
        self.assertEqual(validate_state_schema(json.loads((fixtures / "canonical-state.json").read_text())), [])
        self.assertEqual(
            validate_baseline_schema(json.loads((fixtures / "canonical-worker-baseline.json").read_text())),
            [],
        )

    def test_22_state_mutation_after_spawn_waits_for_worker_then_fails_closed(self) -> None:
        wrapper = self.start_worker("import time; time.sleep(.2); print('finished')")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            lifecycle_path = self.run_dir / "worker-lifecycle.json"
            if lifecycle_path.exists() and load_json(lifecycle_path)["stage"] == "running":
                break
            time.sleep(0.01)
        state = load_json(self.run_dir / "state.json")
        state["runId"] = "externally-mutated"
        atomic_write_json(self.run_dir / "state.json", state)
        self.assertIsNone(wrapper.poll())
        self.assertEqual(wrapper.wait(timeout=2), 125)
        lifecycle = load_json(self.run_dir / "worker-lifecycle.json")
        self.assertEqual(lifecycle["errorCode"], "state_mutated_after_spawn")
        self.assertTrue(lifecycle["workerSpawned"])
        self.assertFalse(lifecycle["artifactPublished"])


if __name__ == "__main__":
    unittest.main()
