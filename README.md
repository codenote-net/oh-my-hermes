# oh-my-hermes

Personal customizations for [Hermes Agent](https://github.com/NousResearch/hermes-agent): skills, plugins, hooks, and config templates — all without modifying the agent core.

## Naming convention

Everything maintained in this repository uses the `omh` prefix so customizations are easy to identify and do not collide with other extensions. See [AGENTS.md](AGENTS.md) for the identifier-specific rules and examples.

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
hermes skills tap add codenote-net/oh-my-hermes
```

Alternatively, add this repo's `skills/` directory to `skills.external_dirs` in your `~/.hermes/config.yaml`:

```yaml
skills:
  external_dirs:
    - /path/to/oh-my-hermes/skills
```

### Plugins

```sh
hermes plugins install codenote-net/oh-my-hermes
```

Then enable them explicitly under `plugins.enabled` in `config.yaml`.

### Config

Run the repository installer to symlink each skill and plugin directory, and to create a config from the template when one does not already exist:

```sh
./scripts/install.sh
```

Set `HERMES_DIR` to install into a location other than `~/.hermes`. You can also copy `config/config.example.yaml` to `~/.hermes/config.yaml` manually and fill in your own values.

Never commit secrets to this repository.

## License

[MIT](LICENSE)

## Review smoke test

This line exists to provide a small, harmless change for validating automated pull request review workflows.
