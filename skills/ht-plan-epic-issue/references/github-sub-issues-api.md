# GitHub native sub-issue operations

Use this reference when `ht-plan-epic-issue` creates, attaches, orders, or verifies sub-issues. GitHub
Docs are authoritative when this reference differs:

- https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues
- https://docs.github.com/en/rest/issues/sub-issues

## Current platform constraints

- Adding native sub-issues requires at least triage permission for the repository.
- One parent supports up to 100 direct sub-issues.
- GitHub supports up to eight nested sub-issue levels.
- The REST add operation requires Issues write permission.
- A sub-issue added through REST must belong to the same repository owner as its parent.
- Do not set `replace_parent=true`; this workflow never silently moves an Issue from another parent.

Check the live documentation before relying on these limits in a long-lived or resumed run.

## Inspect ancestry and nesting depth

Endpoint:

```text
GET /repos/{owner}/{repo}/issues/{issue_number}/parent
```

Follow the parent endpoint from the epic to the root. A 200 response supplies the next parent; the
documented no-parent response terminates the walk. Record every REST integer ID and reject a repeated
ID as a cycle or inconsistent remote state. Count the root and descendant levels according to the
current GitHub documentation, then require that adding a direct child remains within the eight-level
limit.

For any existing Issue the user explicitly approves for attachment, perform the same checks before
mutation: it must not be the epic or one of the epic's ancestors, must not have another parent, and
its deepest descendant must still fit when its tree is placed under the epic. Walk each candidate's
`sub_issues` endpoint recursively with pagination, record visited IDs, and stop on a repeated ID or
incomplete page rather than guessing the deepest level. New Issues have no descendants, but an
ambiguous failed creation still requires an existence and parent read-back.

## Prefer native GitHub CLI creation when available

Current GitHub CLI supports creating and attaching in one operation:

```bash
gh issue create \
  --repo OWNER/REPOSITORY \
  --title "$title" \
  --body-file "$body_file" \
  --parent "https://github.com/OWNER/REPOSITORY/issues/EPIC_NUMBER"
```

Preflight the installed CLI with `gh issue create --help` and require the `--parent` flag before
using it. A successful command response is not verification; read back the Issue and parent.

Existing Issues can also be attached with `gh issue edit --add-sub-issue`, but use the REST endpoint
below when exact database IDs, response capture, and uniform verification are preferable.

## REST database ID versus other identifiers

The native add and priority payloads require the REST integer Issue `id`. This is not:

- the visible Issue `number`; or
- the GraphQL node ID returned by some `gh issue view --json id` calls.

Resolve the integer safely:

```bash
gh api "repos/OWNER/REPOSITORY/issues/NUMBER" --jq '.id'
```

Require a positive integer and verify that the same response has the expected `html_url`, `number`,
and no `pull_request` field.

## List every native sub-issue

Endpoint:

```text
GET /repos/{owner}/{repo}/issues/{issue_number}/sub_issues
```

Use GitHub CLI pagination and preserve server order:

```bash
gh api --paginate --slurp \
  "repos/OWNER/REPOSITORY/issues/EPIC_NUMBER/sub_issues?per_page=100"
```

Flatten the returned pages without sorting. Verify page completeness and retain each item's `id`,
`number`, `html_url`, `title`, `body`, `state`, and repository identity. Empty output is distinct
from an access or pagination failure.

## Attach an existing Issue

Endpoint:

```text
POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues
```

Command shape:

```bash
gh api --method POST \
  -H "Accept: application/vnd.github+json" \
  "repos/OWNER/REPOSITORY/issues/EPIC_NUMBER/sub_issues" \
  -F sub_issue_id="$rest_integer_id"
```

Expected success is HTTP 201. Do not pass `replace_parent`. Read the parent before attachment; a
conflicting current parent is a stop condition. After attachment, list the epic's native sub-issues
and require the exact Issue ID to occur once.

## Reprioritize the complete list

Endpoint:

```text
PATCH /repos/{owner}/{repo}/issues/{issue_number}/sub_issues/priority
```

To place one Issue directly after another:

```bash
gh api --method PATCH \
  -H "Accept: application/vnd.github+json" \
  "repos/OWNER/REPOSITORY/issues/EPIC_NUMBER/sub_issues/priority" \
  -F sub_issue_id="$current_rest_id" \
  -F after_id="$previous_rest_id"
```

The endpoint also accepts `before_id`; provide exactly one of `after_id` or `before_id`.

For desired complete order `[A, B, C, ...]`, walk from the second item through the last and move each
item immediately after its predecessor. Because the desired list contains every retained native
sub-issue, this converges the complete list even when the first item initially appears later. A
single-item list needs no priority mutation.

After all priority calls, list every page again and compare the exact returned REST ID sequence with
the desired sequence. Do not infer order from Issue numbers, creation timestamps, or mutation order.

Immediately before the first priority call, re-list all pages and require the ID set to equal the
reviewed initial set plus the exact Issues attached by this run. Re-check that set after each priority
call. On an unexpected addition or removal, stop remaining reorder calls and preserve the completed
call ledger; detecting concurrency only after the full reorder is too late.

## Failure and rate-limit handling

GitHub documents secondary rate limiting for rapid content creation and add-sub-issue mutations.
Run mutations serially. On 403 or 422, capture response headers and body without secrets, honor
`Retry-After` or rate-limit reset information, and determine whether the mutation already took
effect before any retry. Never retry Issue creation blindly.

Maintain a mutation ledger containing:

- planning key;
- intended title/body hash;
- disposition;
- Issue URL, number, and REST integer ID;
- creation verification;
- parent verification;
- intended position; and
- final order verification.

If the run stops, this ledger is the recovery source. Do not compensate automatically by closing
Issues, removing parent links, or reordering unrelated entries.
