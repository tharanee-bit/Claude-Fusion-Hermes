#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-${HOME}/.hermes}"
PLUGIN_DIR="${HERMES_HOME_DIR}/plugins/claude-fusion"
errors=0

ok() { printf '  [ok] %s\n' "$1"; }
fail() { printf '  [fail] %s\n' "$1" >&2; errors=$((errors + 1)); }
version_at_least() {
  local major="$1" minor="$2" patch="$3" required_major="$4" required_minor="$5" required_patch="$6"
  (( major > required_major )) ||
    (( major == required_major && minor > required_minor )) ||
    (( major == required_major && minor == required_minor && patch >= required_patch ))
}

printf 'Claude Fusion Hermes doctor\n'

if command -v hermes >/dev/null 2>&1; then
  hermes_version=""
  hermes_version_line=""
  if hermes_version="$(hermes --version 2>&1)" &&
    hermes_version_line="${hermes_version%%$'\n'*}" &&
    [[ "$hermes_version_line" =~ ^Hermes[[:space:]]Agent[[:space:]]v([0-9]+)\.([0-9]+)\.([0-9]+)([[:space:]].*)?$ ]] &&
    version_at_least "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" 0 19 1; then
    ok "Hermes CLI supported: ${hermes_version_line}"
  else
    fail "Hermes >=0.19.1 required; found ${hermes_version_line:-unknown version}"
  fi
else
  fail "Hermes CLI not found"
fi

if command -v claude >/dev/null 2>&1; then
  if claude_help="$(claude --help 2>&1)" && [[ "$claude_help" == *"--safe-mode"* ]]; then
    ok "Claude Code supports --safe-mode"
  else
    fail "Claude Code does not support --safe-mode"
  fi
  if [[ "$claude_help" == *"--json-schema"* && "$claude_help" == *"--output-format"* ]]; then
    ok "Claude Code supports structured output"
  else
    printf '  [warn] Claude Code lacks structured output flags; text fallback will be used\n'
  fi
else
  fail "Claude Code CLI not found"
fi

if command -v git >/dev/null 2>&1; then
  ok "Git available"
else
  fail "Git not found"
fi

if command -v python3 >/dev/null 2>&1; then
  if python_version="$(python3 --version 2>&1)" &&
    [[ "$python_version" =~ ^Python[[:space:]]([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] &&
    version_at_least "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" 3 9 0; then
    ok "Python supported: ${python_version}"
  else
    fail "Python >=3.9 required; found ${python_version:-unknown version}"
  fi
else
  fail "Python 3 not found"
fi

if [[ -d "$PLUGIN_DIR" || -L "$PLUGIN_DIR" ]]; then
  ok "Plugin installed at ${PLUGIN_DIR}"
  if [[ -f "${PLUGIN_DIR}/plugin.yaml" && -f "${PLUGIN_DIR}/__init__.py" && -f "${PLUGIN_DIR}/hermes-plugin/runtime.py" ]]; then
    ok "Installed plugin files are complete"
  else
    fail "Installed plugin files are incomplete"
  fi
  if command -v python3 >/dev/null 2>&1 && PYTHONDONTWRITEBYTECODE=1 python3 "${ROOT_DIR}/hermes-plugin/doctor.py" "$PLUGIN_DIR" >/dev/null; then
    ok "Installed plugin entrypoint registers all Hermes hooks, middleware, and its skill"
  else
    fail "Installed plugin entrypoint failed registration validation"
  fi
else
  fail "Plugin not installed at ${PLUGIN_DIR}; run ./install-hermes.sh"
fi

if command -v hermes >/dev/null 2>&1; then
  if plugin_list="$(hermes plugins list --json --user --enabled 2>&1)" &&
    printf '%s' "$plugin_list" | python3 "${ROOT_DIR}/hermes-plugin/doctor.py" --plugin-list >/dev/null; then
    ok "Hermes discovers claude-fusion as an enabled user plugin"
  else
    fail "Hermes plugin listing failed or did not report one enabled user claude-fusion plugin"
  fi
fi

if [[ "$errors" -ne 0 ]]; then
  printf '\nHermes port: %s problem(s) found\n' "$errors" >&2
  exit 1
fi

printf '\nHermes port: ready\n'
