# Repository context

This repository contains personal skills, plugins, hooks, bundles, and configuration templates for Hermes Agent.

## Naming convention

- When creating a skill, prefix its directory name with `omh-` and set the same value in the `SKILL.md` frontmatter `name:` field (for example, `skills/omh-deploy-runbook/`, invoked as `/omh-deploy-runbook`).
- When creating a plugin, use `omh` as its manifest name, or `omh-*` when multiple plugins are needed. Set custom tools to `toolset: "omh"` and prefix tool names with `omh_` (for example, `omh_fetch_api`).
- Prefix slash commands, CLI subcommands, and scheduled task keys with `omh_` or `omh-`, according to the identifier style they use.
- Prefix bundle and hook directory or file names with `omh-`.

Hermes skills have no automatic namespace: their directory names are global identifiers. The `omh` prefix makes every customization recognizable and avoids collisions with bundled or tap-provided skills and other extensions.
