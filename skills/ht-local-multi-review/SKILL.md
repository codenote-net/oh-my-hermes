---
name: ht-local-multi-review
description: Run three independent read-only Claude Code and Codex reviews against one captured local Git change set, including committed branch changes, staged and unstaged changes, and relevant untracked files. Use when reviewing the current working tree without a pull request or extracting only the local review phase of ht-issue-loop.
---

# Local Multi Review

Review the current working tree without requiring or creating a pull request. This skill only
collects evidence. It never fixes findings or changes the candidate.

## Hard boundaries

- Remain read-only in the source repository and in each reviewer invocation.
- Do not edit, create, delete, format, generate, or restore candidate files in response to a
  finding.
- Do not run a finding-fix loop.
- Do not commit, amend, reset, stash, push, fetch, pull, switch branches, create worktrees, or
  create or update pull requests, issues, comments, checks, or any other GitHub state.
- Do not run Claude Code `/review`. That fourth review belongs to `ht-pr-multi-review`.
- Do not summarize, aggregate, merge, deduplicate, reprioritize, classify, or omit reviewer output.
- Do not create OpenAI/Codex UI metadata such as `agents/openai.yaml`.

Temporary files used only to capture process output are allowed outside the repository. Remove
them after their complete contents and statuses have been collected. Never write a report or
review artifact into the repository unless the user explicitly requests one.

## Reviewers

Run exactly these three independent reviews:

1. Claude Code `/code-review`
2. Claude Code `/security-review`
3. Codex `/review`

Use the configured Claude Code and Codex models unless the user supplied model settings. Do not
edit global CLI configuration. Invoke Claude Code non-interactively with
`--no-session-persistence` and `--permission-mode plan`, and explicitly disallow its file-editing
tools. Invoke Codex with `exec --ephemeral --sandbox read-only`; begin its prompt with `/review`.
Do not use `--yolo`, `--dangerously-skip-permissions`, `danger-full-access`, or a writable sandbox.

## Resolve one shared review target

Complete this phase before starting any reviewer:

1. Require `git`, `claude`, and `codex` on `PATH`. Run `claude auth status` and
   `codex login status`. A failed prerequisite is a failed review preflight, not a clean result.
2. Resolve the repository root without changing directory state outside the review commands. Read
   every applicable `AGENTS.md` and `CLAUDE.md`, including instructions inherited from parent
   directories.
3. Record the initial state verbatim:
   - `git rev-parse HEAD`;
   - `git branch --show-current` (an empty value means detached HEAD);
   - `git status --short --branch --untracked-files=all`.
4. Resolve exactly one comparison base. Use a user-specified base when present. Otherwise resolve
   the remote default branch from the existing local `refs/remotes/<remote>/HEAD` symbolic ref and
   use `git merge-base <remote-default-ref> HEAD`. Do not fetch to refresh it. If no unambiguous
   base is available locally, stop and ask the user for the base rather than guessing.
5. Record the resolved base commit with `git rev-parse <base>` and the target HEAD from step 3.
   These two commits remain fixed for the run.
6. Capture the shared scope before launch:
   - committed branch changes: `git diff --binary <base-commit>...<target-head>`;
   - staged changes: `git diff --binary --cached <target-head>`;
   - unstaged changes: `git diff --binary`;
   - non-ignored untracked paths: `git ls-files --others --exclude-standard -z`;
   - the contents or Git object hash of every included untracked path.

Treat every non-ignored untracked path as relevant by default. Exclude one only when it is clearly
unrelated to the requested review, and record the exclusion before reviewer launch. Never omit an
untracked path merely because it is large or inconvenient. If a required capture is incomplete,
ambiguous, or changes before all reviewers are launched, stop; do not mix targets.

The shared target is the tuple of comparison-base commit, target HEAD, staged diff, unstaged diff,
included untracked paths and contents, and applicable repository instructions. Give the same tuple
to every reviewer. Instruct each reviewer to inspect the repository at that captured scope rather
than choosing its own base or silently narrowing the review.

## Run through Hermes

Use Hermes `terminal` and `process` tools rather than assuming an OpenAI skill runner or Codex UI
metadata exists.

1. Build three prompts from the same captured target. Each prompt must include:
   - repository root, comparison-base commit, branch or detached state, and target HEAD;
   - the complete included untracked-path list and any recorded exclusions;
   - the applicable repository instruction paths;
   - an instruction to include committed, staged, unstaged, and included untracked changes;
   - an instruction to remain read-only and not invoke GitHub or alter Git state;
   - a request for actionable findings with file and line references when available and an
     explicit statement when no findings exist.
2. Start the prompts with `/code-review`, `/security-review`, and `/review`, respectively. Do not
   substitute a generic review prompt for any slash command.
3. Launch all three as separate Hermes `terminal(background=true, notify_on_complete=true)`
   processes from the repository root. Launch them back-to-back only after the target capture is
   complete. Immediately before each launch, recapture and compare HEAD, status, all three diff
   byte streams, and the untracked path/content manifest with the shared target; stop launching
   additional reviewers on the first mismatch. Independent reviewers must not share sessions or
   consume another reviewer's output.
4. Create one secure temporary capture directory outside the repository, with separate stdout and
   stderr files for each process. Redirect each reviewer command to its own files rather than
   relying on a combined terminal stream. Use `process(wait)` or completion notification, then
   `process(log)` and the capture files as needed to retrieve the complete output. Record a numeric
   exit status for each reviewer. A timeout requires terminating that reviewer, waiting until it
   is confirmed stopped, and preserving all output captured before termination.
5. Do not infer success from plausible-looking text. A reviewer succeeds only with confirmed
   terminal completion, numeric exit status zero, and complete output.

Recommended command constraints are:

```text
claude --permission-mode plan -p --no-session-persistence \
  --disallowedTools Edit,Write,NotebookEdit \
  '<PROMPT BEGINNING WITH THE REQUIRED SLASH COMMAND>'

codex exec --ephemeral --sandbox read-only \
  '<PROMPT BEGINNING WITH /review>'
```

If the installed CLI rejects a safety option, report that reviewer as failed. Do not weaken the
read-only boundary to make it run.

## Completion and consistency check

After all reviewer processes are confirmed stopped, capture again:

- `git rev-parse HEAD`;
- `git status --short --branch --untracked-files=all`;
- the committed, staged, and unstaged diff byte streams and included untracked path/content
  manifest used for the shared target.

Compare every value byte-for-byte with the initial records. If any value differs, clearly mark the
repository state as inconsistent in every affected reviewer section. Preserve the reviewer output,
but do not call any review successful for the originally captured target. Do not revert, repair,
stash, or otherwise conceal the change.

## Failure handling and exact output contract

Return exactly three top-level sections in this order. Do not add a preface, conclusion, summary,
cross-review analysis, execution note, or fourth section.

```markdown
# Claude Code `code-review`

<complete review output>

# Claude Code `security-review`

<complete review output>

# Codex `review`

<complete review output>
```

Within each section, reproduce the reviewer output completely and verbatim. When stderr is not
already interleaved with stdout, label and include both streams without rewriting either one.

If a reviewer fails, times out, has incomplete output, lacks a confirmed numeric exit status, or
is invalidated by a repository-state inconsistency, add a clear factual status block in that
reviewer's section containing the known exit status, timeout or incomplete state, and consistency
result. Then include every available stdout and stderr byte verbatim. Never replace missing output
with an orchestrator-authored review, and never describe a failed or incomplete reviewer as clean.

If preflight fails or the shared target changes before every reviewer is launched, still return the
same three sections. Mark each reviewer that did not start as `not started`, give the exact
preflight or target-consistency reason, and include all available command stdout and stderr. Do not
invent an exit status for a process that never existed.