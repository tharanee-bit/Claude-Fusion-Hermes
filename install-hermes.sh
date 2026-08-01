#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-${HOME}/.hermes}"
PLUGIN_DIR="${HERMES_HOME_DIR}/plugins/claude-fusion"
MODE="copy"
FORCE=0

usage() {
  cat <<'EOF'
Usage: ./install-hermes.sh [--link] [--force]

Install and enable Claude Fusion as a Hermes Agent plugin.

  --link   Symlink this checkout instead of copying it (development mode)
  --force  Replace an existing claude-fusion plugin directory
  -h       Show this help
EOF
}

version_at_least() {
  local major="$1" minor="$2" patch="$3" required_major="$4" required_minor="$5" required_patch="$6"
  (( major > required_major )) ||
    (( major == required_major && minor > required_minor )) ||
    (( major == required_major && minor == required_minor && patch >= required_patch ))
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --link) MODE="link" ;;
    --force|-f) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

for command_name in hermes claude git python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  fi
done

if [[ ! -f "${ROOT_DIR}/plugin.yaml" || ! -f "${ROOT_DIR}/__init__.py" ]]; then
  printf 'Claude Fusion Hermes plugin files are incomplete in %s\n' "$ROOT_DIR" >&2
  exit 1
fi

if ! hermes_version="$(hermes --version 2>&1)"; then
  printf 'Unable to query the Hermes version; Hermes >=0.19.1 is required.\n' >&2
  exit 1
fi
hermes_version_line="${hermes_version%%$'\n'*}"
if [[ ! "$hermes_version_line" =~ ^Hermes[[:space:]]Agent[[:space:]]v([0-9]+)\.([0-9]+)\.([0-9]+)([[:space:]].*)?$ ]] ||
  ! version_at_least "${BASH_REMATCH[1]:-0}" "${BASH_REMATCH[2]:-0}" "${BASH_REMATCH[3]:-0}" 0 19 1; then
  printf 'Hermes >=0.19.1 required; found %s\n' "${hermes_version_line:-unknown version}" >&2
  exit 1
fi

if ! python_version="$(python3 --version 2>&1)"; then
  printf 'Unable to query Python; Python >=3.9 is required.\n' >&2
  exit 1
fi
if [[ ! "$python_version" =~ ^Python[[:space:]]([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] ||
  ! version_at_least "${BASH_REMATCH[1]:-0}" "${BASH_REMATCH[2]:-0}" "${BASH_REMATCH[3]:-0}" 3 9 0; then
  printf 'Python >=3.9 required; found %s\n' "${python_version:-unknown version}" >&2
  exit 1
fi

if ! claude_help="$(claude --help 2>&1)"; then
  printf 'Unable to query Claude Code capabilities; update or repair Claude Code before installing.\n' >&2
  exit 1
fi
if [[ "$claude_help" != *"--safe-mode"* ]]; then
  printf 'Claude Code must support --safe-mode; update Claude Code before installing.\n' >&2
  exit 1
fi

PLUGINS_DIR="${HERMES_HOME_DIR}/plugins"
CONFIG_PATH="${HERMES_HOME_DIR}/config.yaml"
mkdir -p "$PLUGINS_DIR"
# Keep staged and backup manifests outside the plugin discovery tree. Hermes
# recursively discovers plugin.yaml files under plugins/, so an in-tree backup
# can shadow the activated plugin during a forced upgrade. HERMES_HOME is on
# the same filesystem, preserving atomic mv-based activation and rollback.
WORK_DIR="$(mktemp -d "${HERMES_HOME_DIR}/.claude-fusion.install.XXXXXX")"
STAGE_DIR="${WORK_DIR}/staged"
BACKUP_DIR="${WORK_DIR}/previous"
CONFIG_BACKUP="${WORK_DIR}/config.yaml"
CONFIG_EXISTED=0
if [[ -f "$CONFIG_PATH" ]]; then
  cp -p "$CONFIG_PATH" "$CONFIG_BACKUP"
  CONFIG_EXISTED=1
fi
SUCCESS=0
ACTIVATED=0

cleanup() {
  if [[ "$SUCCESS" -ne 1 ]]; then
    if [[ "$ACTIVATED" -eq 1 ]]; then
      rm -rf "$PLUGIN_DIR"
    fi
    if [[ -e "$BACKUP_DIR" || -L "$BACKUP_DIR" ]]; then
      mv "$BACKUP_DIR" "$PLUGIN_DIR"
    fi
    if [[ "$CONFIG_EXISTED" -eq 1 ]]; then
      cp -p "$CONFIG_BACKUP" "$CONFIG_PATH"
    else
      rm -f "$CONFIG_PATH"
    fi
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -e "$PLUGIN_DIR" || -L "$PLUGIN_DIR" ]]; then
  if [[ "$FORCE" -ne 1 ]]; then
    printf 'Plugin already exists at %s (rerun with --force to replace it).\n' "$PLUGIN_DIR" >&2
    exit 1
  fi
fi

if [[ "$MODE" == "link" ]]; then
  ln -s "$ROOT_DIR" "$STAGE_DIR"
else
  mkdir -p "$STAGE_DIR"
  tar -C "$ROOT_DIR" \
    -cf - \
    __init__.py \
    plugin.yaml \
    README.md \
    hermes-plugin/__init__.py \
    hermes-plugin/runtime.py \
    hermes-plugin/doctor.py \
    hermes-plugin/skills/claude-fusion/SKILL.md | tar -C "$STAGE_DIR" -xf -
fi

if [[ ! -f "${STAGE_DIR}/plugin.yaml" || ! -f "${STAGE_DIR}/__init__.py" || ! -f "${STAGE_DIR}/hermes-plugin/runtime.py" ]]; then
  printf 'Staged Claude Fusion plugin is incomplete; existing installation was not changed.\n' >&2
  exit 1
fi
if ! PYTHONDONTWRITEBYTECODE=1 python3 "${ROOT_DIR}/hermes-plugin/doctor.py" "$STAGE_DIR" >/dev/null; then
  printf 'Staged Claude Fusion plugin failed registration validation; existing installation was not changed.\n' >&2
  exit 1
fi

if [[ -e "$PLUGIN_DIR" || -L "$PLUGIN_DIR" ]]; then
  mv "$PLUGIN_DIR" "$BACKUP_DIR"
fi
mv "$STAGE_DIR" "$PLUGIN_DIR"
ACTIVATED=1

if ! hermes plugins enable claude-fusion --no-allow-tool-override; then
  printf 'Hermes could not enable claude-fusion; restoring the previous installation.\n' >&2
  exit 1
fi
if ! plugin_list="$(hermes plugins list --json --user --enabled 2>&1)" ||
  ! printf '%s' "$plugin_list" | PYTHONDONTWRITEBYTECODE=1 python3 "${PLUGIN_DIR}/hermes-plugin/doctor.py" --plugin-list >/dev/null; then
  printf 'Hermes did not discover claude-fusion after enablement; restoring the previous installation.\n' >&2
  exit 1
fi

SUCCESS=1

printf '\nClaude Fusion is installed and enabled for Hermes.\n'
printf 'Plugin: %s\n' "$PLUGIN_DIR"
printf 'Run ./doctor-hermes.sh to verify the integration.\n'
