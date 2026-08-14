from __future__ import annotations

import sys
import os
import signal
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from operation_protocol import initial_state, spawn_gated, update_state  # noqa: E402
from worker_protocol import atomic_write_json  # noqa: E402
from worker_protocol import process_group_identities  # noqa: E402


class GatedLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_child_cannot_run_before_identity_publication(self) -> None:
        marker = self.root / "marker"
        stdout_path, stderr_path = self.root / "stdout", self.root / "stderr"
        published = []
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = spawn_gated(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
                cwd=self.root,
                stdout=stdout,
                stderr=stderr,
                publish_identity=lambda identity: (
                    self.assertFalse(marker.exists()), published.append(identity)
                ),
            )
            self.assertEqual(process.wait(), 0)
        self.assertTrue(published)
        self.assertEqual(marker.read_text(), "ran")

    def test_publication_failure_prevents_child_execution(self) -> None:
        marker = self.root / "marker"
        stdout_path, stderr_path = self.root / "stdout", self.root / "stderr"

        def reject(_identity):
            raise RuntimeError("publication failed")

        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                spawn_gated(
                    [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
                    cwd=self.root,
                    stdout=stdout,
                    stderr=stderr,
                    publish_identity=reject,
                )
        self.assertFalse(marker.exists())

    def test_reparented_helper_remains_visible_in_process_group(self) -> None:
        stdout_path, stderr_path = self.root / "stdout", self.root / "stderr"
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = spawn_gated(
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'])",
                ],
                cwd=self.root,
                stdout=stdout,
                stderr=stderr,
                publish_identity=lambda _identity: None,
            )
            self.assertEqual(process.wait(), 0)
        try:
            self.assertTrue(process_group_identities(process.pid))
        finally:
            os.killpg(process.pid, signal.SIGTERM)

    def test_invalid_deadline_is_rejected_before_launch(self) -> None:
        with self.assertRaisesRegex(ValueError, "deadline timestamp is invalid"):
            initial_state(
                operation_kind="reviewer",
                attempt_id="attempt-1",
                command_hash="a" * 64,
                target_sha="b" * 40,
                expected_artifact_paths={},
                deadline="not-a-timestamp",
                resume_after_completion="continue review gate",
            )

    def test_invalid_phase_transition_is_rejected(self) -> None:
        state_path = self.root / "operation-state.json"
        atomic_write_json(
            state_path,
            initial_state(
                operation_kind="reviewer",
                attempt_id="attempt-1",
                command_hash="a" * 64,
                target_sha="b" * 40,
                expected_artifact_paths={},
                deadline=None,
                resume_after_completion="continue review gate",
            ),
        )
        with self.assertRaisesRegex(ValueError, "invalid operation phase transition"):
            update_state(state_path, "reviewer", phase="reviewer_reconciled")


if __name__ == "__main__":
    unittest.main()
