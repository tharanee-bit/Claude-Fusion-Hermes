#!/usr/bin/env bash
# Canonical shared helpers for Claude Fusion hooks. This file is sourced by hook scripts from their
# own directory; the legacy installer copies it alongside them (it is not registered as a hook
# itself, so /hooks trust review does not list it).

clf_init_common() {
  CLF_LOG_PREFIX="$1"
  export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  # Per-user state dir, mode 0700. On a shared /tmp a hostile co-tenant could otherwise pre-create a
  # predictable shared dir and read/delete our markers, so we also refuse a dir we do not own.
  STATE_DIR="${TMPDIR:-/tmp}/claude-fusion-state-$(id -u 2>/dev/null || echo 0)"
  # Claude Fusion tries Claude Opus 5 at extra-high (xhigh) effort, then retries fast failures once
  # with the latest Fable at xhigh. The primary model/effort can be overridden; the fallback stays
  # fixed so a bad override still recovers on a known-good pair.
  CLAUDE_MODEL="${CLAUDE_FUSION_MODEL:-claude-opus-5}" # explicit Opus 5 model ID
  CLAUDE_EFFORT="${CLAUDE_FUSION_EFFORT:-xhigh}"   # low / medium / high / xhigh / max
  CLF_FALLBACK_MODEL="fable"                        # latest Fable by alias
  CLF_FALLBACK_EFFORT="xhigh"
  CONTINUITY="${CLAUDE_FUSION_CONTINUITY:-0}"      # opt-in resumable UserPromptSubmit sessions
  SUBAGENT_REVIEW="${CLAUDE_FUSION_SUBAGENT_REVIEW:-1}"
  SUBAGENT_REVIEW_LIMIT="$(clf_positive_int "${CLAUDE_FUSION_SUBAGENT_REVIEW_LIMIT:-2}" 2)"
  # Adversarial verification of codex-dw worker subagents. Enabled by default, bounded by its own
  # per-run reservation budget so a workflow cannot fan verification out without a ceiling.
  WORKFLOW_VERIFY="${CLAUDE_FUSION_WORKFLOW_VERIFY:-1}"
  WORKFLOW_VERIFY_LIMIT="$(clf_positive_int "${CLAUDE_FUSION_WORKFLOW_VERIFY_LIMIT:-2}" 2)"
  DEPTH="${CLAUDE_FUSION_DEPTH:-workflow}"         # workflow (deeper pass) | single (one-shot)
  # Ultra Code: ask Claude for xhigh-effort dynamic workflows (multi-agent read-only orchestration)
  # instead of one single-agent pass. The Task/Workflow/ToolSearch tools and the `ultracode:` keyword
  # are Claude Code built-ins, so this is independent of safe mode: a build that does not expose them
  # degrades to a single deep pass rather than failing.
  ULTRACODE="${CLAUDE_FUSION_ULTRACODE:-1}"        # 1 = ultracode + dynamic workflows | 0 = single agent
  # Internal, never read from the environment: a hook sets it to 0 to force one Claude agent even in
  # ultracode mode. Initialized here so an inherited CLF_ALLOW_FANOUT cannot widen or narrow a hook.
  CLF_ALLOW_FANOUT=1
  TOOLSMODE="${CLAUDE_FUSION_TOOLS:-readonly}"     # readonly (explore repo) | none (--tools "", baked-in only)
  SAFE_MODE="${CLAUDE_FUSION_SAFE_MODE:-1}"        # 1 isolates Claude customizations; 0 allows CLAUDE.md/memory/skills/workflows
  CLAUDE_SAFE_ARGS=(--safe-mode)
  case "$SAFE_MODE" in
    0|false|False|FALSE|no|No|NO|off|Off|OFF) CLAUDE_SAFE_ARGS=();;
  esac
  CUSTOM_CLAUDE_CONTEXT=0
  [ "${#CLAUDE_SAFE_ARGS[@]}" -eq 0 ] && CUSTOM_CLAUDE_CONTEXT=1
  # Workflow-depth analyses are slow; give them headroom. This is one shared whole-hook budget for
  # every Claude attempt, not a fresh timeout per retry. Cap overrides below the registered 660s
  # hook timeout so Codex cannot kill the shell before it records a bounded fail-open outcome.
  if [ "$DEPTH" = "workflow" ]; then DEF_TIMEOUT=600; else DEF_TIMEOUT=300; fi
  CLAUDE_TIMEOUT="$(clf_positive_int "${CLAUDE_FUSION_TIMEOUT:-$DEF_TIMEOUT}" "$DEF_TIMEOUT")"
  [ "$CLAUDE_TIMEOUT" -le 630 ] || CLAUDE_TIMEOUT=630
  CLF_HOOK_STARTED="${SECONDS:-0}"
  CLF_MAX_FILE_BYTES="$(clf_positive_int "${CLAUDE_FUSION_MAX_FILE_BYTES:-409600}" 409600)"
  # Paths matched (lowercased, basename and full relative path) against these globs are dropped from
  # every git status and diff before anything is handed to Claude. Extend with CLAUDE_FUSION_EXCLUDE
  # (extra space-separated globs; globs containing spaces are unsupported).
  CLF_SENSITIVE_GLOBS='.env .env.* *.env .envrc
*.pem *.key *.p12 *.pfx *.jks *.keystore *.kdbx *.ppk
id_rsa* id_dsa* id_ecdsa* id_ed25519* *_rsa *_dsa *_ecdsa *_ed25519
credentials* *credentials.json secrets* secret.* *history .netrc _netrc .npmrc .pypirc .git-credentials .htpasswd auth.json
*.sqlite *.sqlite3 *.db
.ssh/* .aws/* .gnupg/*'
  [ -n "${CLAUDE_FUSION_EXCLUDE:-}" ] && CLF_SENSITIVE_GLOBS="$CLF_SENSITIVE_GLOBS $CLAUDE_FUSION_EXCLUDE"
}

clf_positive_int() {
  case "$1" in
    ''|*[!0-9]*) printf '%s' "$2";;
    0) printf '%s' "$2";;
    *) printf '%s' "$1";;
  esac
}

clf_enabled() {
  case "$1" in 1|true|True|TRUE|yes|Yes|YES|on|On|ON) return 0;; *) return 1;; esac
}

clf_ultracode_enabled() {
  # Ultra Code only applies to the deeper `workflow` consultation depth; `single` stays one-shot.
  [ "$DEPTH" = "workflow" ] && clf_enabled "$ULTRACODE"
}

clf_ensure_state_dir() {
  mkdir -p -m 700 "$STATE_DIR" 2>/dev/null || return 1
  [ ! -L "$STATE_DIR" ] || return 1
  [ -O "$STATE_DIR" ] || return 1
  chmod 700 "$STATE_DIR" 2>/dev/null || return 1
}

clf_dbg() {
  [ "${CLAUDE_FUSION_DEBUG:-0}" = "1" ] || return 0
  clf_ensure_state_dir || return 0
  printf '%s %s: %s\n' "$$" "$CLF_LOG_PREFIX" "$*" >>"$STATE_DIR/debug.log"
}

clf_peer_fusion_active() {
  # Both Fusion directions set their flag before shelling out to the peer (env inherited through the
  # process tree); honoring either breaks any claude<->codex hook loop when both are installed. This
  # is the hard loop guard: no hook may ever ignore it.
  [ "${CLAUDE_FUSION_ACTIVE:-0}" = "1" ] || [ "${CODEX_FUSION_ACTIVE:-0}" = "1" ]
}

clf_workflow_worker() {
  # codex-dw marks every SDK leaf worker. Prompt analysis and final-diff review stay suppressed here
  # (the workflow coordinator owns those), but SubagentStop may opt in as an adversarial verifier
  # under its own bounded budget.
  [ "${CODEX_DW_ACTIVE:-0}" = "1" ]
}

clf_nested_fusion_active() {
  clf_peer_fusion_active || clf_workflow_worker
}

clf_workflow_budget_key() {
  # Prefer a run-scoped key so one verification budget covers the whole workflow; codex-dw exports a
  # run id when it can. Fall back to the worker's own turn/session key, which still bounds each
  # worker. Printed empty when nothing identifies the caller, and the hook then declines to verify.
  # Reservation names use the `workflow-` namespace (see clf_reserve_dir), so this key never collides
  # with an interactive turn's `subagent-` reservations even for identical identifiers.
  _wb_run="$(clf_sanitize_session_id "${CODEX_DW_RUN_ID:-}")"
  [ -n "$_wb_run" ] && { printf '%s' "$_wb_run"; return 0; }
  _wb_turn="$(clf_turn_key "$1" "$2" 2>/dev/null)" || _wb_turn=""
  printf '%s' "$_wb_turn"
}

clf_reserve_dir() {
  # $1 = namespace (subagent | workflow), $2 = budget key, $3 = agent|slot, $4 = id.
  printf '%s/%s.%s-%s-%s' "$STATE_DIR" "$2" "$1" "$3" "$4"
}

clf_cleanup_stale_workflow_reservations() {
  # Workflow reservations are not owned by any Stop-hook turn, so sweep long-idle empty ones instead
  # of leaking them. Half a day is far longer than any single codex-dw run.
  [ -d "$STATE_DIR" ] || return 0
  find "$STATE_DIR" -maxdepth 1 -type d -name '*.workflow-*' -empty -mmin +720 -delete 2>/dev/null
  return 0
}

clf_setup_claude_runtime() {
  PY="/usr/bin/python3"
  [ -x "$PY" ] || PY="$(command -v python3 2>/dev/null)"
  [ -x "$PY" ] || { clf_dbg "no python3"; return 1; }

  command -v timeout >/dev/null 2>&1 || { clf_dbg "no timeout"; return 1; }

  # Resolve the claude binary, then put its real bin dir on PATH so claude's bundled runtime is reachable.
  CLAUDE_BIN="$(command -v claude 2>/dev/null)"
  if [ ! -x "$CLAUDE_BIN" ]; then
    for _cl_c in "$HOME/.local/bin/claude" "$HOME/bin/claude" "/usr/local/bin/claude" "$HOME/.npm-global/bin/claude"; do
      [ -x "$_cl_c" ] && { CLAUDE_BIN="$_cl_c"; break; }
    done
  fi
  [ -x "$CLAUDE_BIN" ] || { clf_dbg "no claude"; return 1; }

  _cl_dir="$(dirname "$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CLAUDE_BIN" 2>/dev/null || echo "$CLAUDE_BIN")")"
  case ":$PATH:" in *":$_cl_dir:"*) ;; *) export PATH="$_cl_dir:$PATH";; esac
  return 0
}

clf_parse_fields() {
  # $1 = raw stdin JSON, remaining args = field names. Emits one base64 line per field so newlines
  # survive the shell. Codex's hook stdin schema matches Claude Code's (snake_case).
  _pf_input="$1"
  shift
  printf '%s' "$_pf_input" | "$PY" -c '
import sys, json, base64
try: d = json.load(sys.stdin)
except Exception: d = {}
for k in sys.argv[1:]:
    v = d.get(k, "")
    if isinstance(v, bool): v = "true" if v else "false"
    sys.stdout.write(base64.b64encode(str(v or "").encode()).decode() + "\n")
' "$@" 2>/dev/null
}

clf_field() {
  printf '%s' "$1" | sed -n "${2}p" | base64 -d 2>/dev/null
}

clf_sanitize_session_id() {
  # Sanitize before it becomes a filename under STATE_DIR (no path traversal / odd chars).
  case "$1" in *[!A-Za-z0-9._-]*|.|..) printf '';; *) printf '%s' "$1";; esac
}

clf_turn_key() {
  # State is turn-scoped when Codex provides turn_id. A missing turn_id deliberately preserves the
  # pre-0.1 session-only convention so older Codex clients and existing markers keep working.
  _tk_session="$(clf_sanitize_session_id "$1")"
  _tk_turn="$(clf_sanitize_session_id "$2")"
  [ -n "$_tk_session" ] || return 1
  if [ -n "$_tk_turn" ]; then printf '%s.%s' "$_tk_session" "$_tk_turn"; else printf '%s' "$_tk_session"; fi
}

clf_find_complex_marker() {
  # Prefer the exact session+turn baseline, but recognize the legacy session-only marker during
  # upgrades. The selected path is printed for the caller to consume or preserve.
  _fm_session="$1"
  _fm_turn="$2"
  _fm_key="$(clf_turn_key "$_fm_session" "$_fm_turn")" || return 1
  _fm_exact="$STATE_DIR/$_fm_key.complex"
  [ -f "$_fm_exact" ] && { printf '%s' "$_fm_exact"; return 0; }
  if [ -n "$_fm_turn" ]; then
    _fm_legacy="$STATE_DIR/$_fm_session.complex"
    [ -f "$_fm_legacy" ] && { printf '%s' "$_fm_legacy"; return 0; }
  fi
  return 1
}

clf_cleanup_subagent_turn() {
  # Reservation directories are empty and turn-scoped. Remove only matching empty directories after
  # the main Stop hook reaches a definitive outcome; transient failures keep reservations stable.
  [ -n "$1" ] && [ -d "$STATE_DIR" ] || return 0
  find "$STATE_DIR" -maxdepth 1 -type d -name "$1.subagent-*" -empty -delete 2>/dev/null
}

clf_probe_capabilities() {
  # Cached --help probe for every CLI feature whose absence changes the execution path. The cache
  # key is the resolved binary plus mtime:size, so Claude Code upgrades re-probe automatically.
  # Never cache an empty/crashed probe: a transient failure must not disable features permanently.
  _sm_real="$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CLAUDE_BIN" 2>/dev/null || printf '%s' "$CLAUDE_BIN")"
  # Use Python rather than GNU/BSD-specific stat flags. macOS provides BSD stat, where `stat -c`
  # fails and used to collapse every binary version to the same `nostat` cache key.
  _sm_stat="$("$PY" -c '
import os, sys
value = os.stat(sys.argv[1])
print(f"{value.st_mtime_ns}:{value.st_size}")
' "$_sm_real" 2>/dev/null)"
  _sm_key="$_sm_real|${_sm_stat:-nostat}"
  _sm_cache="$STATE_DIR/claude-caps"
  if clf_ensure_state_dir && [ -f "$_sm_cache" ]; then
    IFS=$'\t' read -r _sm_ckey CLF_CAP_SAFE CLF_CAP_STRUCTURED CLF_CAP_RESUME <"$_sm_cache" 2>/dev/null
    if [ "$_sm_ckey" = "$_sm_key" ] && [ -n "$CLF_CAP_STRUCTURED" ]; then
      return 0
    fi
  fi
  _sm_help="$(timeout 10 "$CLAUDE_BIN" --help 2>&1)"
  [ -n "$_sm_help" ] || return 1
  CLF_CAP_SAFE=0
  CLF_CAP_STRUCTURED=0
  CLF_CAP_RESUME=0
  printf '%s' "$_sm_help" | grep -q -- '--safe-mode' && CLF_CAP_SAFE=1
  if printf '%s' "$_sm_help" | grep -q -- '--output-format' && printf '%s' "$_sm_help" | grep -q -- '--json-schema'; then
    CLF_CAP_STRUCTURED=1
  fi
  printf '%s' "$_sm_help" | grep -q -- '--resume' && CLF_CAP_RESUME=1
  clf_ensure_state_dir && printf '%s\t%s\t%s\t%s\n' "$_sm_key" "$CLF_CAP_SAFE" "$CLF_CAP_STRUCTURED" "$CLF_CAP_RESUME" >"$_sm_cache" 2>/dev/null
  return 0
}

clf_safe_mode_supported() {
  clf_probe_capabilities || return 1
  [ "$CLF_CAP_SAFE" = "1" ]
}

clf_structured_output_supported() {
  clf_probe_capabilities || return 1
  [ "$CLF_CAP_STRUCTURED" = "1" ]
}

clf_resume_supported() {
  clf_probe_capabilities || return 1
  [ "$CLF_CAP_RESUME" = "1" ]
}

clf_sensitive_path() {
  _sp_path="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  _sp_base="${_sp_path##*/}"
  # set -f: the glob words must reach the case patterns literally, not expand against the cwd.
  set -f
  for _sp_g in $CLF_SENSITIVE_GLOBS; do
    case "$_sp_base" in $_sp_g) set +f; return 0;; esac
    case "$_sp_path" in $_sp_g|*/$_sp_g) set +f; return 0;; esac
  done
  set +f
  return 1
}

clf_filtered_status() {
  # git status --short with sensitive path names replaced by a placeholder (keeps the count signal).
  git -C "$1" status --short 2>/dev/null |
    while IFS= read -r _fs_line; do
      _fs_rest="${_fs_line:3}"
      _fs_old="$_fs_rest"
      _fs_new="$_fs_rest"
      case "$_fs_rest" in *' -> '*) _fs_old="${_fs_rest%% -> *}"; _fs_new="${_fs_rest##* -> }";; esac
      case "$_fs_old" in \"*\") _fs_old="${_fs_old#\"}"; _fs_old="${_fs_old%\"}";; esac
      case "$_fs_new" in \"*\") _fs_new="${_fs_new#\"}"; _fs_new="${_fs_new%\"}";; esac
      if clf_sensitive_path "$_fs_old" || clf_sensitive_path "$_fs_new"; then
        printf '%s [redacted sensitive path]\n' "${_fs_line:0:2}"
      else
        printf '%s\n' "$_fs_line"
      fi
    done
}

clf_filtered_diff() {
  # $1 = repo dir, $2 = base ref. Emits the diff with sensitive paths and files over the size cap
  # dropped entirely (the secret bytes never enter the payload). Returns 1 when nothing reviewable
  # remains. --name-only paths are toplevel-relative, so resolve sizes and run the final diff there.
  _fd_repo="$1"
  _fd_base="$2"
  _fd_top="$(git -C "$_fd_repo" rev-parse --show-toplevel 2>/dev/null)"
  [ -n "$_fd_top" ] || return 1
  _fd_files=()
  _fd_excluded=0
  while IFS= read -r -d '' _fd_f; do
    if clf_sensitive_path "$_fd_f"; then
      _fd_excluded=$((_fd_excluded + 1))
      continue
    fi
    _fd_sz="$(wc -c <"$_fd_top/$_fd_f" 2>/dev/null | tr -d ' ')"
    if [ "${_fd_sz:-0}" -gt "$CLF_MAX_FILE_BYTES" ]; then
      _fd_excluded=$((_fd_excluded + 1))
      continue
    fi
    _fd_files+=("$_fd_f")
  done < <(git -C "$_fd_repo" diff --name-only -z "$_fd_base" -- 2>/dev/null)
  [ "${#_fd_files[@]}" -gt 0 ] || return 1
  [ "$_fd_excluded" -gt 0 ] && printf '### note: %s changed file(s) excluded (sensitive path or size cap)\n' "$_fd_excluded"
  # :(literal) magic: pathspecs after -- are PATTERNS, so a changed file whose name looks like a
  # glob (e.g. ".en?") would otherwise re-match an excluded sibling (".env") and re-include its
  # content, and a name starting with ':' would be eaten as pathspec magic or abort git entirely.
  _fd_specs=()
  for _fd_f in "${_fd_files[@]}"; do
    _fd_specs+=(":(literal)$_fd_f")
  done
  git -C "$_fd_top" diff "$_fd_base" -- "${_fd_specs[@]}" 2>/dev/null
}

clf_filtered_range_diff() {
  # $1 = repository root, $2/$3 = validated full commit ids. Apply the same path and per-file size
  # policy as the working-tree filter, but read sizes from Git objects because the integration range
  # may live only on an external branch/worktree. A file is excluded if either endpoint blob exceeds
  # the cap; deletions and additions therefore remain bounded too.
  _fr_repo="$1"
  _fr_base="$2"
  _fr_head="$3"
  _fr_files=()
  _fr_excluded=0
  while IFS= read -r -d '' _fr_f; do
    if clf_sensitive_path "$_fr_f"; then
      _fr_excluded=$((_fr_excluded + 1))
      continue
    fi
    _fr_base_sz="$(git -C "$_fr_repo" cat-file -s "$_fr_base:$_fr_f" 2>/dev/null)"
    _fr_head_sz="$(git -C "$_fr_repo" cat-file -s "$_fr_head:$_fr_f" 2>/dev/null)"
    if [ "${_fr_base_sz:-0}" -gt "$CLF_MAX_FILE_BYTES" ] || [ "${_fr_head_sz:-0}" -gt "$CLF_MAX_FILE_BYTES" ]; then
      _fr_excluded=$((_fr_excluded + 1))
      continue
    fi
    _fr_files+=("$_fr_f")
  done < <(git -C "$_fr_repo" diff --name-only -z "$_fr_base" "$_fr_head" -- 2>/dev/null)
  [ "${#_fr_files[@]}" -gt 0 ] || return 1
  [ "$_fr_excluded" -gt 0 ] && printf '### note: %s committed file(s) excluded (sensitive path or size cap)\n' "$_fr_excluded"
  _fr_specs=()
  for _fr_f in "${_fr_files[@]}"; do
    _fr_specs+=(":(literal)$_fr_f")
  done
  git -C "$_fr_repo" diff "$_fr_base" "$_fr_head" -- "${_fr_specs[@]}" 2>/dev/null
}

clf_filtered_range_status() {
  # Emit only reviewable committed path names. The range diff carries exact statuses; this compact
  # summary deliberately omits sensitive and oversized names/content from the Claude payload.
  _rs_repo="$1"
  _rs_base="$2"
  _rs_head="$3"
  _rs_excluded=0
  while IFS= read -r -d '' _rs_f; do
    if clf_sensitive_path "$_rs_f"; then
      _rs_excluded=$((_rs_excluded + 1))
      continue
    fi
    _rs_base_sz="$(git -C "$_rs_repo" cat-file -s "$_rs_base:$_rs_f" 2>/dev/null)"
    _rs_head_sz="$(git -C "$_rs_repo" cat-file -s "$_rs_head:$_rs_f" 2>/dev/null)"
    if [ "${_rs_base_sz:-0}" -gt "$CLF_MAX_FILE_BYTES" ] || [ "${_rs_head_sz:-0}" -gt "$CLF_MAX_FILE_BYTES" ]; then
      _rs_excluded=$((_rs_excluded + 1))
      continue
    fi
    printf '%s\n' "$_rs_f"
  done < <(git -C "$_rs_repo" diff --name-only -z "$_rs_base" "$_rs_head" -- 2>/dev/null)
  [ "$_rs_excluded" -gt 0 ] && printf '[%s committed path(s) redacted by policy]\n' "$_rs_excluded"
}

clf_git_common_dir() {
  # Compare this canonical path to recognize linked worktrees of the same repository without
  # weakening the artifact repository binding.
  _gc_repo="$1"
  _gc_dir="$(git -C "$_gc_repo" rev-parse --git-common-dir 2>/dev/null)"
  [ -n "$_gc_dir" ] || return 1
  case "$_gc_dir" in /*) ;; *) _gc_dir="$_gc_repo/$_gc_dir";; esac
  "$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$_gc_dir" 2>/dev/null
}

clf_truncate_bytes() {
  # $1 = text, $2 = byte cap, $3 = label for the marker. Cuts on a line boundary where possible and
  # appends an explicit marker so Claude knows it reviewed a partial payload.
  _tb_len="$(printf '%s' "$1" | wc -c | tr -d ' ')"
  if [ "${_tb_len:-0}" -le "$2" ]; then
    printf '%s' "$1"
    return 0
  fi
  _tb_head="$(printf '%s' "$1" | head -c "$2")"
  _tb_trim="${_tb_head%"${_tb_head##*$'\n'}"}"
  [ -n "$_tb_trim" ] && _tb_head="$_tb_trim"
  printf '%s\n[... %s truncated at %s bytes ...]\n' "$_tb_head" "$3" "$2"
}

clf_build_claude_args() {
  # Tool sandbox, built once and applied on EVERY attempt (including the retry) so a failed first
  # try can never silently widen Claude's tool access beyond what CLAUDE_FUSION_TOOLS asked for.
  if [ "$TOOLSMODE" = "none" ]; then
    CLAUDE_TOOL_ARGS=(--tools "")
  else
    _ba_allow="Read Grep Glob Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(ls:*) Bash(cat:*)"
    # CLF_ALLOW_FANOUT=0 keeps a single Claude agent even in ultracode mode. Verification running
    # inside a codex-dw worker uses it so one worker cannot expand into a nested Claude fan-out.
    if clf_ultracode_enabled && [ "$CLF_ALLOW_FANOUT" = "1" ]; then
      _ba_allow="$_ba_allow Task Workflow ToolSearch"
    fi
    CLAUDE_TOOL_ARGS=(--allowedTools "$_ba_allow")
  fi
  clf_set_claude_args "$CLAUDE_MODEL" "$CLAUDE_EFFORT" text
}

clf_set_claude_args() {
  # $1 model, $2 effort, $3 text|structured. CLF_KEEP_SESSION/CLF_RESUME_ID are set only by the
  # UserPromptSubmit hook when continuity is explicitly enabled; review hooks always stay fresh.
  _ca_model="$1"
  _ca_effort="$2"
  _ca_format="$3"
  CLAUDE_ARGS=(-p "${CLAUDE_SAFE_ARGS[@]}" --permission-mode plan)
  if [ "${CLF_KEEP_SESSION:-0}" = "1" ]; then
    [ -n "${CLF_RESUME_ID:-}" ] && CLAUDE_ARGS+=(--resume "$CLF_RESUME_ID")
  else
    CLAUDE_ARGS+=(--no-session-persistence)
  fi
  if [ "$_ca_format" = "structured" ]; then
    CLAUDE_ARGS+=(--output-format json --json-schema "$CLF_JSON_SCHEMA")
  else
    CLAUDE_ARGS+=(--output-format text)
  fi
  [ -n "$_ca_model" ] && CLAUDE_ARGS+=(--model "$_ca_model")
  [ -n "$_ca_effort" ] && CLAUDE_ARGS+=(--effort "$_ca_effort")
  CLAUDE_ARGS+=("${CLAUDE_TOOL_ARGS[@]}")
}

clf_run_claude() {
  # Read-only Claude invocation. Every retry consumes the same budget captured at hook start; once
  # exhausted return timeout semantics without starting another process. Both Fusion-active flags
  # mark the subtree so nested hooks short-circuit.
  _rc_elapsed=$(( ${SECONDS:-0} - ${CLF_HOOK_STARTED:-0} ))
  _rc_remaining=$(( CLAUDE_TIMEOUT - _rc_elapsed ))
  if [ "$_rc_remaining" -le 0 ]; then
    clf_dbg "shared hook budget exhausted before Claude attempt"
    return 124
  fi
  printf '%s' "$CLAUDE_PROMPT" | CLAUDE_FUSION_ACTIVE=1 CODEX_FUSION_ACTIVE=1 timeout "$_rc_remaining" \
    "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}" 2>/dev/null
}

clf_run_claude_with_retry() {
  # Sets CLF_OUTPUT / CLF_RC. On a fast failure (e.g. unknown model/effort), retry once with the
  # fixed fallback model/effort. KEEP the tool sandbox so a retry can never widen Claude's access.
  # Timeouts (rc 124) are not retried.
  CLF_OUTPUT="$(clf_run_claude)"
  CLF_RC=$?
  if { [ "$CLF_RC" -ne 0 ] || [ -z "$CLF_OUTPUT" ]; } && [ "$CLF_RC" -ne 124 ]; then
    clf_dbg "claude rc=$CLF_RC / empty; retrying with model=$CLF_FALLBACK_MODEL, effort=$CLF_FALLBACK_EFFORT (sandbox preserved)"
    clf_set_claude_args "$CLF_FALLBACK_MODEL" "$CLF_FALLBACK_EFFORT" text
    CLF_OUTPUT="$(clf_run_claude)"
    CLF_RC=$?
  fi
}

clf_parse_structured_envelope() {
  # Validate both Claude's outer JSON result envelope and the contract-specific structured payload.
  # On success set CLF_STRUCTURED_OUTPUT and CLF_CLAUDE_SESSION_ID without ever eval'ing model data.
  _se_parsed="$(printf '%s' "$1" | "$PY" -c '
import base64, json, sys
kind = sys.argv[1]
try:
    envelope = json.load(sys.stdin)
    if not isinstance(envelope, dict): raise ValueError()
    if envelope.get("type") != "result" or envelope.get("subtype") != "success": raise ValueError()
    if envelope.get("is_error") is not False: raise ValueError()
    output = envelope.get("structured_output")
    if not isinstance(output, dict): raise ValueError()
    if kind == "analysis":
        if not isinstance(output.get("analysis"), str) or not output["analysis"].strip(): raise ValueError()
        questions = output.get("questions", [])
        if not isinstance(questions, list) or len(questions) > 3: raise ValueError()
        for q in questions:
            if not isinstance(q, dict) or q.get("importance") not in ("required", "advisory"): raise ValueError()
            if not all(isinstance(q.get(k), str) and q[k].strip() for k in ("header", "prompt", "recommendation")): raise ValueError()
            opts = q.get("options")
            if not isinstance(opts, list) or not 2 <= len(opts) <= 3: raise ValueError()
            if not all(isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"].strip() and isinstance(o.get("description"), str) and o["description"].strip() for o in opts): raise ValueError()
            if q["recommendation"] not in [o["label"] for o in opts]: raise ValueError()
    elif kind == "review":
        if output.get("verdict") not in ("PASS", "ISSUES_FOUND"): raise ValueError()
        findings = output.get("findings", [])
        if not isinstance(findings, list) or not all(isinstance(x, str) for x in findings): raise ValueError()
        if output["verdict"] == "ISSUES_FOUND" and not findings: raise ValueError()
    else:
        raise ValueError()
    compact = json.dumps(output, separators=(",", ":"), ensure_ascii=False).encode()
    session = envelope.get("session_id", "")
    if not isinstance(session, str): session = ""
    print(base64.b64encode(compact).decode())
    print(base64.b64encode(session.encode()).decode())
except Exception:
    sys.exit(1)
' "$CLF_CONTRACT_TYPE" 2>/dev/null)" || return 1
  CLF_STRUCTURED_OUTPUT="$(printf '%s' "$_se_parsed" | sed -n '1p' | base64 -d 2>/dev/null)" || return 1
  CLF_CLAUDE_SESSION_ID="$(printf '%s' "$_se_parsed" | sed -n '2p' | base64 -d 2>/dev/null)" || return 1
  [ -n "$CLF_STRUCTURED_OUTPUT" ]
}

clf_run_structured_attempt() {
  clf_set_claude_args "$1" "$2" structured
  _sa_raw="$(clf_run_claude)"
  CLF_RC=$?
  [ "$CLF_RC" -eq 0 ] || return 1
  clf_parse_structured_envelope "$_sa_raw" || { CLF_RC=65; return 1; }
  CLF_OUTPUT="$CLF_STRUCTURED_OUTPUT"
  CLF_RESULT_MODE=structured
  return 0
}

clf_run_claude_contract() {
  # Structured-capable clients get primary and fixed-fallback schema attempts. Any non-success
  # envelope, is_error, malformed JSON, missing structured_output, or contract violation is a failed
  # attempt. After structured exhaustion make one final fixed-fallback text attempt. A timeout is
  # never retried. Older clients retain the two-attempt text path.
  CLF_OUTPUT=""
  CLF_RC=1
  CLF_RESULT_MODE=text
  CLF_CLAUDE_SESSION_ID=""

  if ! clf_structured_output_supported; then
    clf_set_claude_args "$CLAUDE_MODEL" "$CLAUDE_EFFORT" text
    clf_run_claude_with_retry
    return 0
  fi

  if clf_run_structured_attempt "$CLAUDE_MODEL" "$CLAUDE_EFFORT"; then return 0; fi
  [ "$CLF_RC" -eq 124 ] && return 0

  # A stale or invalid resumable session must not poison later turns. Discard it and retry the same
  # primary model fresh before moving to the fixed fallback.
  if [ -n "${CLF_RESUME_ID:-}" ]; then
    clf_dbg "resume failed; discarding saved Claude session and retrying fresh"
    [ -n "${CLF_RESUME_FILE:-}" ] && rm -f "$CLF_RESUME_FILE" 2>/dev/null
    CLF_RESUME_ID=""
    if clf_run_structured_attempt "$CLAUDE_MODEL" "$CLAUDE_EFFORT"; then return 0; fi
    [ "$CLF_RC" -eq 124 ] && return 0
  fi

  clf_dbg "structured primary failed; trying model=$CLF_FALLBACK_MODEL, effort=$CLF_FALLBACK_EFFORT"
  if clf_run_structured_attempt "$CLF_FALLBACK_MODEL" "$CLF_FALLBACK_EFFORT"; then return 0; fi
  [ "$CLF_RC" -eq 124 ] && return 0

  clf_dbg "structured attempts exhausted; making final fixed-fallback text attempt"
  clf_set_claude_args "$CLF_FALLBACK_MODEL" "$CLF_FALLBACK_EFFORT" text
  CLF_OUTPUT="$(clf_run_claude)"
  CLF_RC=$?
  CLF_RESULT_MODE=text
}

clf_store_session_mapping() {
  # Write only valid Claude session identifiers, atomically, into the owned per-user state dir.
  _ss_file="$1"
  _ss_id="$(clf_sanitize_session_id "$2")"
  [ -n "$_ss_file" ] && [ -n "$_ss_id" ] && clf_ensure_state_dir || return 0
  _ss_tmp="$_ss_file.tmp.$$"
  printf '%s\n' "$_ss_id" >"$_ss_tmp" 2>/dev/null && mv -f "$_ss_tmp" "$_ss_file" 2>/dev/null
  rm -f "$_ss_tmp" 2>/dev/null
}

clf_first_nonempty_line() {
  printf '%s' "$1" | grep -m1 -vE '^[[:space:]]*$'
}
