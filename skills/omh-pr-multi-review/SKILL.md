---
name: omh-pr-multi-review
description: Review a GitHub pull request with Claude Code CLI `/review`, `/security-review`, and `/code-review` plus Codex CLI review, then aggregate independent results and shared findings into one Markdown report. Use when a user invokes `/omh-pr-multi-review` with a PR URL, requests a multi-model PR review, or wants Claude and Codex review results compared.
---

# PR Multi Review

Run the bundled deterministic script. It validates the GitHub PR URL and CLI authentication,
checks out the PR, executes every reviewer with an independent timeout, restores the original
checkout, and writes one report even when individual reviewers fail.

## Usage

Resolve `SKILL_DIR` to the directory containing this file, then run:

```bash
python3 "$SKILL_DIR/scripts/review_pr.py" "https://github.com/OWNER/REPO/pull/123"
```

The default output is `./pr-review-123.md`. Override it or the per-command timeout:

```bash
python3 "$SKILL_DIR/scripts/review_pr.py" \
  "https://github.com/OWNER/REPO/pull/123" \
  --output "./reviews/pr-123.md" \
  --timeout 2400
```

## Procedure

1. Run the script from a clean checkout of the PR's repository. The script refuses to switch
   branches when tracked or untracked changes exist.
2. Let the script finish all four reviews. Do not replace failed sections manually; their exit,
   timeout, or authentication errors are part of the report.
3. Return the absolute report path and name any failed reviewer sections.

The script invokes these configurations:

- Claude Code: `claude --permission-mode auto -p --model claude-opus-4-8 --effort high`, once
  for each of `/review <PR_URL>`, `/security-review <PR_URL>`, and `/code-review <PR_URL>`.
- Codex: `codex --yolo review --base <remote-base>` with `model="gpt-5.6-sol"`,
  `model_reasoning_effort="high"`, and `service_tier="priority"` (the current CLI's Fast tier).
- Summary: an ephemeral `codex --yolo exec` call with the same Codex model, effort, and tier.

## Preconditions and safety

- Require `gh`, `claude`, `codex`, `git`, and `python3` on `PATH`.
- Require successful `gh auth status`, `claude auth status`, and `codex login status` checks.
- Require the current Git repository's GitHub owner/repository to match the PR URL.
- Never force checkout, discard changes, or modify global CLI configuration.
- Treat each reviewer independently. A timeout or nonzero exit becomes a clearly labeled error
  in that section while later reviewers continue.
- Restore the original branch or detached commit after report generation. If restoration fails,
  print a prominent warning and retain the report.

## Output contract

Write the title, URL, UTC execution timestamp, exact model options, four raw-result sections, and
a final cross-review summary. Preserve reviewer output as returned; do not present the generated
summary as a replacement for the individual evidence.
