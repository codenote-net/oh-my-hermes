from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.error import HTTPError, URLError

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import create_dependabot_cooldown_issue as tool  # noqa: E402

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
REPOSITORY = tool.Repository("OWNER", "REPOSITORY")


def alert(number: int = 41, *, patched: str | None = "2.0.0", package: str = "pkg") -> dict:
    return {
        "number": number,
        "created_at": "2026-01-01T00:00:00Z",
        "dependency": {
            "package": {"name": package, "ecosystem": "npm"},
            "manifest_path": "package.json",
            "scope": "runtime",
        },
        "security_vulnerability": {
            "vulnerable_version_range": "< 2.0.0",
            "first_patched_version": None if patched is None else {"identifier": patched},
        },
        "security_advisory": {
            "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
            "cve_id": "CVE-2026-0001",
            "severity": "high",
        },
    }


class FakeNpm:
    def __init__(self, published: datetime = NOW - timedelta(days=2)):
        self.published = published
        self.calls: list[tuple[str, str]] = []

    def published_at(self, package: str, version: str) -> datetime:
        self.calls.append((package, version))
        return self.published


class FakeGh:
    def __init__(self, *, alerts: list[dict] | None = None, issues: list[dict] | None = None):
        self.alerts = alerts or []
        self.issues = issues or []
        self.issue_reads = 0
        self.posts: list[dict] = []
        self.labels = [{"name": "security"}, {"name": "dependencies"}]
        self.workspace: str | None = None
        self.metadata = {"full_name": REPOSITORY.full_name, "default_branch": "trunk"}

    def api(self, endpoint: str, *, paginate=False, method="GET", payload=None):
        if endpoint == f"/repos/{REPOSITORY.full_name}":
            return self.metadata
        if "/contents/pnpm-workspace.yaml" in endpoint:
            if self.workspace is None:
                raise tool.GitHubApiError("GitHub API failed: HTTP 404", 404)
            return {"encoding": "base64", "content": base64.b64encode(self.workspace.encode()).decode()}
        if "/dependabot/alerts" in endpoint:
            return self.alerts
        if endpoint.endswith("/issues?state=all&per_page=100"):
            self.issue_reads += 1
            return self.issues
        if endpoint.endswith("/labels?per_page=100"):
            return self.labels
        if endpoint.endswith("/issues") and method == "POST":
            self.posts.append(payload)
            return {"html_url": "https://github.com/OWNER/REPOSITORY/issues/99"}
        raise AssertionError(endpoint)


def options(*, apply=False, age=None, labels=("security", "dependencies")) -> tool.Options:
    return tool.Options(REPOSITORY.url, apply, age, labels, tool.DEFAULT_TITLE)


def run_execute(gh: FakeGh, *, apply=False, age=None, npm=None):
    stdout, stderr = StringIO(), StringIO()
    code = tool.execute(
        options(apply=apply, age=age),
        gh=gh,
        npm=npm or FakeNpm(),
        now=NOW,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.value


class DependabotCooldownTests(unittest.TestCase):
    def test_01_valid_repository_url(self):
        self.assertEqual(tool.parse_repository_url(REPOSITORY.url), REPOSITORY)

    def test_02_invalid_repository_url(self):
        with self.assertRaises(tool.ToolError):
            tool.parse_repository_url("https://example.com/OWNER/REPOSITORY")

    def test_03_issue_url_rejected(self):
        with self.assertRaises(tool.ToolError):
            tool.parse_repository_url(REPOSITORY.url + "/issues/1")

    def test_04_pull_request_url_rejected(self):
        with self.assertRaises(tool.ToolError):
            tool.parse_repository_url(REPOSITORY.url + "/pull/1")

    def test_05_ssh_url_rejected(self):
        with self.assertRaises(tool.ToolError):
            tool.parse_repository_url("git@github.com:OWNER/REPOSITORY.git")

    def test_06_query_fragment_and_credentials_rejected(self):
        for value in (
            REPOSITORY.url + "?x=1",
            REPOSITORY.url + "#x",
            "https://user:token@github.com/OWNER/REPOSITORY",
        ):
            with self.subTest(value=value), self.assertRaises(tool.ToolError):
                tool.parse_repository_url(value)

    def test_07_repository_identity_mismatch_fails_closed(self):
        gh = FakeGh()
        gh.metadata["full_name"] = "OTHER/REPOSITORY"
        with self.assertRaises(tool.ToolError):
            tool.fetch_repository_metadata(gh, REPOSITORY)

    def test_08_dependabot_pagination_flattens_pages(self):
        runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, '[[{"number":1}],[{"number":2}]]', "")
        data = tool.GhClient(runner).api("/alerts", paginate=True)
        self.assertEqual([item["number"] for item in data], [1, 2])

    def test_09_issue_pagination_flattens_pages(self):
        runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, '[[{"number":1}],[]]', "")
        self.assertEqual(len(tool.GhClient(runner).api("/issues", paginate=True)), 1)

    def test_10_default_branch_metadata(self):
        self.assertEqual(tool.fetch_repository_metadata(FakeGh(), REPOSITORY), "trunk")

    def test_11_workspace_404_uses_default(self):
        self.assertEqual(tool.repository_cooldown(FakeGh(), REPOSITORY, "trunk").minutes, 1440)

    def test_12_github_failures_are_not_empty_results(self):
        for result in (
            subprocess.CompletedProcess([], 1, "", "HTTP 403"),
            subprocess.CompletedProcess([], 1, "", "HTTP 500"),
            subprocess.CompletedProcess([], 0, "{", ""),
        ):
            runner = lambda *a, result=result, **k: result
            with self.subTest(result=result), self.assertRaises(tool.ToolError):
                tool.GhClient(runner).api("/endpoint")
        def timeout(*_a, **_k):
            raise subprocess.TimeoutExpired(["gh"], 1)
        with self.assertRaises(tool.ToolError):
            tool.GhClient(timeout).api("/endpoint")

    def test_13_npm_failures_fail_closed(self):
        for failure in (
            URLError("timeout"),
            HTTPError("x", 500, "error", {}, None),
            TimeoutError(),
        ):
            def opener(*_a, failure=failure, **_k):
                raise failure
            with self.subTest(failure=failure), self.assertRaises(tool.ToolError):
                tool.NpmClient(opener).metadata("pkg")
        with self.assertRaises(tool.ToolError):
            tool.NpmClient(lambda *_a, **_k: Response(b"{")).metadata("pkg")

    def test_14_scoped_package_is_url_encoded(self):
        seen = []
        def opener(request, **_):
            seen.append(request.full_url)
            return Response(b'{"time":{"1.0.0":"2026-01-01T00:00:00Z"}}')
        tool.NpmClient(opener).published_at("@scope/pkg", "1.0.0")
        self.assertEqual(seen[0], "https://registry.npmjs.org/%40scope%2Fpkg")

    def test_15_cli_override_wins(self):
        gh = FakeGh()
        gh.workspace = "minimumReleaseAge: 10"
        self.assertEqual(tool.resolve_cooldown(20, gh, REPOSITORY, "trunk"), tool.Cooldown(20, "CLI override"))

    def test_16_workspace_age_is_parsed(self):
        self.assertEqual(tool.parse_minimum_release_age("minimumReleaseAge: 10080\n"), 10080)

    def test_17_repository_setting_is_used_without_override(self):
        gh = FakeGh()
        gh.workspace = "minimumReleaseAge: 10080"
        self.assertEqual(tool.resolve_cooldown(None, gh, REPOSITORY, "trunk").source, "pnpm-workspace.yaml")

    def test_18_default_is_1440(self):
        self.assertEqual(tool.resolve_cooldown(None, FakeGh(), REPOSITORY, "trunk").minutes, 1440)

    def test_19_missing_workspace_is_default(self):
        self.assertEqual(tool.repository_cooldown(FakeGh(), REPOSITORY, "trunk").source, "pnpm 11 default")

    def test_20_workspace_without_setting_is_default(self):
        gh = FakeGh()
        gh.workspace = "packages:\n  - packages/*"
        self.assertEqual(tool.repository_cooldown(gh, REPOSITORY, "trunk").minutes, 1440)

    def test_21_zero_is_valid(self):
        self.assertEqual(tool.parse_minimum_release_age("minimumReleaseAge: 0"), 0)

    def test_22_invalid_workspace_values_fail_closed(self):
        for value in ("-1", "1.5", "true", '"10"', "${AGE}"):
            with self.subTest(value=value), self.assertRaises(tool.ToolError):
                tool.parse_minimum_release_age(f"minimumReleaseAge: {value}")

    def test_23_404_is_distinct_from_other_api_errors(self):
        class ErrorGh(FakeGh):
            def api(self, *_a, **_k):
                raise tool.GitHubApiError("HTTP 403", 403)
        with self.assertRaises(tool.ToolError):
            tool.repository_cooldown(ErrorGh(), REPOSITORY, "trunk")

    def test_24_age_and_source_appear_in_dry_run_and_body(self):
        gh = FakeGh(alerts=[alert()])
        _, output, _ = run_execute(gh, age=10)
        self.assertIn("minimumReleaseAge: 10 minutes", output)
        self.assertIn("Source: CLI override", output)

    def test_25_before_cooldown_is_ineligible(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm(NOW - timedelta(minutes=9)))
        self.assertFalse(tool.is_eligible(item, tool.Cooldown(10, "x"), NOW))

    def test_26_exact_boundary_is_eligible(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm(NOW - timedelta(minutes=10)))
        self.assertTrue(tool.is_eligible(item, tool.Cooldown(10, "x"), NOW))

    def test_27_after_boundary_is_eligible(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm(NOW - timedelta(minutes=11)))
        self.assertTrue(tool.is_eligible(item, tool.Cooldown(10, "x"), NOW))

    def test_28_fixed_now_produces_deterministic_boundary(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm(NOW - timedelta(days=1)))
        self.assertEqual(tool.elapsed_text(item, NOW), "1440 minutes")

    def test_29_first_patched_version_is_normalized(self):
        item, reason = tool.normalize_alert(alert(), REPOSITORY, FakeNpm())
        self.assertIsNone(reason)
        self.assertEqual(item.patched_version, "2.0.0")

    def test_30_missing_first_patched_version_is_excluded(self):
        item, reason = tool.normalize_alert(alert(patched=None), REPOSITORY, FakeNpm())
        self.assertIsNone(item)
        self.assertIn("not available", reason)

    def test_31_missing_npm_publish_time_fails(self):
        client = tool.NpmClient(lambda *_a, **_k: Response(b'{"time":{}}'))
        with self.assertRaises(tool.ToolError):
            client.published_at("pkg", "2.0.0")

    def test_32_malformed_timestamp_fails(self):
        with self.assertRaises(tool.ToolError):
            tool.parse_timestamp("yesterday", "timestamp")

    def test_33_npm_metadata_is_cached(self):
        calls = []
        def opener(*_a, **_k):
            calls.append(1)
            return Response(b'{"time":{"1":"2026-01-01T00:00:00Z","2":"2026-01-02T00:00:00Z"}}')
        client = tool.NpmClient(opener)
        client.published_at("pkg", "1")
        client.published_at("pkg", "2")
        self.assertEqual(len(calls), 1)

    def test_34_open_issue_marker_is_tracked(self):
        body = f"{tool.MARKER}\n<!-- repository:{REPOSITORY.full_name} -->\n<!-- dependabot-alerts:41 -->"
        self.assertEqual(tool.tracked_alert_numbers([{"body": body}], REPOSITORY), {41})

    def test_35_closed_issue_marker_is_tracked(self):
        body = f"{tool.MARKER}\n<!-- repository:{REPOSITORY.full_name} -->\n<!-- dependabot-alerts:41 -->"
        self.assertEqual(tool.tracked_alert_numbers([{"state": "closed", "body": body}], REPOSITORY), {41})

    def test_36_pull_request_marker_is_ignored(self):
        body = f"{tool.MARKER}\n<!-- repository:{REPOSITORY.full_name} -->\n<!-- dependabot-alerts:41 -->"
        self.assertEqual(tool.tracked_alert_numbers([{"body": body, "pull_request": {}}], REPOSITORY), set())

    def test_37_only_untracked_alerts_are_created(self):
        body = f"{tool.MARKER}\n<!-- repository:{REPOSITORY.full_name} -->\n<!-- dependabot-alerts:41 -->"
        gh = FakeGh(alerts=[alert(41), alert(42)], issues=[{"body": body}])
        run_execute(gh, apply=True, age=0)
        self.assertIn("dependabot-alerts:42", gh.posts[0]["body"])

    def test_38_no_eligible_alert_has_empty_apply_stdout(self):
        code, output, _ = run_execute(FakeGh(), apply=True)
        self.assertEqual((code, output), (0, ""))

    def test_39_dry_run_never_mutates(self):
        gh = FakeGh(alerts=[alert()])
        run_execute(gh, age=0)
        self.assertEqual(gh.posts, [])
        self.assertFalse(tool.build_parser().parse_args([REPOSITORY.url]).apply)

    def test_40_apply_creates_one_issue(self):
        gh = FakeGh(alerts=[alert()])
        code, output, _ = run_execute(gh, apply=True, age=0)
        self.assertEqual(code, 0)
        self.assertEqual(output, "https://github.com/OWNER/REPOSITORY/issues/99\n")

    def test_41_dry_run_and_apply_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            tool.build_parser().parse_args([REPOSITORY.url, "--dry-run", "--apply"])

    def test_42_issue_links_are_full_urls(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm())
        body = tool.render_issue(REPOSITORY, tool.Cooldown(0, "CLI override"), [item], NOW)
        self.assertIn(REPOSITORY.url, body)
        self.assertIn(REPOSITORY.url + "/security/dependabot/41", body)
        self.assertIn("https://github.com/advisories/GHSA-", body)

    def test_43_secret_redaction(self):
        cleaned = tool.redact("Authorization: secret token=abc cookie=xyz gho_123456")
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("abc", cleaned)
        self.assertNotIn("xyz", cleaned)
        self.assertNotIn("gho_123456", cleaned)

    def test_44_missing_label_is_warned_and_skipped(self):
        gh = FakeGh(alerts=[alert()])
        gh.labels = [{"name": "security"}]
        _, _, errors = run_execute(gh, apply=True, age=0)
        self.assertEqual(gh.posts[0]["labels"], ["security"])
        self.assertIn("dependencies", errors)

    def test_45_duplicate_alerts_within_run_are_removed(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm())
        self.assertEqual(len(tool.deduplicate_alerts([item, item])), 1)

    def test_46_alert_numbers_are_sorted(self):
        items = [tool.normalize_alert(alert(number), REPOSITORY, FakeNpm())[0] for number in (42, 3, 41)]
        self.assertEqual([item.number for item in tool.deduplicate_alerts(items)], [3, 41, 42])

    def test_47_body_is_stable_for_fixed_inputs(self):
        item, _ = tool.normalize_alert(alert(), REPOSITORY, FakeNpm())
        first = tool.render_issue(REPOSITORY, tool.Cooldown(0, "CLI override"), [item], NOW)
        second = tool.render_issue(REPOSITORY, tool.Cooldown(0, "CLI override"), [item], NOW)
        self.assertEqual(first, second)

    def test_48_duplicate_check_is_repeated_before_mutation(self):
        gh = FakeGh(alerts=[alert()])
        run_execute(gh, apply=True, age=0)
        self.assertEqual(gh.issue_reads, 2)


if __name__ == "__main__":
    unittest.main()
