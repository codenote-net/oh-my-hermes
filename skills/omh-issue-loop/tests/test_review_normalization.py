from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
NORMALIZER = SCRIPTS / "normalize-review-candidate.py"
RUNNER = SCRIPTS / "run-reviewer.py"
RECONCILER = SCRIPTS / "reconcile-reviewer.py"
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


class ReviewNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.run_dir = self.root / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "base")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "HEAD")
        self.branch = self.git("branch", "--show-current").stdout.strip()
        (self.run_dir / "issue-snapshot.json").write_text(
            json.dumps({"number": 1, "title": "test", "body": "", "labels": []}) + "\n"
        )
        self.allowed = self.root / "allowed.json"
        self.allowed.write_text("[]\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.gh_state = self.root / "gh-state.json"
        self.gh_state.write_text("[]\n")
        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "state = pathlib.Path(os.environ['FAKE_GH_STATE'])\n"
            "if sys.argv[1:3] == ['pr', 'list']:\n"
            "    value = json.loads(state.read_text())\n"
            "    print(json.dumps([{'number': item['number']} for item in value]))\n"
            "elif sys.argv[1:3] == ['pr', 'view']:\n"
            "    number = int(sys.argv[3]); value = json.loads(state.read_text())\n"
            "    print(json.dumps(next(item for item in value if item['number'] == number)))\n"
            "else:\n"
            "    raise SystemExit(2)\n"
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_GH_STATE"] = str(self.gh_state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=check,
            capture_output=True,
            text=True,
        )

    def write_allowed(self, *paths: str) -> None:
        self.allowed.write_text(json.dumps(list(paths)) + "\n")

    def normalize(self, round_id: str = "round-1", env: dict[str, str] | None = None):
        run = subprocess.run(
            [
                sys.executable,
                str(NORMALIZER),
                "--run-dir",
                str(self.run_dir),
                "--repository",
                str(self.repo),
                "--allowed-paths-json",
                str(self.allowed),
                "--round-id",
                round_id,
            ],
            capture_output=True,
            text=True,
            env=env or self.env,
        )
        output = json.loads(run.stdout) if run.stdout.strip() else {}
        return run, output

    def make_git_wrapper(self, mutation: str) -> dict[str, str]:
        wrapper = self.bin / "git"
        real_git = shutil.which("git")
        if real_git is None:
            raise RuntimeError("git is required")
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "real = os.environ['REAL_GIT']; args = sys.argv[1:]\n"
            "result = subprocess.run([real, *args])\n"
            "if result.returncode == 0 and 'add' in args and '-N' in args:\n"
            f"    {mutation}\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        env = self.env.copy()
        env["REAL_GIT"] = real_git
        return env

    def make_pre_release_wrapper(self, artifact_dir: Path) -> dict[str, str]:
        wrapper = self.bin / "git"
        real_git = shutil.which("git")
        if real_git is None:
            raise RuntimeError("git is required")
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "real = os.environ['REAL_GIT']; args = sys.argv[1:]\n"
            "result = subprocess.run([real, *args])\n"
            "artifact_dir = pathlib.Path(os.environ['REVIEW_ARTIFACT_DIR'])\n"
            "mutation_marker = artifact_dir / '.pre-release-mutated'\n"
            "if result.returncode == 0 and (artifact_dir / 'metadata.json').is_file() and not mutation_marker.exists():\n"
            "    repo = pathlib.Path(args[args.index('-C') + 1])\n"
            "    (repo / 'new.txt').write_text('changed before release')\n"
            "    mutation_marker.touch()\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        env = self.env.copy()
        env["REAL_GIT"] = real_git
        env["REVIEW_ARTIFACT_DIR"] = str(artifact_dir)
        return env

    def artifact(self, output: dict) -> dict:
        return json.loads(Path(output["artifactPath"]).read_text())

    def run_reviewer(
        self,
        artifact: dict,
        *,
        code: str,
        artifact_dir: Path | None = None,
        env: dict[str, str] | None = None,
    ):
        artifact_dir = artifact_dir or self.root / "review"
        report_code = code
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--artifact-dir",
                str(artifact_dir),
                "--repository",
                str(self.repo),
                "--target-sha",
                self.git("rev-parse", "HEAD").stdout.strip(),
                "--review-kind", "code",
                "--normalization-artifact",
                artifact["artifactPath"],
                "--normalization-sha256",
                artifact["artifactSha256"],
                "--round-id",
                artifact["roundId"],
                "--",
                sys.executable,
                "-c",
                report_code,
            ],
            capture_output=True,
            text=True,
            env=env or self.env,
        )

    def reconcile(self, directory: Path):
        return subprocess.run(
            [
                sys.executable,
                str(RECONCILER),
                "--artifact-dir",
                str(directory),
                "--repository",
                str(self.repo),
                "--expected-head",
                self.git("rev-parse", "HEAD").stdout.strip(),
                "--interval",
                "0.01",
                "--timeout",
                "1",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_normalizes_approved_file_and_preserves_content(self) -> None:
        path = self.repo / "new file.txt"
        path.write_text("candidate\n")
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_allowed("new file.txt")
        run, output = self.normalize()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        artifact = self.artifact(output)
        self.assertEqual(artifact["paths"], ["new file.txt"])
        self.assertEqual(artifact["emptyBlobOid"], EMPTY_BLOB)
        self.assertEqual(artifact["before"]["untrackedFiles"][0]["sha256"], before_hash)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before_hash)
        entry = self.git("ls-files", "--stage", "--", "new file.txt").stdout
        self.assertIn(EMPTY_BLOB, entry)
        self.assertTrue(artifact["workingTreeContentUnchanged"])

    def test_round_evidence_cannot_be_overwritten(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        first_run, first = self.normalize()
        self.assertEqual(first_run.returncode, 0, first_run.stderr)
        artifact_path = Path(first["artifactPath"])
        original = artifact_path.read_bytes()

        second_run, second = self.normalize()
        self.assertNotEqual(second_run.returncode, 0)
        self.assertEqual(second["classification"], "failed")
        self.assertEqual(artifact_path.read_bytes(), original)
        self.assertTrue(artifact_path.with_suffix(".lock").is_dir())

    def test_concurrent_normalizers_have_one_exclusive_winner(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        command = [
            sys.executable, str(NORMALIZER),
            "--run-dir", str(self.run_dir),
            "--repository", str(self.repo),
            "--allowed-paths-json", str(self.allowed),
            "--round-id", "concurrent-round",
        ]
        processes = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
            )
            for _ in range(2)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate()
            results.append((process.returncode, stdout, stderr))
        self.assertEqual(sorted(result[0] for result in results), [0, 2])
        successful = json.loads(next(result[1] for result in results if result[0] == 0))
        artifact_path = Path(successful["artifactPath"])
        self.assertEqual(
            hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            successful["artifactSha256"],
        )
        self.assertTrue(artifact_path.with_suffix(".lock").is_dir())

    def test_sha256_repository_fails_before_intent_to_add(self) -> None:
        repository = self.root / "sha256-repo"
        remote = self.root / "sha256-remote.git"
        initialized = subprocess.run(
            ["git", "init", "-q", "--object-format=sha256", str(repository)],
            capture_output=True,
            text=True,
        )
        if initialized.returncode:
            self.skipTest("installed Git does not support SHA-256 repositories")
        subprocess.run(
            ["git", "init", "-q", "--bare", "--object-format=sha256", str(remote)],
            check=True,
        )
        for arguments in (
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.com"),
            ("config", "commit.gpgsign", "false"),
        ):
            subprocess.run(["git", "-C", str(repository), *arguments], check=True)
        (repository / "tracked.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(repository), "add", "--", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "base"], check=True)
        branch = subprocess.run(
            ["git", "-C", str(repository), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repository), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "push", "-q", "-u", "origin", branch],
            check=True,
        )
        (repository / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        run = subprocess.run(
            [
                sys.executable, str(NORMALIZER),
                "--run-dir", str(self.run_dir),
                "--repository", str(repository),
                "--allowed-paths-json", str(self.allowed),
                "--round-id", "sha256-round",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        output = json.loads(run.stdout)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("unsupported Git object format", " ".join(output["reasons"]))
        status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1", "--", "new.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "?? new.txt\n")

    def test_repeated_intent_to_add_is_idempotent_and_reconciles(self) -> None:
        (self.repo / "new.txt").write_text("candidate\n")
        self.write_allowed("new.txt")
        run, output = self.normalize()
        self.assertEqual(run.returncode, 0, run.stderr)
        before = self.git("status", "--porcelain=v1", "--untracked-files=all").stdout
        self.git("add", "-N", "--", "new.txt")
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all").stdout, before)
        review_dir = self.root / "review"
        artifact = {**output, "roundId": "round-1"}
        review = self.run_reviewer(
            artifact,
            artifact_dir=review_dir,
            code="import subprocess; subprocess.run(['git','add','-N','--','new.txt'],check=True); print('Review complete with detail.\\nHigh-priority findings: 0')",
        )
        self.assertEqual(review.returncode, 0, review.stderr)
        reconciled = self.reconcile(review_dir)
        self.assertEqual(reconciled.returncode, 0, reconciled.stdout + reconciled.stderr)
        self.assertEqual(json.loads(reconciled.stdout)["classification"], "confirmed")

    def test_multiple_reviewers_capture_same_normalized_baseline(self) -> None:
        (self.repo / "new.txt").write_text("candidate\n")
        self.write_allowed("new.txt")
        _, output = self.normalize()
        artifact = {**output, "roundId": "round-1"}
        dirs = [self.root / "review-a", self.root / "review-b"]
        for directory in dirs:
            run = self.run_reviewer(
                artifact,
                artifact_dir=directory,
                code="print('Review complete with detail.\\nHigh-priority findings: 0')",
            )
            self.assertEqual(run.returncode, 0, run.stderr)
        baselines = [json.loads((directory / "baseline.json").read_text()) for directory in dirs]
        self.assertEqual(baselines[0]["repositoryState"], baselines[1]["repositoryState"])
        self.assertEqual(baselines[0]["normalizationSha256"], baselines[1]["normalizationSha256"])

    def test_repository_change_before_child_release_blocks_reviewer(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        _, output = self.normalize()
        artifact = {**output, "roundId": "round-1"}
        artifact_dir = self.root / "pre-release-review"
        reviewer_marker = self.root / "reviewer-started"
        env = self.make_pre_release_wrapper(artifact_dir)
        run = self.run_reviewer(
            artifact,
            artifact_dir=artifact_dir,
            env=env,
            code=f"from pathlib import Path; Path({str(reviewer_marker)!r}).touch()",
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(reviewer_marker.exists())
        self.assertEqual((self.repo / "new.txt").read_text(), "changed before release")

    def test_publication_replaces_intent_to_add_with_real_staged_content(self) -> None:
        (self.repo / "new.txt").write_text("candidate\n")
        self.write_allowed("new.txt")
        run, _ = self.normalize()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.git("add", "--", "new.txt")
        entry = self.git("ls-files", "--stage", "--", "new.txt").stdout
        self.assertNotIn(EMPTY_BLOB, entry)
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout, "new.txt\n")

    def test_rejects_out_of_scope_or_unknown_untracked_file_without_normalizing_anything(self) -> None:
        (self.repo / "approved.txt").write_text("a")
        (self.repo / "unknown.txt").write_text("u")
        self.write_allowed("approved.txt")
        run, output = self.normalize()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(output["classification"], "failed")
        self.assertIn("unknown.txt", " ".join(output["reasons"]))
        self.assertEqual(self.git("status", "--porcelain=v1").stdout, "?? approved.txt\n?? unknown.txt\n")

    def test_rejects_index_change_outside_normalization_set(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        env = self.make_git_wrapper(
            "repo=pathlib.Path(args[args.index('-C') + 1]); (repo/'tracked.txt').write_text('changed'); subprocess.run([real, '-C', str(repo), 'add', '--', 'tracked.txt'], check=True)"
        )
        run, output = self.normalize(env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("staged diff", " ".join(output["reasons"]))

    def test_rejects_real_content_staged_for_normalized_path(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        env = self.make_git_wrapper(
            "subprocess.run([real, '-C', args[args.index('-C') + 1], 'add', '--', 'new.txt'], check=True)"
        )
        run, output = self.normalize(env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("staged diff", " ".join(output["reasons"]))

    def test_rejects_file_content_change_during_normalization(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        env = self.make_git_wrapper(
            "pathlib.Path(args[args.index('-C') + 1], 'new.txt').write_text('mutated')"
        )
        run, output = self.normalize(env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("working-tree content", " ".join(output["reasons"]))

    def test_rejects_head_or_reflog_change(self) -> None:
        for mutation in (
            "repo=pathlib.Path(args[args.index('-C') + 1]); (repo/'commit.txt').write_text('x'); subprocess.run([real,'-C',str(repo),'add','--','commit.txt'],check=True); subprocess.run([real,'-C',str(repo),'-c','commit.gpgsign=false','commit','-q','-m','mutation'],check=True)",
            "repo=pathlib.Path(args[args.index('-C') + 1]); (repo/'temporary-commit.txt').write_text('x'); subprocess.run([real,'-C',str(repo),'add','--','temporary-commit.txt'],check=True); subprocess.run([real,'-C',str(repo),'-c','commit.gpgsign=false','commit','-q','-m','temporary'],check=True); subprocess.run([real,'-C',str(repo),'reset','--hard','HEAD^'],check=True,capture_output=True)",
            "repo=pathlib.Path(args[args.index('-C') + 1]); branch=subprocess.run([real,'-C',str(repo),'branch','--show-current'],check=True,capture_output=True,text=True).stdout.strip(); subprocess.run([real,'-C',str(repo),'checkout','-q','--detach','HEAD'],check=True); subprocess.run([real,'-C',str(repo),'checkout','-q',branch],check=True)",
        ):
            with self.subTest(mutation=mutation[:20]):
                if (self.repo / "new.txt").exists():
                    self.git("reset", "--hard", "-q", f"origin/{self.branch}")
                    (self.repo / "new.txt").unlink(missing_ok=True)
                (self.repo / "new.txt").write_text("candidate")
                self.write_allowed("new.txt")
                env = self.make_git_wrapper(mutation)
                run, output = self.normalize(round_id=f"round-{len(list((self.run_dir / 'review-normalizations').glob('*')))}", env=env)
                self.assertNotEqual(run.returncode, 0)
                self.assertRegex(" ".join(output["reasons"]), "HEAD|reflog")

    def test_rejects_ref_change(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        env = self.make_git_wrapper(
            "repo=pathlib.Path(args[args.index('-C') + 1]); subprocess.run([real,'-C',str(repo),'update-ref','refs/heads/reviewer-side-effect','HEAD'],check=True)"
        )
        run, output = self.normalize(env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("ref changed", " ".join(output["reasons"]))

    def test_rejects_remote_or_pull_request_change(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        mutation = (
            "repo=pathlib.Path(args[args.index('-C') + 1]); "
            "branch=subprocess.run([real,'-C',str(repo),'branch','--show-current'],check=True,capture_output=True,text=True).stdout.strip(); "
            "tree=subprocess.run([real,'-C',str(repo),'rev-parse','HEAD^{tree}'],check=True,capture_output=True,text=True).stdout.strip(); "
            "oid=subprocess.run([real,'-C',str(repo),'commit-tree',tree,'-m','remote mutation'],check=True,capture_output=True,text=True).stdout.strip(); "
            "subprocess.run([real,'-C',str(repo),'push','-q','origin',oid+':refs/heads/'+branch],check=True)"
        )
        env = self.make_git_wrapper(mutation)
        run, output = self.normalize(env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("remote", " ".join(output["reasons"]))

    def test_rejects_pull_request_change(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        env = self.make_git_wrapper(
            "pathlib.Path(os.environ['FAKE_GH_STATE']).write_text('[{\"number\": 1}]')"
        )
        run, output = self.normalize(env=env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("pull-request", " ".join(output["reasons"]))

    def test_reviewer_mutations_remain_side_effects(self) -> None:
        cases = {
            "new intent": "from pathlib import Path; import subprocess; Path('other.txt').write_text('x'); subprocess.run(['git','add','-N','--','other.txt'],check=True)",
            "ordinary add": "import subprocess; subprocess.run(['git','add','--','new.txt'],check=True)",
            "file edit": "from pathlib import Path; Path('new.txt').write_text('edited')",
            "checkout round trip": f"import subprocess; subprocess.run(['git','checkout','-q','--detach','HEAD'],check=True); subprocess.run(['git','checkout','-q',{self.branch!r}],check=True)",
            "ref change": "import subprocess; subprocess.run(['git','update-ref','refs/heads/reviewer-side-effect','HEAD'],check=True)",
            "Git config change": "import subprocess; subprocess.run(['git','config','reviewer.side-effect','true'],check=True)",
        }
        for index, (label, mutation) in enumerate(cases.items()):
            with self.subTest(label=label):
                self.git("reset", "--hard", "-q", f"origin/{self.branch}")
                for path in (self.repo / "new.txt", self.repo / "other.txt"):
                    path.unlink(missing_ok=True)
                (self.repo / "new.txt").write_text("candidate")
                self.write_allowed("new.txt")
                _, output = self.normalize(round_id=f"review-round-{index}")
                artifact = {**output, "roundId": f"review-round-{index}"}
                directory = self.root / f"review-{index}"
                code = mutation + "; print('Review complete with detail.\\nHigh-priority findings: 0')"
                run = self.run_reviewer(artifact, artifact_dir=directory, code=code)
                self.assertEqual(run.returncode, 0, run.stderr)
                reconciled = self.reconcile(directory)
                self.assertEqual(json.loads(reconciled.stdout)["classification"], "side_effect_detected")

    def test_review_launch_rejects_missing_or_tampered_normalization_artifact(self) -> None:
        (self.repo / "new.txt").write_text("candidate")
        self.write_allowed("new.txt")
        _, output = self.normalize()
        artifact = {**output, "roundId": "round-1"}
        marker = self.root / "reviewer-started"
        missing = {**artifact, "artifactPath": str(self.root / "missing-normalization.json")}
        missing_run = self.run_reviewer(
            missing,
            artifact_dir=self.root / "missing-review",
            code=f"from pathlib import Path; Path({str(marker)!r}).touch()",
        )
        self.assertNotEqual(missing_run.returncode, 0)
        self.assertFalse(marker.exists())

        Path(output["artifactPath"]).write_text("{}\n")
        run = self.run_reviewer(
            artifact,
            artifact_dir=self.root / "tampered-review",
            code=f"from pathlib import Path; Path({str(marker)!r}).touch()",
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(marker.exists())
        Path(output["artifactPath"]).unlink()
        run = self.run_reviewer(
            artifact,
            artifact_dir=self.root / "missing-review",
            code=f"from pathlib import Path; Path({str(marker)!r}).touch()",
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(marker.exists())

    def test_reviewer_normalization_evidence_tampering_is_a_side_effect(self) -> None:
        for index, target in enumerate(("round-claim", "before-evidence")):
            with self.subTest(target=target):
                self.git("reset", "--hard", "-q", f"origin/{self.branch}")
                (self.repo / "new.txt").unlink(missing_ok=True)
                (self.repo / "new.txt").write_text("candidate")
                self.write_allowed("new.txt")
                round_id = f"evidence-round-{index}"
                _, output = self.normalize(round_id=round_id)
                artifact = {**output, "roundId": round_id}
                if target == "round-claim":
                    evidence = Path(output["artifactPath"]).with_suffix(".lock")
                    mutation = f"from pathlib import Path; Path({str(evidence)!r}).rmdir()"
                else:
                    evidence = Path(output["beforeEvidencePath"])
                    mutation = f"from pathlib import Path; Path({str(evidence)!r}).write_text('{{}}')"
                directory = self.root / f"evidence-review-{index}"
                run = self.run_reviewer(
                    artifact,
                    artifact_dir=directory,
                    code=mutation + "; print('Review complete with detail.\\nHigh-priority findings: 0')",
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                reconciled = self.reconcile(directory)
                result = json.loads(reconciled.stdout)
                self.assertEqual(result["classification"], "side_effect_detected")


if __name__ == "__main__":
    unittest.main()
