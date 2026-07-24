---
name: omh-issue-loop
description: Drive one GitHub issue from its issue URL through autonomous implementation, validation, iterative Codex and Claude Code reviews, high-priority finding fixes, draft PR verification, and a ready-for-review pull request without merging. Use when given a GitHub issue URL and asked to implement it fully, loop until review gates are clean, or produce a human-reviewable PR.
---

# Issue Implementation Loop

Orchestrate the workflow only. Delegate working-tree implementation and fixes to Codex CLI and
all independent review and behavior verification to Claude Code CLI. Keep every Git history,
remote, pull-request, issue, CI, and review operation under exclusive orchestrator control. Never
implement, fix, review, merge, close the issue, or widen scope in the orchestrating agent.

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

## Responsibility boundary

Treat Codex implementation and fix processes as working-tree workers, not autonomous issue
owners. They may read the issue and repository instructions, edit only issue-scoped files, run
relevant local validation, and report their work. They must leave all changes uncommitted.

Only the orchestrator may run reviews, create commits, push branches, create or edit pull
requests, mark a PR ready, inspect PR checks, update the issue or PR, or perform final reporting.
Apply this boundary to the initial implementation and every later fix invocation. Never rely on
the worker to infer the boundary from the surrounding workflow.

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

### Safe reruns after an abandoned attempt

When an earlier attempt left a branch or PR for the same issue, do not silently reuse, reset, or
overwrite it. Inspect the PR state, head branch, local branch, remote branch, and worktree first.
Only close the stale PR and delete its local or remote branch when the user explicitly requests
that cleanup. After cleanup, verify that the old PR is closed, the old branch is absent locally
and remotely, the worktree is clean, and the new branch starts at the current default-branch
remote tip. Preserve the old PR number and head SHA in the orchestration record so the rerun is
not mistaken for a successful continuation.

## Local implementation loop

1. Before each Codex implementation or fix process, capture a side-effect baseline:
   - current HEAD SHA and `git log --oneline <base-sha>..HEAD`;
   - current branch name and its local reflog, so commit-then-reset activity is detectable;
   - `git status --short --branch` and the tracked, staged, and untracked file set;
   - the remote branch OID from `git ls-remote --heads origin <branch-name>` (including absence);
   - all PRs for the branch from
     `gh pr list --repo <owner>/<repo> --head <branch-name> --state all`, plus enough `gh pr view`
     metadata to detect edits, readiness, state, body, title, and head changes;
   - issue state and mutable metadata from `gh issue view`.
2. Invoke Codex with this complete prompt, substituting the actual issue URL:

   ```text
   /goal <issue-url>

   You are the implementation worker for this issue. Implement the issue and run the relevant
   local validation only.

   Strict restrictions:
   - Do not create, amend, or reset any commit.
   - Do not run `git commit`, `git push`, or `git push --force`.
   - Do not create, edit, mark ready, or merge any pull request.
   - Do not close or modify the GitHub issue.
   - Do not monitor GitHub Actions, PR checks, or external review tools.
   - Do not perform Codex, Claude Code, security, or any other review.
   - Do not make changes outside the issue scope.
   - Do not modify generated files unless they are explicitly required by the issue or repository
     workflow.

   Before making changes, read the repository instructions and the issue carefully. Work only in
   the current branch. Leave the implementation changes uncommitted in the working tree so that
   the orchestrator can review them before committing and pushing.

   At the end, report only:
   1. The files changed.
   2. The exact validation commands executed.
   3. The exit status and result of each validation command.
   4. Any blockers or unresolved concerns.

   The orchestrator will handle all reviews, commits, pushes, pull-request operations, and final
   reporting.
   ```

3. After the process exits, perform a mandatory fail-closed side-effect check before any review:
   - require exit status zero and all four requested report sections;
   - require HEAD and the baseline commit range to be unchanged;
   - compare the branch reflog with the baseline and reject evidence of commit, amend, or reset;
   - compare the remote branch OID with the baseline and reject creation or movement of the
     remote branch;
   - compare the complete branch PR snapshot and issue snapshot with their baselines and reject
     any creation, edit, readiness, state, or content change;
   - inspect captured worker output for prohibited Git/GitHub, CI-monitoring, or review commands,
     even when their observable remote state did not change;
   - inspect status and diffs, including staged and untracked files, and require every change to
     be necessary for the issue or an explicitly required repository-generated artifact.
4. If any check fails or cannot be completed, stop immediately and report an orchestration
   failure with the before/after evidence. Do not undo or conceal the worker action. Do not start
   local reviews, create or update a PR, invoke another worker, or perform further PR operations.
5. If every check passes, preserve the worker report and continue. The orchestrator must not
   repair the implementation itself.
6. Capture these three independent review artifacts:
   - Codex `/review` against the branch diff from the recorded base.
   - Claude Code `/code-review` against the same diff.
   - Claude Code `/security-review` against the same diff.
7. Run long jobs in the background when supported and poll them to terminal completion. Preserve
   stdout, stderr, exit status, and target SHA for each artifact. A review that prints a
   complete-looking report but times out, hangs, or has no recorded exit status is incomplete.
   Retry with a narrower read-only prompt when appropriate, but never count a partial artifact as
   clean. Run independent read-only reviews in parallel only when they cannot mutate the same
   worktree. A failed or incomplete review is not a clean result.
8. Normalize priorities. Treat `critical`, `high`, `P0`, and `P1` (and explicit equivalents such
   as blocker or severe exploitable vulnerability) as high priority. Do not promote ambiguous
   findings merely to force convergence; retain the reviewer's evidence and stated severity.
9. If zero high-priority findings remain and every review completed, leave the local loop.
10. Otherwise, if five fix invocations have already completed, stop before a sixth, summarize
   repeated and unresolved findings, preserve the branch, and ask the user to decide. Never open
   or ready a PR while this safety valve is active. If fewer than five fixes have run, increment
   the shared fix count and continue.
11. Invoke Codex with the complete high-priority findings and the same strict restrictions and
    report contract used by the implementation prompt, followed by this task instruction:

   ```text
   Fix the high-priority findings from the Codex review, Claude Code review, and Claude Code
   security review. Keep scope limited to the issue and the findings. Rerun only affected
   validation, plus any repository-required final checks. Report files and exact results.
   ```

12. Apply the mandatory post-execution side-effect check to the fix worker. Only after it passes,
    return to the three-review gate. Do not unnecessarily rerun already successful validations at
    the same unchanged SHA.

## Draft PR and PR loop

1. Run the complete repository-required validation gate once at the clean candidate SHA. Require
   success, or document a genuine environment blocker and stop for user direction.
2. Before committing, verify that the repository's configured commit-signing path is usable. If
   signing fails, keep staged changes intact and stop for the user to restore the configured
   signer; never silently disable signing or alter global Git configuration. Read
   [commit-signing-preflight.md](references/commit-signing-preflight.md) for the required check.
3. Commit only issue-scoped files, then publish with the repository-required procedure, such as a
   repository-provided signoff command when its instructions require one. Do not replace that
   procedure with a plain `git push`. Never use force push or rewrite history. Open a draft PR
   only after required publication or signoff succeeds. Record every commit in `<base>..HEAD`.
4. Run all five review checks for the exact current PR head SHA. Draft-PR CI success, signoff
   success, or a skipped automated reviewer is not a substitute for a missing review artifact:
   - Codex `/review` locally.
   - Claude Code `/code-review` locally.
   - Claude Code `/security-review` locally.
   - Claude Code `/review #<pr-number>` against the PR.
   - Claude Code behavior verification in a fresh worktree.
5. For fresh-worktree verification, create a separate temporary worktree at the exact remote PR
   head, read the issue and repository instructions, run or exercise the real behavior and the
   relevant discovered validation commands, and return prioritized evidence. Do not edit the
   primary worktree. Remove only the temporary worktree after its process has finished; retain its
   report. Treat setup, checkout, or validation failure as an incomplete gate, not zero findings.
6. Aggregate high-priority counts separately for all five sources. If every source completed with
   zero high-priority findings at the same SHA, exit the PR loop.
7. Otherwise apply the same shared five-fix safety valve. Ask Codex, under the complete worker
   restrictions and mandatory side-effect check, to fix only the current high-priority findings
   and rerun affected validation. Then repeat the three local reviews until clean. The
   orchestrator commits and pushes normally, reruns PR review and fresh-worktree verification at
   the new head, and repeats. Never amend, rebase, force push, or hide earlier review artifacts.

## Complete the PR

Before declaring completion, require this hard checklist:

- Record all five review artifacts for the exact PR head SHA, including exit status.
- Include review results and counts, validation results, unresolved findings, and the exact
  closing-keyword line in the PR body.
- Re-read final PR metadata and require its head SHA to match the reviewed SHA.
- Inspect every required CI and signoff check, while never treating CI success as a replacement
  for a review gate.
- Require `gh pr ready` to succeed and verify `isDraft: false`, unless a stop condition requires
  preserving the draft.
- Generate the final report only after every item passes. If the PR or issue changed externally,
  re-read it and report the discrepancy instead of relying on an earlier snapshot.

1. Update the PR body with:
   - implementation summary;
   - exact validation commands and results;
   - all five review sources, target SHA, result, and high-priority count;
   - unresolved medium/low findings and why each remains open, or `None`;
   - `Closes #<issue-number>` as its own top-level line.
2. Keep the closing keyword outside code blocks, quotes, lists, headings, and sentences. A bare
   issue URL is insufficient. Read the body back with `gh pr view` and perform a simple exact-line
   check for `Closes #<issue-number>`.
3. For dependency-alert issues, distinguish the repository default-branch alert count from the
   proposed branch's manifest and lockfile validation. Do not claim that open Dependabot alerts
   are resolved while the PR is unmerged. Record the baseline count and state that GitHub
   recalculates alert state after merge unless a supported branch-specific API or check provides
   direct evidence.
4. Reconfirm that the PR head SHA matches the fully reviewed SHA, then mark the draft ready with
   `gh pr ready`. Never merge the PR or close the issue.
5. Ask a human to perform the final review and merge decision.
6. Return a concise report containing the issue URL, branch, PR URL, commits created by Codex,
   high-priority finding count for each of the five sources, validation results, and every
   unresolved finding.

## Stop conditions

Stop safely and request user direction when authentication is unavailable, repository identity
does not match, the worktree is dirty, required validation cannot run, a child review is
incomplete, changes escape issue scope, a branch already exists, or five fix iterations do not
converge. Preserve evidence and never treat a tooling failure as approval.
