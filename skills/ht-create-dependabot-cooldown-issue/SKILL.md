---
name: ht-create-dependabot-cooldown-issue
description: Create one Issue from an npm repository URL for untracked Dependabot fixes whose npm patched-version release has passed minimumReleaseAge. Use for safe dry-runs, explicit Issue creation, or per-repository Hermes cron setup.
---

# Dependabot Cooldown Issue

Use the bundled deterministic CLI for npm/pnpm repositories. It groups open npm Dependabot
security alerts into one Issue only after each first patched version has been published on npm for
the selected cooldown. The alert creation date is not the release-safety signal.

## Preconditions

- Require one exact `https://github.com/OWNER/REPOSITORY` URL. Never infer it.
- Require `python3` and authenticated `gh`.
- The GitHub credential needs repository metadata/content, labels and existing Issues read,
  Dependabot alerts read, and—only for `--apply`—Issues write permission.
- Read [cron.md](references/cron.md) before creating a schedule. Do not create cron jobs merely
  because this skill was invoked.

## Run dry-run first

Resolve `SKILL_DIR` to this skill directory:

```bash
python3 "$SKILL_DIR/scripts/create_dependabot_cooldown_issue.py" \
  "https://github.com/OWNER/REPOSITORY" \
  --dry-run
```

Dry-run is also the default when neither mode flag is supplied. Review the normalized repository,
eligible and excluded alerts, cooldown value/source, proposed title/body, full links, and labels.
It performs no GitHub mutation.

The cooldown is selected in this strict order:

1. `--minimum-release-age-minutes`;
2. top-level plain non-negative integer `minimumReleaseAge` in `pnpm-workspace.yaml` on the
   repository's resolved default branch;
3. `1440` minutes, the pnpm 11 default.

The boundary is evaluated in UTC as:

```text
now >= npm patched_version_published_at + minimumReleaseAge minutes
```

A missing workspace file or absent setting uses 1440. Invalid configuration, permissions,
timeouts, malformed JSON, missing npm timestamps, and other API failures fail closed instead of
becoming an empty alert set or a fallback.

## Apply

Only after reviewing dry-run:

```bash
python3 "$SKILL_DIR/scripts/create_dependabot_cooldown_issue.py" \
  "https://github.com/OWNER/REPOSITORY" \
  --apply
```

Optional arguments:

```text
--minimum-release-age-minutes 4320
--labels security,dependencies
--title "Fix Dependabot security alerts past the release cooldown"
```

Before applying, confirm repository identity did not redirect or rename, required alert/Issue
permissions are available, the cooldown source is intended, every alert has complete npm data,
and the generated Issue is correct. `--apply` may create one Issue and performs no other mutation.
Unknown labels are warned and omitted; they are never created.

## Idempotency and output

Existing open and closed Issues are paginated and scanned for the version, repository, and numeric
alert markers. Pull requests are ignored. A second scan immediately before creation narrows the
concurrency window. GitHub Issues has no cross-process atomic compare-and-create primitive, so
simultaneous runs can still race; schedule one job per repository without overlap.

- Apply with no new alerts: exit 0 and empty stdout.
- Successful apply: exit 0 and only the new full Issue URL on stdout.
- Dry-run: decision details and proposed content on stdout.
- Failure: nonzero with redacted diagnostic on stderr; no partial Issue.

## Common pitfalls

- Do not use alert `created_at` as cooldown start.
- Do not assume `main` or `master`; metadata selects the default branch.
- A workspace 404 differs from 403, 5xx, timeout, or malformed data.
- Only a top-level plain integer YAML scalar is supported. Quoted values, booleans, expressions,
  negatives, decimals, and duplicate keys fail closed without adding a YAML dependency.
- Do not use Issue title search as deduplication truth.
- Never pass `--apply` from tests or exploratory live validation.

## Verification checklist

1. Run dry-run and inspect cooldown source, exclusions, links, markers, labels, and proposed body.
2. Confirm the repository identity equals the input URL.
3. Confirm patched publish times and exact-boundary decisions are plausible.
4. Confirm existing open/closed tracking Issues were excluded.
5. Run `--apply` only with explicit mutation intent.
6. For maintenance, run:

   ```bash
   python3 -m unittest discover \
     -s "$SKILL_DIR/tests" -p 'test_*.py'
   python3 -m py_compile "$SKILL_DIR/scripts/create_dependabot_cooldown_issue.py"
   ```
