#!/usr/bin/env bash
# Symlink skills and plugins into ~/.hermes/ so `git pull` updates take effect immediately.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_DIR="${HERMES_DIR:-$HOME/.hermes}"

mkdir -p "$HERMES_DIR/skills" "$HERMES_DIR/plugins"

link_children() {
  local source_parent="$1"
  local target_parent="$2"
  local source target

  for source in "$source_parent"/*/; do
    [ -d "$source" ] || continue
    source="${source%/}"
    target="$target_parent/$(basename "$source")"

    if [ -e "$target" ] && [ ! -L "$target" ]; then
      printf 'Refusing to replace existing path: %s\n' "$target" >&2
      return 1
    fi

    ln -sfn "$source" "$target"
  done
}

link_children "$REPO_DIR/skills" "$HERMES_DIR/skills"
link_children "$REPO_DIR/plugins" "$HERMES_DIR/plugins"

if [ ! -f "$HERMES_DIR/config.yaml" ]; then
  cp "$REPO_DIR/config/config.example.yaml" "$HERMES_DIR/config.yaml"
  echo "Copied config.example.yaml to $HERMES_DIR/config.yaml"
fi

echo "Done. Remember to enable plugins under plugins.enabled in config.yaml."
