# Repository context

The `hermes-talaria` repository contains personal skills, plugins, hooks, bundles, and configuration templates for Hermes Agent.

## Naming convention

- When creating a skill, prefix its directory name with `ht-` and set the same value in the `SKILL.md` frontmatter `name:` field (for example, `skills/ht-deploy-runbook/`, invoked as `/ht-deploy-runbook`).
- When creating a plugin, use `ht` as its manifest name, or `ht-*` when multiple plugins are needed. Set custom tools to `toolset: "ht"` and prefix tool names with `ht_` (for example, `ht_fetch_api`).
- Prefix slash commands, CLI subcommands, and scheduled task keys with `ht_` or `ht-`, according to the identifier style they use.
- Prefix bundle and hook directory or file names with `ht-`.

Hermes skills have no automatic namespace: their directory names are global identifiers. The `ht` prefix makes every customization recognizable and avoids collisions with bundled or tap-provided skills and other extensions.

The `ht` prefix replaces the former `omh` namespace. This is an intentional breaking migration; do not create or retain new identifiers with the old prefix.
