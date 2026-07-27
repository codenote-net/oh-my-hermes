---
name: omh-issue-loop
description: Drive one GitHub issue from its issue URL through autonomous implementation, validation, iterative Codex and Claude Code reviews, high-priority finding fixes, draft PR verification, and a ready-for-review pull request without merging. Use when given a GitHub issue URL and asked to implement it fully, loop until review gates are clean, or produce a human-reviewable PR.
---

# Issue Implementation Loop

Orchestrate the workflow only. Delegate working-tree implementation and fixes to Codex CLI and
all independent review and behavior verification to Claude Code CLI. Keep every Git history,
remote, pull-request, issue, CI, and review operation under exclusive orchestrator control. Never
implement, fix, review, merge, close the issue, or widen scope in the orchestrating agent.

Before starting, read [workflow-flowchart.md](references/workflow-flowchart.md) to understand the
end-to-end phases, ownership boundaries, loops, stop paths, CI gate, and Human handoff. Use it as
the workflow map; this file remains the authoritative source for detailed requirements.

## Fixed execution configuration

Use these settings verbatim on every applicable child invocation:

- **Codex CLI**: `model="gpt-5.6-sol"`, `model_reasoning_effort="low"`, `service_tier="fast"`
  - Pass `--yolo` before the `exec` or `review` subcommand.
  - Implementation prompt: `/goal`, followed by the parent-provided issue snapshot.
  - Local review prompt: `/review`
  - Fix only high-priority findings without widening scope.
- **Claude Code CLI**: model `Opus 4.7`, reasoning effort `high`
  - Pass `--permission-mode auto --model claude-opus-4-7 --effort high -p`.
  - Local review prompt: `/code-review`
  - Security review prompt: `/security-review`
  - PR review prompt: `/review #<pr-number>`
  - Perform behavior verification from a fresh worktree.

Apply Codex settings per process; never edit global configuration. Use this command shape:

```bash
codex --yolo exec --ephemeral -c model='"gpt-5.6-sol"' \
  -c model_reasoning_effort='"low"' -c service_tier='"fast"' '<PROMPT>'
```

Use `claude --permission-mode auto -p --model claude-opus-4-7 --effort high
--no-session-persistence '<PROMPT>'` for Claude Code. Give every child the parent-captured issue
snapshot and explicitly forbid it from fetching the issue again. Give every reviewer the issue
URL for provenance, current branch or PR target, repository instructions, and a request to label
each finding `critical`, `high`, `medium`, or `low`.

## Single-fetch issue snapshot

The orchestrator is the only process allowed to fetch the issue. Run exactly one
`gh issue view` for the specified issue during preflight and preserve its exact JSON output as an
immutable issue snapshot for the entire run. Do not refresh it, including after implementation,
during side-effect checks, in review loops, or before final reporting.

Include the complete snapshot verbatim in every Codex and Claude Code child prompt inside a
clearly delimited `ISSUE SNAPSHOT` block. Tell each child that the snapshot is authoritative and
that it must not run `gh issue view`, `gh api` for the issue, or otherwise fetch or mutate the
issue. This applies to initial implementation, every fix worker, all local and PR reviews, and
fresh-worktree behavior verification. Passing only the issue URL is insufficient.

Keep the issue URL in prompts only as provenance. Never use URL-based slash-command syntax that
causes a child to fetch the issue implicitly. If the one preflight fetch fails or its JSON is
incomplete, stop; never delegate issue retrieval to a child.

## Responsibility boundary

Treat Codex implementation and fix processes as working-tree workers, not autonomous issue
owners. They may read the parent-provided issue snapshot and repository instructions, edit only
issue-scoped files, run relevant local validation, and report their work. They must leave all
changes uncommitted and must not retrieve live issue data.

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
3. Resolve the current human-review target with `gh api user --jq .login`. Require a non-empty
   GitHub login and record it in orchestration state. This is intentionally the same account whose
   `gh` authentication runs the loop; self-mentioning that account is the explicit handoff from
   the agent to its human operator. Never hardcode a username.
4. Resolve the current repository root and `gh repo view --json nameWithOwner,defaultBranchRef`.
   Compare `nameWithOwner` case-insensitively with the URL owner/repository and stop on mismatch.
5. Fetch the issue exactly once:

   ```bash
   gh issue view <issue-url> \
     --json number,title,body,labels,state,url,author,assignees,milestone,createdAt,updatedAt
   ```

   Require valid JSON containing at least the matching URL and number, a non-empty title, and the
   body and labels fields. Preserve the exact output as the immutable issue snapshot. Derive the
   title, body, acceptance criteria, labels, and number only from this snapshot. Do not run
   `gh issue view` again anywhere in the workflow.
6. Inspect `git status --short --branch` and require no status entries after its branch header;
   equivalently, require `git status --porcelain` to be empty. Never stash, discard, overwrite, or
   absorb pre-existing changes.
7. Discover repository instructions and validation commands from applicable `AGENTS.md`,
   `CLAUDE.md`, `README`, manifests such as `package.json`, build files, and CI configuration.
   Discover whether publication requires `gh signoff`, partial signoff contexts, or a repository
   wrapper by reading the same instructions and checking the default branch with `gh signoff
   check` when the extension is available. Set `signoff_required=true` only when repository
   instructions, a repository wrapper, or required status-check configuration provides direct
   evidence. Otherwise set it to `false`. Installing the extension locally is not evidence that
   the repository requires it. Do not hardcode validation commands or invent signoff contexts.
8. Fetch the dynamically discovered default branch and create a new branch from its latest remote
   tip. Name it `omh/issue-<number>-<short-topic>`, with a short lowercase hyphenated topic. Stop if
   that local or remote branch already exists; never reset or reuse it implicitly.
9. Record the base commit, issue URL, immutable issue snapshot, branch, human-review target,
   validation plan, review outputs, finding counts, and fix iteration count in the orchestration
   state. Set a total maximum of ten fix iterations across the local and PR loops.

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
   - the immutable issue snapshot captured during preflight; do not fetch the issue again for the
     baseline.
2. Invoke Codex with this complete prompt, substituting the actual snapshot and issue URL:

   ```text
   /goal

   You are the implementation worker for this issue. Implement the issue and run the relevant
   local validation only.

   ISSUE URL (provenance only): <issue-url>

   ----- BEGIN ISSUE SNAPSHOT -----
   <exact JSON captured by the orchestrator's single gh issue view>
   ----- END ISSUE SNAPSHOT -----

   The issue snapshot above is authoritative. Do not run `gh issue view`, call `gh api` for this
   issue, or otherwise fetch the issue. Do not infer missing requirements from live GitHub state.

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

   Before making changes, read the repository instructions and the issue snapshot carefully. Work
   only in the current branch. Leave the implementation changes uncommitted in the working tree
   so that the orchestrator can review them before committing and pushing.

   At the end, include all of this information in any clear format:
   - files changed, or explicitly `None`;
   - exact validation commands executed, or explicitly `None`;
   - exit status and result for each executed validation command;
   - blockers or unresolved concerns, or explicitly `None`.

   Do not optimize for a fixed number of headings or sections. One combined section, three
   sections, four sections, or another clear structure is acceptable; information completeness is
   the contract.

   The orchestrator will handle all reviews, commits, pushes, pull-request operations, and final
   reporting.
   ```

3. After the process exits, perform a mandatory fail-closed side-effect and report-content check
   before any review:
   - require exit status zero;
   - require HEAD and the baseline commit range to be unchanged;
   - compare the branch reflog with the baseline and reject evidence of commit, amend, or reset;
   - compare the remote branch OID with the baseline and reject creation or movement of the
     remote branch;
   - compare the complete branch PR snapshot with its baseline and reject any creation, edit,
     readiness, state, or content change;
   - inspect captured worker output for prohibited Git/GitHub, CI-monitoring, or review commands,
     including any attempt to fetch or mutate the issue, even when its observable remote state was
     not re-read;
   - inspect status and diffs, including staged and untracked files, and require every change to
     be necessary for the issue or an explicitly required repository-generated artifact;
   - assess the output semantically for the four required information categories above. Never
     count headings or require numbered sections.
4. If the worker exited nonzero, performed a prohibited side effect, or changed files outside
   scope, stop immediately and report an orchestration failure with before/after evidence. Do not
   undo or conceal the worker action. Do not start local reviews, create or update a PR, invoke
   another implementation worker, or perform further PR operations.
5. If side-effect checks pass but required report information is missing or ambiguous, do not
   stop immediately:
   - derive the changed-file list from `git status` and diffs when possible, but never invent
     validation commands, exit statuses, results, blockers, or concerns;
   - make exactly one report-repair invocation for that worker. Give it the original worker
     output, the missing information categories, and the immutable issue snapshot;
   - instruct the repair process to produce only the missing report information from existing
     evidence. It must not edit files, run validation, mutate Git or GitHub, monitor CI, or perform
     reviews;
   - capture a fresh side-effect baseline before repair and apply the same fail-closed
     post-execution side-effect check afterward;
   - do not count report repair as a fix iteration.
6. Merge the original output and repair output, then reassess semantic completeness without
   requiring any section count. If required information is still missing, the repair process
   fails, or it performs a prohibited side effect, stop and list the exact missing categories or
   before/after evidence.
7. If every safety and information check passes, preserve the combined worker report and continue.
   The orchestrator must not repair the implementation itself.
8. Capture these three independent review artifacts. Include the complete immutable issue
   snapshot and the no-refetch instruction in every review prompt:
   - Codex `/review` against the branch diff from the recorded base.
   - Claude Code `/code-review` against the same diff.
   - Claude Code `/security-review` against the same diff.
9. Run long jobs in the background when supported and poll them to terminal completion. Preserve
   stdout, stderr, exit status, and target SHA for each artifact. A review that prints a
   complete-looking report but times out, hangs, or has no recorded exit status is incomplete.
   Retry with a narrower read-only prompt when appropriate, but never count a partial artifact as
   clean. Run independent read-only reviews in parallel only when they cannot mutate the same
   worktree. A failed or incomplete review is not a clean result.
10. Normalize priorities. Treat `critical`, `high`, `P0`, and `P1` (and explicit equivalents such
   as blocker or severe exploitable vulnerability) as high priority. Do not promote ambiguous
   findings merely to force convergence; retain the reviewer's evidence and stated severity.
11. If zero high-priority findings remain and every review completed, leave the local loop.
12. Otherwise, if ten fix invocations have already completed, stop before an eleventh, summarize
   repeated and unresolved findings, preserve the branch, execute the fix-limit human handoff
   below, and ask the user to decide. Never open or ready a PR while this safety valve is active.
   If fewer than ten fixes have run, increment the shared fix count and continue.
13. Invoke Codex with the complete high-priority findings, complete immutable issue snapshot,
    no-refetch instruction, and the same strict restrictions and report contract used by the
    implementation prompt, followed by this task instruction:

   ```text
   Fix the high-priority findings from the Codex review, Claude Code review, and Claude Code
   security review. Keep scope limited to the issue and the findings. Rerun only affected
   validation, plus any repository-required final checks. Report files and exact results.
   ```

14. Apply the mandatory post-execution side-effect check, semantic report validation, and one-time
    report-repair procedure to the fix worker. Only after all pass, return to the three-review
    gate. Do not unnecessarily rerun already successful validations at the same unchanged SHA.

## Conditional publication and signoff invariant

All repositories use signed commits and normal pushes. Signoff is an optional, repository-driven
publication gate:

- When `signoff_required=false`, do not require or invoke `gh signoff`, do not require the
  extension to be installed, and proceed from verified push directly to PR creation or reviews.
- When `signoff_required=true`, require the discovered command and contexts. If the command is
  unavailable, stop with the installation or repository-wrapper requirement; never silently
  downgrade the mode.

For either mode, apply this sequence to the first published commit and every later review or CI
fix commit:

1. Create the signed commit and verify its Git signature according to
   [commit-signing-preflight.md](references/commit-signing-preflight.md).
2. Push the exact current branch normally. Never force push or rewrite history.
3. Require a clean working tree, an upstream branch, and `HEAD` equal to the pushed upstream SHA.
4. Branch on the recorded mode:
   - If `signoff_required=false`, record `signoff: not required` with the evidence used to make
     that decision, then continue.
   - If `signoff_required=true`, never use `gh signoff -f`. Run the repository-prescribed wrapper
     when one exists; otherwise run `gh signoff` with exactly the contexts discovered during
     preflight. Signoff must target the just-pushed `HEAD`.
5. In required mode, run `gh signoff status` and verify the expected `signoff` or
   `signoff/<context>` commit statuses are successful for that exact SHA. Record the SHA, contexts,
   command, exit status, and verification evidence.
6. In required mode, if signoff or status verification fails, stop before PR creation, review
   reruns, ready handoffs, or CI monitoring. Preserve the commit and branch for recovery; do not
   substitute a prior SHA's signoff or treat other green checks as signoff.

Only in required mode, every new commit invalidates the previous commit's signoff for workflow
purposes. When that mode is active, even if the PR already exists or is ready, push and sign off
the new HEAD before running any of the five reviews for that HEAD.

## Draft PR and PR loop

1. Run the complete repository-required validation gate once at the clean candidate SHA. Require
   success, or document a genuine environment blocker and stop for user direction.
2. Before committing, verify that the repository's configured commit-signing path is usable. If
   signing fails, keep staged changes intact and stop for the user to restore the configured
   signer; never silently disable signing or alter global Git configuration. Read
   [commit-signing-preflight.md](references/commit-signing-preflight.md) for the required check.
3. Commit only issue-scoped files, then execute the complete conditional publication and signoff
   invariant above. Open a draft PR after the pushed HEAD is verified and, only in required mode,
   signoff succeeds. Record every commit in `<base>..HEAD`.
4. Run all five review checks for the exact current PR head SHA. Include the complete immutable
   issue snapshot and no-refetch instruction in every child prompt. Draft-PR CI success, signoff
   success, or a skipped automated reviewer is not a substitute for a missing review artifact:
   - Codex `/review` locally.
   - Claude Code `/code-review` locally.
   - Claude Code `/security-review` locally.
   - Claude Code `/review #<pr-number>` against the PR.
   - Claude Code behavior verification in a fresh worktree.
5. For fresh-worktree verification, create a separate temporary worktree at the exact remote PR
   head, pass the immutable issue snapshot into the verifier prompt, read the repository
   instructions, run or exercise the real behavior and the relevant discovered validation
   commands, and return prioritized evidence. The verifier must not fetch the issue. Do not edit
   the primary worktree. Remove only the temporary worktree after its process has finished; retain
   its report. Treat setup, checkout, or validation failure as an incomplete gate, not zero
   findings.
6. Aggregate high-priority counts separately for all five sources. If every source completed with
   zero high-priority findings at the same SHA, immediately execute the ready handoff below before
   waiting for CI. Review success permits Human review to begin, but is not final completion.
7. Otherwise apply the same shared ten-fix safety valve. Ask Codex, under the complete worker
   restrictions and mandatory side-effect check, to fix only the current high-priority findings
   and rerun affected validation. Then repeat the three local reviews until clean. The
   orchestrator creates a signed commit and pushes it. It reapplies and verifies signoff only when
   `signoff_required=true`, then reruns PR review and fresh-worktree verification. When ten fix
   invocations have completed without convergence, stop before an eleventh and execute the
   fix-limit human handoff below. Never amend, rebase, force push, or hide earlier review
   artifacts.

## Ready handoff and background CI monitor

After all five review sources are clean for the exact PR head SHA, make the PR ready immediately
and hand it to the Human without waiting for external CI. CI remains a required final gate, runs
in parallel with Human review, and is monitored by the orchestrator in the background.

### Make the reviewed PR ready

1. Update the PR body with:
   - implementation summary;
   - exact validation commands and results;
   - publication command and verified signoff contexts for the exact reviewed SHA, when required;
   - all five review sources, target SHA, result, and high-priority count;
   - unresolved medium/low findings and why each remains open, or `None`;
   - a prominent CI status note stating that CI is still being monitored and merge must wait for
     the final green-CI handoff;
   - `Closes #<issue-number>` as its own top-level line.
2. Keep the closing keyword outside code blocks, quotes, lists, headings, and sentences. Read the
   body back with `gh pr view` and require the exact closing line and CI-waiting note.
3. Reconfirm that the PR head SHA matches the SHA that passed all five reviews. If the PR is still
   draft, run `gh pr ready`; if a prior cycle already made it ready, leave it ready. In both cases
   verify `isDraft: false`. Never merge the PR or close the issue.
4. Re-resolve `gh api user --jq .login` and require it to match the recorded human-review target.
   Post and read back exactly one comment for this reviewed SHA, using the marker
   `<!-- omh-issue-loop-human-review-ci-pending:<reviewed-head-sha> -->` to avoid duplicates:

   ```text
   @<login> All five automated reviews passed for <reviewed-head-sha>, and this PR is ready for
   Human review. CI may still be pending and is being monitored in the background. Please begin
   review, but wait for the final CI-green handoff before deciding to merge.

   <!-- omh-issue-loop-human-review-ci-pending:<reviewed-head-sha> -->
   ```

5. Record the ready state and verified preliminary handoff comment URL. This comment begins Human
   review; it does not claim CI success or authorize merge.

### Monitor CI in the background

1. Start monitoring only after the ready state and preliminary handoff are verified. Record the
   current PR head SHA, then inspect all checks with `gh pr checks <pr-url> --json
   name,state,bucket,link,workflow`. Run watch or polling in the background so Human review is not
   blocked, and preserve every terminal status transition.
2. Treat exit code 8 or a `pending` bucket as still running. Continue bounded polling until checks
   change state; do not post repeated pending comments or duplicate mentions.
3. Require every applicable check to have bucket `pass` for green. Do not treat failed,
   cancelled, timed-out, action-required, stale, or missing expected checks as green. Accept a
   skipped check only with evidence that repository configuration makes it non-applicable. If
   workflows or required checks are configured but no check is reported, keep the state pending.
   If the repository genuinely has no CI configured, record that explicitly as the terminal green
   equivalent.
4. Re-read the PR head SHA on every terminal observation. If it differs from the reviewed SHA,
   discard stale CI and review results, run all five reviews for the new head, refresh the PR body
   and ready handoff for that SHA, then start a new background CI monitor.
5. On red, inspect available GitHub Actions logs and distinguish an actionable implementation
   failure from an external infrastructure, permission, quota, or service failure. Never change
   code to hide or bypass a failing check.
6. For actionable red, count the repair against the shared ten-fix limit and invoke Codex as a
   restricted fix worker with the immutable issue snapshot, exact CI evidence, standard report
   contract, and mandatory post-execution side-effect check. After affected validation, the
   orchestrator creates a new signed commit and pushes normally. Reapply and verify signoff only
   when `signoff_required=true`. Run all five reviews after the conditional publication gate
   passes, then refresh the ready handoff and monitor CI again.
7. If red is not repository-actionable or required evidence is unavailable, preserve the ready
   PR, post a non-duplicated status comment describing the blocker, and stop for Human direction.
   Do not claim final completion or post the CI-green marker.
8. If the shared ten-fix limit is exhausted by CI repairs, stop before an eleventh attempt and
   execute the fix-limit human handoff below.
9. On green, require the PR head SHA to equal both the reviewed SHA and green-CI SHA. Update and
   read back the PR body's CI note with the exact terminal check results. Then post and verify one
   final at-mention comment, deduplicated by
   `<!-- omh-issue-loop-human-review-ci-green:<reviewed-head-sha> -->`:

   ```text
   @<login> CI is now green for <reviewed-head-sha>. All five automated reviews and all
   applicable CI checks passed. Please perform the final review and decide whether to merge.

   <!-- omh-issue-loop-human-review-ci-green:<reviewed-head-sha> -->
   ```

10. Record the complete check set and final comment URL. Only this green-CI handoff permits the
    workflow to report final completion and ask the Human for the merge decision.

## Fix-limit human handoff

When the shared ten-fix limit is exhausted, the orchestrator must leave a durable progress
comment before returning control to the user. This is a stop-path handoff, not a successful
completion signal. Do not create a PR solely to hold this comment.

1. Re-resolve `gh api user --jq .login` and require it to match the recorded human-review target.
2. Determine the comment target from orchestration state:
   - If a PR has already been created for this run, comment on that PR with `gh pr comment`,
     whether it is still draft or ready.
   - If no PR has been created, comment on the original issue with `gh issue comment
     <issue-url>`. This comment operation does not replace or refresh the immutable issue
     snapshot, and the orchestrator must not run another `gh issue view`.
3. Build a progress summary containing:
   - phase reached (`local` or `PR`) and `10/10` fixes used;
   - branch, current HEAD SHA, and PR URL when one exists;
   - changed files and the latest validation results;
   - each completed review artifact, its target SHA, status, and high-priority count;
   - repeated and unresolved findings, blockers, and the exact reason convergence failed;
   - the explicit next action: the human must review the progress and decide whether to continue
     manually or start a new run.
4. Append the idempotency marker
   `<!-- omh-issue-loop-fix-limit:<current-head-sha> -->`. Inspect comments on the selected target
   first; if that exact marker exists, reuse the existing comment and do not post a duplicate.
5. Post exactly one comment with this shape, substituting the complete progress summary:

   ```text
   @<login> The omh-issue-loop fix limit was reached after 10/10 fix attempts.
   Automated work has stopped before an eleventh attempt.

   <progress summary>

   Please review this progress and decide whether to continue manually or start a new run.

   <!-- omh-issue-loop-fix-limit:<current-head-sha> -->
   ```

6. Read the selected target's comments back and require one comment to contain the exact
   `@<login>` mention and marker. Record its URL. If posting or verification fails, remain stopped
   and report both the exhausted fix limit and the handoff failure; never resume the loop, create
   or ready a PR, or claim successful completion.
7. Include the selected target type, target URL, mentioned login, and verified comment URL in the
   stop report.

## Complete the PR

Before declaring completion, require this hard checklist:

- Record all five review artifacts for the exact PR head SHA, including exit status.
- Include review results and counts, validation results, unresolved findings, and the exact
  closing-keyword line in the PR body.
- Re-read final PR metadata and require its head SHA to match the reviewed SHA.
- Require verified signoff for the exact final PR head SHA only when `signoff_required=true`;
  otherwise require the recorded evidence that signoff was not required.
- Require `gh pr ready` to succeed and verify `isDraft: false`, unless a stop condition requires
  preserving the draft.
- Require the verified preliminary Human-review comment for the reviewed SHA.
- Require background CI monitoring to reach green for that same SHA.
- Require the verified final CI-green at-mention comment for that SHA.
- Generate the final report only after every item passes. Re-read PR state when necessary, but
  never re-fetch the issue; final issue reporting must use the immutable preflight snapshot.

1. For dependency-alert issues, distinguish the repository default-branch alert count from the
   proposed branch's manifest and lockfile validation. Do not claim that open Dependabot alerts
   are resolved while the PR is unmerged. Record the baseline count and state that GitHub
   recalculates alert state after merge unless a supported branch-specific API or check provides
   direct evidence.
2. Ask the twice-mentioned Human to perform the final review and merge decision.
3. Return a concise report containing the issue URL, branch, PR URL, commits created by Codex,
   `signoff_required` mode and conditional publication result for the final SHA, high-priority
   finding count for each of the five sources, validation results, complete CI results, every
   unresolved finding, the mentioned login, and both verified handoff comment URLs.

## Stop conditions

Stop safely and request user direction when authentication is unavailable, repository identity
does not match, the worktree is dirty, required validation cannot run, a child review is
incomplete, changes escape issue scope, a branch already exists, or ten fix iterations do not
converge. On fix-limit exhaustion, complete the mandatory PR-or-issue human handoff before
returning whenever GitHub access is available. Preserve evidence and never treat a tooling failure
as approval.
