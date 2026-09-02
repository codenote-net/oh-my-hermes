# hermes-talaria

Personal customizations for [Hermes Agent](https://github.com/NousResearch/hermes-agent): skills, plugins, hooks, and config templates — all without modifying the agent core.

The repository is named after the Talaria, the winged sandals of Hermes in Greek mythology—a fitting metaphor for extensions that empower Hermes Agent.

## Naming convention

Everything maintained in this repository uses the `ht` prefix so customizations are easy to identify and do not collide with other extensions. See [AGENTS.md](AGENTS.md) for the identifier-specific rules and examples.

This prefix replaces the former `omh` namespace. The migration is intentionally breaking: existing skill invocations, scheduled tasks, and configuration references must be updated from `omh-`/`omh_` to `ht-`/`ht_`.

## Layout

```
skills/    # SKILL.md-based knowledge/skills (tap-compatible)
plugins/   # Python plugins (custom tools, hooks, slash commands)
hooks/     # Gateway event hooks (HOOK.yaml + handler.py)
bundles/   # Skill bundles (YAML) exposed as slash commands
config/    # config.yaml template and SOUL.md persona (no secrets)
scripts/   # Setup helpers
```

## Install

### Skills (as a tap)

```sh
hermes skills tap add codenote-net/hermes-talaria
```

Alternatively, add this repo's `skills/` directory to `skills.external_dirs` in your `~/.hermes/config.yaml`:

```yaml
skills:
  external_dirs:
    - /path/to/hermes-talaria/skills
```

### Plugins

```sh
hermes plugins install codenote-net/hermes-talaria
```

Then enable them explicitly under `plugins.enabled` in `config.yaml`.

### Config

Run the repository installer to symlink each skill and plugin directory, and to create a config from the template when one does not already exist:

```sh
./scripts/install.sh
```

If Hermes is already running, refresh its in-process skill cache after installation:

```text
/reload-skills
```

Invoke an installed skill with its generated direct slash command, for example
`/ht-plan-epic-issue`. `/skills` manages the Skills Hub, so `/skills
ht-plan-epic-issue` is parsed as an unknown Hub action rather than a skill invocation.

Set `HERMES_DIR` to install into a location other than `~/.hermes`. You can also copy `config/config.example.yaml` to `~/.hermes/config.yaml` manually and fill in your own values.

Never commit secrets to this repository.

## License

[MIT](LICENSE)
