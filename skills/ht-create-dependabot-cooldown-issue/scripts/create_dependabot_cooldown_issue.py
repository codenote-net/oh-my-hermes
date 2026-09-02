#!/usr/bin/env python3
"""Create one issue for npm Dependabot fixes whose release cooldown has elapsed."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_AGE_MINUTES = 1440
DEFAULT_LABELS = ("security", "dependencies")
DEFAULT_TITLE = "Fix Dependabot security alerts past the release cooldown"
MARKER = "<!-- ht-create-dependabot-cooldown-issue:v1 -->"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")
ALERTS_RE = re.compile(r"<!-- dependabot-alerts:([0-9]+(?:,[0-9]+)*) -->")
REPOSITORY_MARKER_RE = re.compile(r"<!-- repository:([^\s]+) -->")
TOP_LEVEL_AGE_RE = re.compile(r"^minimumReleaseAge:[ \\t]*([0-9]+)[ \\t]*(?:#.*)?$")


class ToolError(RuntimeError):
    pass


class GitHubApiError(ToolError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


@dataclass(frozen=True)
class Cooldown:
    minutes: int
    source: str


@dataclass(frozen=True)
class Alert:
    number: int
    url: str
    package: str
    ecosystem: str
    manifest_path: str
    scope: str
    severity: str
    ghsa_id: str
    cve_id: str | None
    vulnerable_range: str
    patched_version: str
    advisory_url: str
    created_at: str
    published_at: datetime


def parse_repository_url(value: str) -> Repository:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ToolError(f"invalid repository URL: {error}") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ToolError("repository URL must match https://github.com/OWNER/REPOSITORY exactly")
    parts = parsed.path.split("/")
    if len(parts) != 3 or not all(parts[1:]):
        raise ToolError("repository URL must not be an issue, pull request, API URL, or nested path")
    owner, repository = parts[1:]
    if repository.endswith(".git") or not REPOSITORY_RE.fullmatch(owner) or not REPOSITORY_RE.fullmatch(repository):
        raise ToolError("repository owner or name is invalid")
    return Repository(owner, repository)


def redact(text: str) -> str:
    text = re.sub(r"(?i)(authorization\s*:\s*)([^\s]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(token|bearer|cookie)=?\s*[^\s]+", r"\1=[REDACTED]", text)
    text = re.sub(r"\b(?:gh[pousr]_|npm_)[A-Za-z0-9_]+\b", "[REDACTED]", text)
    return text


class GhClient:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.runner = runner

    def api(
        self,
        endpoint: str,
        *,
        paginate: bool = False,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        command = ["gh", "api", "--method", method, endpoint]
        if paginate:
            command += ["--paginate", "--slurp"]
        input_text = None
        if payload is not None:
            command += ["--input", "-"]
            input_text = json.dumps(payload)
        try:
            result = self.runner(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError(f"GitHub API command failed: {redact(str(error))}") from error
        if result.returncode:
            message = redact((result.stderr or result.stdout).strip())
            status_match = re.search(r"\bHTTP\s+([0-9]{3})\b", message)
            status = int(status_match.group(1)) if status_match else None
            raise GitHubApiError(f"GitHub API {method} {endpoint} failed: {message}", status)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ToolError(f"GitHub API returned invalid JSON for {endpoint}") from error
        if paginate:
            if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
                raise ToolError(f"GitHub paginated response has an invalid shape for {endpoint}")
            return [item for page in data for item in page]
        return data


class NpmClient:
    def __init__(self, opener: Callable[..., Any] = urlopen, timeout: float = 15):
        self.opener = opener
        self.timeout = timeout
        self.cache: dict[str, dict[str, Any]] = {}

    def metadata(self, package: str) -> dict[str, Any]:
        if package in self.cache:
            return self.cache[package]
        url = f"https://registry.npmjs.org/{quote(package, safe='')}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "ht-dependabot-cooldown/1"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise ToolError(f"npm registry request failed for {package}: {redact(str(error))}") from error
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ToolError(f"npm registry returned invalid JSON for {package}") from error
        if not isinstance(data, dict):
            raise ToolError(f"npm registry metadata has an invalid shape for {package}")
        self.cache[package] = data
        return data

    def published_at(self, package: str, version: str) -> datetime:
        times = self.metadata(package).get("time")
        if not isinstance(times, dict) or version not in times:
            raise ToolError(f"npm publish time is missing for {package}@{version}")
        return parse_timestamp(times[version], f"npm publish time for {package}@{version}")


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ToolError(f"{label} is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ToolError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise ToolError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_minimum_release_age(content: str) -> int | None:
    values: list[int] = []
    for raw_line in content.splitlines():
        if raw_line.startswith((" ", "\\t")):
            continue
        if re.match(r"^minimumReleaseAge(?:\s*:)", raw_line):
            match = TOP_LEVEL_AGE_RE.fullmatch(raw_line)
            if not match:
                raise ToolError("top-level minimumReleaseAge must be a plain non-negative integer")
            values.append(int(match.group(1)))
    if len(values) > 1:
        raise ToolError("pnpm-workspace.yaml contains duplicate top-level minimumReleaseAge values")
    return values[0] if values else None


def repository_cooldown(gh: GhClient, repository: Repository, default_branch: str) -> Cooldown:
    endpoint = (
        f"/repos/{repository.full_name}/contents/pnpm-workspace.yaml"
        f"?ref={quote(default_branch, safe='')}"
    )
    try:
        data = gh.api(endpoint)
    except GitHubApiError as error:
        if error.status == 404:
            return Cooldown(DEFAULT_AGE_MINUTES, "pnpm 11 default")
        raise
    if not isinstance(data, dict) or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        raise ToolError("pnpm-workspace.yaml API response is incomplete")
    try:
        encoded = "".join(data["content"].split())
        content = base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise ToolError("pnpm-workspace.yaml content is invalid") from error
    value = parse_minimum_release_age(content)
    return Cooldown(value, "pnpm-workspace.yaml") if value is not None else Cooldown(
        DEFAULT_AGE_MINUTES, "pnpm 11 default"
    )


def resolve_cooldown(
    override: int | None, gh: GhClient, repository: Repository, default_branch: str
) -> Cooldown:
    if override is not None:
        return Cooldown(override, "CLI override")
    return repository_cooldown(gh, repository, default_branch)


def normalize_alert(raw: dict[str, Any], repository: Repository, npm: NpmClient) -> tuple[Alert | None, str | None]:
    try:
        number = raw["number"]
        dependency = raw["dependency"]
        package = dependency["package"]
        vulnerability = raw["security_vulnerability"]
        advisory = raw["security_advisory"]
        patched = vulnerability.get("first_patched_version")
        if patched is None:
            return None, f"alert {number}: first patched version is not available"
        version = patched.get("identifier")
        if not isinstance(number, int) or not isinstance(version, str) or not version:
            raise KeyError("number or patched version")
        package_name = package["name"]
        ecosystem = package["ecosystem"]
        if ecosystem.lower() != "npm":
            return None, f"alert {number}: ecosystem is not npm"
        ghsa_id = advisory["ghsa_id"]
        alert = Alert(
            number=number,
            url=f"{repository.url}/security/dependabot/{number}",
            package=package_name,
            ecosystem=ecosystem,
            manifest_path=dependency.get("manifest_path") or "(unknown)",
            scope=dependency.get("scope") or "(unknown)",
            severity=advisory["severity"],
            ghsa_id=ghsa_id,
            cve_id=advisory.get("cve_id"),
            vulnerable_range=vulnerability["vulnerable_version_range"],
            patched_version=version,
            advisory_url=f"https://github.com/advisories/{ghsa_id}",
            created_at=raw["created_at"],
            published_at=npm.published_at(package_name, version),
        )
    except (KeyError, TypeError) as error:
        raise ToolError("Dependabot alert response is incomplete") from error
    return alert, None


def is_eligible(alert: Alert, cooldown: Cooldown, now: datetime) -> bool:
    return now >= alert.published_at + timedelta(minutes=cooldown.minutes)


def tracked_alert_numbers(issues: Iterable[dict[str, Any]], repository: Repository) -> set[int]:
    tracked: set[int] = set()
    for issue in issues:
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        body = issue.get("body")
        if not isinstance(body, str) or MARKER not in body:
            continue
        repository_match = REPOSITORY_MARKER_RE.search(body)
        alerts_match = ALERTS_RE.search(body)
        if (
            repository_match
            and repository_match.group(1).lower() == repository.full_name.lower()
            and alerts_match
        ):
            tracked.update(int(value) for value in alerts_match.group(1).split(","))
    return tracked


def deduplicate_alerts(alerts: Iterable[Alert]) -> list[Alert]:
    return sorted({alert.number: alert for alert in alerts}.values(), key=lambda alert: alert.number)


def elapsed_text(alert: Alert, now: datetime) -> str:
    minutes = int((now - alert.published_at).total_seconds() // 60)
    return f"{minutes} minutes"


def render_issue(
    repository: Repository,
    cooldown: Cooldown,
    alerts: list[Alert],
    now: datetime,
) -> str:
    numbers = ",".join(str(alert.number) for alert in alerts)
    sections = [
        MARKER,
        f"<!-- repository:{repository.full_name} -->",
        f"<!-- dependabot-alerts:{numbers} -->",
        "",
        "## Summary",
        "",
        f"- Repository: {repository.url}",
        f"- Run at (UTC): {now.isoformat()}",
        f"- minimumReleaseAge: {cooldown.minutes} minutes",
        f"- Source: {cooldown.source}",
        f"- Eligible alert count: {len(alerts)}",
        "",
        "## Eligible alerts",
    ]
    for alert in alerts:
        sections += [
            "",
            f"### {alert.package} — {alert.severity}",
            "",
            f"- Vulnerable range: `{alert.vulnerable_range}`",
            f"- First patched version: `{alert.patched_version}`",
            f"- Patched version published at: {alert.published_at.isoformat()}",
            f"- Elapsed: {elapsed_text(alert, now)}",
            f"- Manifest: `{alert.manifest_path}`",
            f"- Dependency scope: `{alert.scope}`",
            f"- Dependabot alert: {alert.url}",
            f"- GitHub Advisory: {alert.advisory_url}",
        ]
        if alert.cve_id:
            sections.append(f"- CVE: {alert.cve_id}")
    sections += [
        "",
        "## Completion criteria",
        "",
        "- Update each affected dependency to the first patched version or later",
        "- Complete all repository-required validation",
        "- Confirm that every listed Dependabot alert is resolved",
        "",
        "## Recommended validation",
        "",
        "- Lockfile and dependency manifest consistency",
        "- lint、typecheck、test、build",
        "- Behavioral and security regressions caused by dependency changes",
        "",
    ]
    return "\n".join(sections)


def fetch_repository_metadata(gh: GhClient, repository: Repository) -> str:
    data = gh.api(f"/repos/{repository.full_name}")
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("full_name"), str)
        or not isinstance(data.get("default_branch"), str)
        or not data["default_branch"]
    ):
        raise ToolError("repository metadata is incomplete")
    if data["full_name"].lower() != repository.full_name.lower():
        raise ToolError(
            f"repository identity changed from {repository.full_name} to {data['full_name']}; refusing mutation"
        )
    return data["default_branch"]


def list_alerts(gh: GhClient, repository: Repository) -> list[dict[str, Any]]:
    endpoint = (
        f"/repos/{repository.full_name}/dependabot/alerts"
        "?state=open&ecosystem=npm&has=patch&per_page=100"
    )
    return gh.api(endpoint, paginate=True)


def list_issues(gh: GhClient, repository: Repository) -> list[dict[str, Any]]:
    return gh.api(f"/repos/{repository.full_name}/issues?state=all&per_page=100", paginate=True)


def available_labels(gh: GhClient, repository: Repository) -> set[str]:
    labels = gh.api(f"/repos/{repository.full_name}/labels?per_page=100", paginate=True)
    if any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
        raise ToolError("repository labels response is incomplete")
    return {label["name"] for label in labels}


@dataclass
class Options:
    repository_url: str
    apply: bool
    minimum_release_age_minutes: int | None
    labels: tuple[str, ...]
    title: str


def execute(
    options: Options,
    *,
    gh: GhClient,
    npm: NpmClient,
    now: datetime,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    repository = parse_repository_url(options.repository_url)
    default_branch = fetch_repository_metadata(gh, repository)
    cooldown = resolve_cooldown(options.minimum_release_age_minutes, gh, repository, default_branch)
    raw_alerts = list_alerts(gh, repository)
    normalized: list[Alert] = []
    excluded: list[str] = []
    for raw in raw_alerts:
        alert, reason = normalize_alert(raw, repository, npm)
        if reason:
            excluded.append(reason)
        elif alert and is_eligible(alert, cooldown, now):
            normalized.append(alert)
        elif alert:
            excluded.append(f"alert {alert.number}: cooldown has not elapsed")
    eligible = deduplicate_alerts(normalized)
    tracked = tracked_alert_numbers(list_issues(gh, repository), repository)
    for alert in eligible:
        if alert.number in tracked:
            excluded.append(f"alert {alert.number}: already tracked")
    untracked = [alert for alert in eligible if alert.number not in tracked]

    body = render_issue(repository, cooldown, untracked, now) if untracked else ""
    if not options.apply:
        print(f"minimumReleaseAge: {cooldown.minutes} minutes", file=stdout)
        print(f"Source: {cooldown.source}", file=stdout)
        print("Eligible alerts: " + (",".join(str(alert.number) for alert in untracked) or "None"), file=stdout)
        print("Excluded:", file=stdout)
        for reason in sorted(excluded):
            print(f"- {reason}", file=stdout)
        print(f"Title: {options.title}", file=stdout)
        print("Body:", file=stdout)
        print(body or "(no issue would be created)", file=stdout)
        return 0
    if not untracked:
        return 0

    tracked_again = tracked_alert_numbers(list_issues(gh, repository), repository)
    untracked = [alert for alert in untracked if alert.number not in tracked_again]
    if not untracked:
        return 0
    body = render_issue(repository, cooldown, untracked, now)
    existing_labels = {label.casefold(): label for label in available_labels(gh, repository)}
    selected_labels = [existing_labels[label.casefold()] for label in options.labels if label.casefold() in existing_labels]
    for label in options.labels:
        if label.casefold() not in existing_labels:
            print(f"warning: label does not exist and will be skipped: {label}", file=stderr)
    created = gh.api(
        f"/repos/{repository.full_name}/issues",
        method="POST",
        payload={"title": options.title, "body": body, "labels": selected_labels},
    )
    if not isinstance(created, dict) or not isinstance(created.get("html_url"), str):
        raise ToolError("Issue creation response is incomplete")
    print(created["html_url"], file=stdout)
    return 0


def non_negative_integer(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one Issue for open npm Dependabot alerts after the patched version's "
            "minimumReleaseAge. Order: CLI override, top-level pnpm-workspace.yaml, then "
            "pnpm 11 default 1440 minutes. Dry-run is the default; mutation requires --apply."
        )
    )
    parser.add_argument("repository_url", help="exact URL: https://github.com/OWNER/REPOSITORY")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show the decision and proposed Issue; default")
    mode.add_argument("--apply", action="store_true", help="create one Issue after a final duplicate check")
    parser.add_argument(
        "--minimum-release-age-minutes",
        type=non_negative_integer,
        help="non-negative cooldown override in minutes",
    )
    parser.add_argument(
        "--labels",
        default=",".join(DEFAULT_LABELS),
        help="comma-separated labels (default: security,dependencies)",
    )
    parser.add_argument("--title", default=DEFAULT_TITLE, help=f"Issue title (default: {DEFAULT_TITLE})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    labels = tuple(dict.fromkeys(label.strip() for label in arguments.labels.split(",") if label.strip()))
    if not labels:
        parser.error("--labels must contain at least one non-empty label")
    options = Options(
        repository_url=arguments.repository_url,
        apply=arguments.apply,
        minimum_release_age_minutes=arguments.minimum_release_age_minutes,
        labels=labels,
        title=arguments.title,
    )
    try:
        return execute(
            options,
            gh=GhClient(),
            npm=NpmClient(),
            now=datetime.now(timezone.utc).replace(microsecond=0),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except ToolError as error:
        print(f"error: {redact(str(error))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
