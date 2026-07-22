#!/usr/bin/env python3
"""Run Claude Code and Codex PR reviews and aggregate their output."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence


PR_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)/?(?:[?#].*)?$"
)
CLAUDE_MODEL = "claude-opus-4-8"
CODEX_MODEL = "gpt-5.6-sol"


def run(command: Sequence[str], timeout: int, cwd: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(command), cwd=cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
        output = completed.stdout.strip()
        return completed.returncode, output or "(command produced no output)"
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        message = f"ERROR: timed out after {timeout} seconds."
        if partial.strip():
            message += f"\n\nPartial output:\n{partial.strip()}"
        return 124, message
    except OSError as exc:
        return 127, f"ERROR: could not start command: {exc}"


def require_tools() -> None:
    missing = [name for name in ("gh", "claude", "codex", "git", "python3") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"missing required command(s): {', '.join(missing)}")


def require_auth(cwd: Path) -> None:
    checks = (
        ("GitHub CLI", ["gh", "auth", "status"], "Run `gh auth login`."),
        ("Claude Code CLI", ["claude", "auth", "status", "--text"], "Run `claude auth login`."),
        ("Codex CLI", ["codex", "login", "status"], "Run `codex login`."),
    )
    for label, command, remedy in checks:
        code, output = run(command, 30, cwd)
        if code:
            raise RuntimeError(f"{label} is not logged in. {remedy}\n{output}")


def git_output(args: Sequence[str], cwd: Path) -> str:
    code, output = run(["git", *args], 60, cwd)
    if code:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{output}")
    return output


def fenced_result(code: int, output: str) -> str:
    if code == 0:
        return output
    return f"> Reviewer failed (exit code {code}). Other reviews continued.\n\n{output}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr_url", help="GitHub PR URL, for example https://github.com/OWNER/REPO/pull/123")
    parser.add_argument("--output", type=Path, help="Markdown report path (default: ./pr-review-N.md)")
    parser.add_argument("--timeout", type=int, default=1800, help="timeout in seconds per CLI invocation (default: 1800)")
    args = parser.parse_args()

    match = PR_RE.fullmatch(args.pr_url)
    if not match:
        parser.error("PR_URL must match https://github.com/OWNER/REPO/pull/NUMBER")
    if args.timeout < 1:
        parser.error("--timeout must be a positive integer")

    cwd = Path.cwd().resolve()
    output_path = (args.output or Path(f"pr-review-{match['number']}.md")).expanduser()
    if not output_path.is_absolute():
        output_path = cwd / output_path

    try:
        require_tools()
        require_auth(cwd)
        top = Path(git_output(["rev-parse", "--show-toplevel"], cwd)).resolve()
        status = git_output(["status", "--porcelain", "--untracked-files=all"], top)
        if status != "(command produced no output)":
            raise RuntimeError("working tree is not clean; commit, stash, or remove changes before PR checkout")

        repo_code, repo_name = run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], 60, top)
        expected = f"{match['owner']}/{match['repo']}"
        if repo_code or repo_name.lower() != expected.lower():
            raise RuntimeError(f"current repository is `{repo_name}`; run this skill from `{expected}`")

        meta_code, meta_text = run(
            ["gh", "pr", "view", args.pr_url, "--json", "number,title,url,baseRefName"], 60, top
        )
        if meta_code:
            raise RuntimeError(f"could not read PR metadata:\n{meta_text}")
        metadata = json.loads(meta_text)
        original_branch_code, original_branch = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], 30, top)
        original_target = original_branch if original_branch_code == 0 else git_output(["rev-parse", "HEAD"], top)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    results: list[tuple[str, int, str]] = []
    restore_error = ""
    try:
        checkout_code, checkout_text = run(["gh", "pr", "checkout", args.pr_url, "--detach"], 300, top)
        if checkout_code:
            raise RuntimeError(f"PR checkout failed:\n{checkout_text}")

        base = metadata["baseRefName"]
        fetch_code, fetch_text = run(["git", "fetch", "--quiet", "origin", base], 300, top)
        base_ref = f"origin/{base}"
        if fetch_code:
            base_ref = base
            print(f"warning: could not refresh origin/{base}; using local `{base}`: {fetch_text}", file=sys.stderr)

        for slash_command, heading in (
            ("review", "Claude Code `/review` results"),
            ("security-review", "Claude Code `/security-review` results"),
            ("code-review", "Claude Code `/code-review` results"),
        ):
            code, text = run(
                ["claude", "-p", "--model", CLAUDE_MODEL, "--effort", "high",
                 "--no-session-persistence", f"/{slash_command} {args.pr_url}"],
                args.timeout, top,
            )
            results.append((heading, code, text))

        codex_command = [
            "codex", "review", "-c", f'model="{CODEX_MODEL}"',
            "-c", 'model_reasoning_effort="high"', "-c", 'service_tier="priority"',
            "--base", base_ref,
        ]
        code, text = run(codex_command, args.timeout, top)
        results.append(("Codex `/review` results", code, text))
    except RuntimeError as exc:
        checkout_error = f"ERROR: reviews could not start because PR checkout failed:\n{exc}"
        for heading in (
            "Claude Code `/review` results",
            "Claude Code `/security-review` results",
            "Claude Code `/code-review` results",
            "Codex `/review` results",
        ):
            results.append((heading, 1, checkout_error))
    finally:
        restore_code, restore_text = run(["git", "checkout", "--quiet", original_target], 120, top)
        if restore_code:
            restore_error = f"WARNING: failed to restore original checkout `{original_target}`: {restore_text}"

    review_context = "\n\n".join(f"## {heading}\n{fenced_result(code, text)}" for heading, code, text in results)
    summary = "- Cross-review synthesis was unavailable. Read the individual sections above."
    with tempfile.TemporaryDirectory(prefix="omh-pr-review-") as temp_dir:
        context_path = Path(temp_dir) / "review-context.md"
        context_path.write_text(review_context, encoding="utf-8")
        prompt = (
            f"Read {context_path}. Summarize only important concrete issues independently flagged by multiple "
            "reviewers. Merge duplicates and name the agreeing reviewer sections. If there is no credible overlap, "
            "say so. Return concise Markdown bullets only; do not modify files."
        )
        summary_code, summary_text = run(
            ["codex", "exec", "-c", f'model="{CODEX_MODEL}"',
             "-c", 'model_reasoning_effort="high"', "-c", 'service_tier="priority"',
             "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "-C", str(top), prompt],
            args.timeout, top,
        )
        if summary_code == 0:
            summary = summary_text
        else:
            summary += f"\n\n> Summary invocation failed (exit code {summary_code}): {summary_text}"

    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    report = (
        f"# PR Review: {metadata['title']} (#{metadata['number']})\n\n"
        f"- URL: {metadata['url']}\n"
        f"- Executed: {timestamp}\n"
        f"- Claude Code: `{CLAUDE_MODEL}`, effort `high`, non-interactive `-p`\n"
        f"- Codex: `{CODEX_MODEL}`, reasoning effort `high`, service tier `priority` (Fast), base `{metadata['baseRefName']}`\n\n"
        f"{review_context}\n\n## Summary\n\n{summary.strip()}\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    if restore_error:
        print(restore_error, file=sys.stderr)
    failed = [heading for heading, code, _ in results if code]
    print(f"Report written to {output_path}")
    if failed:
        print(f"Failed sections: {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
