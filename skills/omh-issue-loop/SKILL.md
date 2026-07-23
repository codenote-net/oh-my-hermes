---
name: omh-issue-loop
description: Drive one GitHub issue from its issue URL through autonomous implementation, validation, iterative Codex and Claude Code reviews, high-priority finding fixes, draft PR verification, and a ready-for-review pull request without merging. Use when given a GitHub issue URL and asked to implement it fully, loop until review gates are clean, or produce a human-reviewable PR.
---

# Issue Implementation Loop

Orchestrate the workflow only. Delegate all implementation and fixes to Codex CLI and all
independent review and behavior verification to Claude Code CLI. Never implement, fix, review,
merge, close the issue, or widen scope in the orchestrating agent.

## Fixed execution configuration

Use these settings verbatim on every applicable child invocation:

- **Codex CLI**: `model="gpt-5.6-sol"`, `model_reasoning_effort="low"`, `service_tier="fast"`
  - Implementation prompt: `/goal <issue-url>`
  - Local review prompt: `/review`
  - Fix only high-priority findings without widening scope.
- **Claude Code CLI**: model `Opus 4.8`, reasoning effort `high`
  - Pass `--model claude-opus-4-8 --effort high -p`.
  - Local review prompt: `/code-review`
  - Security review prompt: `/security-review`
  - PR review prompt: `/review #<pr-number>`
  - Perform behavior verification from a fresh worktree.

Apply Codex settings per process; never edit global configuration. Use this command shape:

```bash
codex exec --ephemeral -c model='"gpt-5.6-sol"' \
  -c model_reasoning_effort='"low"' -c service_tier='"fast"' '<PROMPT>'
```

Use `claude -p --model claude-opus-4-8 --effort high --no-session-persistence '<PROMPT>'`
for Claude Code. Give every reviewer the issue URL, current branch or PR target, repository
instructions, and a request to label each finding `critical`, `high`, `medium`, or `low`.

## Input and preflight

1. Accept exactly one URL matching `https://github.com/<owner>/<repo>/issues/<number>`. If it is
   absent or invalid, stop and ask for one; do not infer a PR URL or issue number.
2. Require `gh`, `git`, `claude`, and `codex` on `PATH`. Verify authentication with
   `gh auth status`, `claude auth status`, and `codex login status`. Stop with the matching login
   command when a check fails.
3. Resolve the current repository root and `gh repo view --json nameWithOwner,defaultBranchRef`.
   Compare `nameWithOwner` case-insensitively with the URL owner/repository and stop on mismatch.
4. Read the issue with `gh issue view <issue-url>`. Preserve its title, body, acceptance criteria,
   labels, and number as the authoritative scope.
5. Inspect `git status --short --branch` and require no status entries after its branch header;
   equivalently, require `git status --porcelain` to be empty. Never stash, discard, overwrite, or
   absorb pre-existing changes.
6. Discover repository instructions and validation commands from applicable `AGENTS.md`,
   `CLAUDE.md`, `README`, manifests such as `package.json`, build files, and CI configuration.
   Do not hardcode build, test, lint, or formatting commands.
7. Fetch the dynamically discovered default branch and create a new branch from its latest remote
   tip. Name it `omh/issue-<number>-<short-topic>`, with a short lowercase hyphenated topic. Stop if
   that local or remote branch already exists; never reset or reuse it implicitly.
8. Record the base commit, issue URL, branch, validation plan, review outputs, finding counts, and
   fix iteration count in the orchestration state. Set a total maximum of five fix iterations
   across the local and PR loops.

## Local implementation loop

1. Invoke Codex with `/goal <issue-url>`. Require it to read repository instructions, implement
   only the issue, run the discovered validation commands, and return changed files plus exact
   command results. The orchestrator must not repair its work.
2. Confirm the child process completed and the working tree contains only issue-scoped changes.
   Stop on unrelated or destructive changes and report them.
3. Capture these three independent review artifacts:
   - Codex `/review` against the branch diff from the recorded base.
   - Claude Code `/code-review` against the same diff.
   - Claude Code `/security-review` against the same diff.
4. Run long jobs in the background when supported and poll them to terminal completion. Run
   independent read-only reviews in parallel only when they cannot mutate the same worktree.
   Preserve stdout, stderr, exit status, and target SHA for each artifact. A failed or incomplete
   review is not a clean result.
5. Normalize priorities. Treat `critical`, `high`, `P0`, and `P1` (and explicit equivalents such
   as blocker or severe exploitable vulnerability) as high priority. Do not promote ambiguous
   findings merely to force convergence; retain the reviewer's evidence and stated severity.
6. If zero high-priority findings remain and every review completed, leave the local loop.
7. Otherwise, if five fix invocations have already completed, stop before a sixth, summarize
   repeated and unresolved findings, preserve the branch, and ask the user to decide. Never open
   or ready a PR while this safety valve is active. If fewer than five fixes have run, increment
   the shared fix count and continue.
8. Invoke Codex with the complete high-priority findings and this instruction:

   ```text
   Fix the high-priority findings from the Codex review, Claude Code review, and Claude Code
   security review. Keep scope limited to the issue and the findings. Rerun only affected
   validation, plus any repository-required final checks. Report files and exact results.
   ```

9. Return to the three-review gate. Do not unnecessarily rerun already successful validations
   at the same unchanged SHA.

## Draft PR and PR loop

1. Run the complete repository-required validation gate once at the clean candidate SHA. Require
   success, or document a genuine environment blocker and stop for user direction.
2. Commit only issue-scoped files, push normally with upstream tracking, and open a draft PR.
   Never use force push or rewrite history. Record every commit in `<base>..HEAD`.
3. Run all five review checks for the exact current PR head SHA:
   - Codex `/review` locally.
   - Claude Code `/code-review` locally.
   - Claude Code `/security-review` locally.
   - Claude Code `/review #<pr-number>` against the PR.
   - Claude Code behavior verification in a fresh worktree.
4. For fresh-worktree verification, create a separate temporary worktree at the exact remote PR
   head, read the issue and repository instructions, run or exercise the real behavior and the
   relevant discovered validation commands, and return prioritized evidence. Do not edit the
   primary worktree. Remove only the temporary worktree after its process has finished; retain its
   report. Treat setup, checkout, or validation failure as an incomplete gate, not zero findings.
5. Aggregate high-priority counts separately for all five sources. If every source completed with
   zero high-priority findings at the same SHA, exit the PR loop.
6. Otherwise apply the same shared five-fix safety valve. Ask Codex to fix only the current
   high-priority findings, rerun affected validation, then repeat the three local reviews until
   clean. Commit and push normally, rerun PR review and fresh-worktree verification at the new
   head, and repeat. Never amend, rebase, force push, or hide earlier review artifacts.

## Complete the PR

1. Update the PR body with:
   - implementation summary;
   - exact validation commands and results;
   - all five review sources, target SHA, result, and high-priority count;
   - unresolved medium/low findings and why each remains open, or `None`;
   - `Closes #<issue-number>` as its own top-level line.
2. Keep the closing keyword outside code blocks, quotes, lists, headings, and sentences. A bare
   issue URL is insufficient. Read the body back with `gh pr view` and perform a simple exact-line
   check for `Closes #<issue-number>`.
3. Reconfirm that the PR head SHA matches the fully reviewed SHA, then mark the draft ready with
   `gh pr ready`. Never merge the PR or close the issue.
4. Ask a human to perform the final review and merge decision.
5. Return a concise report containing the issue URL, branch, PR URL, commits created by Codex,
   high-priority finding count for each of the five sources, validation results, and every
   unresolved finding.

## Stop conditions

Stop safely and request user direction when authentication is unavailable, repository identity
does not match, the worktree is dirty, required validation cannot run, a child review is
incomplete, changes escape issue scope, a branch already exists, or five fix iterations do not
converge. Preserve evidence and never treat a tooling failure as approval.
