# Hermes Cron

Hermes cron runs in fresh sessions and supports skill-backed and no-agent jobs. See the official
documentation: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron

Use one repository per job and stagger multiple repositories by several minutes:

```text
omh-create-dependabot-cooldown-issue:OWNER/REPOSITORY       0 9 * * *
omh-create-dependabot-cooldown-issue:EXAMPLE-OWNER/EXAMPLE-REPOSITORY  7 9 * * *
```

The examples below are templates only. Do not register them automatically.

## Agent-backed job

Attach `omh-create-dependabot-cooldown-issue`, pin the provider/model, enable only terminal, and
use a self-contained prompt with the exact full repository URL:

```text
cronjob(
  action="create",
  schedule="0 9 * * *",
  name="omh-create-dependabot-cooldown-issue:OWNER/REPOSITORY",
  skill="omh-create-dependabot-cooldown-issue",
  provider="PINNED_PROVIDER",
  model="PINNED_MODEL",
  enabled_toolsets=["terminal"],
  prompt="Run the bundled script with --apply for https://github.com/OWNER/REPOSITORY. Return its stdout only; do not infer another repository.",
)
```

- Each run is a fresh session; never depend on the conversation that registered it.
- Keep the repository URL in the prompt and never ask the agent to infer it.
- Enable only the toolset needed to invoke the script and authenticated `gh`.
- Remote API operation needs no checkout or `workdir`. Set an absolute `--workdir` only when a
  repository checkout is intentionally required.
- Pin provider/model so unattended behavior does not follow an unrelated global model change.

## No-agent job

No-agent mode delivers script stdout verbatim: empty stdout is silent, while a nonzero exit
delivers an error. Hermes requires no-agent scripts in its scripts directory. Create a small
repository-specific wrapper there and point it to the actual installed skill path:

```bash
#!/usr/bin/env bash
set -euo pipefail

python3 "/ABSOLUTE/INSTALLED/SKILL/PATH/scripts/create_dependabot_cooldown_issue.py" \
  "https://github.com/OWNER/REPOSITORY" \
  --apply
```

Then register that wrapper explicitly:

```bash
hermes cron create "0 9 * * *" \
  --no-agent \
  --script "omh-create-dependabot-cooldown-issue-owner-repository.sh" \
  --name "omh-create-dependabot-cooldown-issue:OWNER/REPOSITORY"
```

- Put no credentials in the wrapper; rely on the gateway user's authenticated `gh`.
- Hardcode the validated full URL per wrapper. Do not accept an unvalidated repository URL from
  environment variables.
- Share the Python implementation; never duplicate its logic into wrappers.
- Include `--apply` deliberately. Without it the default is dry-run and stdout is not silent.
- Use the real absolute installation path, not the placeholder.

## Multiple repositories and concurrency

Reuse the same skill and Python script, but create a separate named job and fixed wrapper/prompt
for each repository. Stagger schedules, for example `0 9 * * *`, `7 9 * * *`, and `14 9 * * *`,
to reduce GitHub/npm load and avoid concurrent duplicate checks.

The script rechecks markers immediately before Issue creation, but GitHub Issues does not offer an
atomic “create only if no marker exists” operation. Do not intentionally overlap jobs for the same
repository. If two processes race after both final reads, duplicate Issues remain possible.
