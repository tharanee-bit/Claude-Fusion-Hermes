#!/usr/bin/env bash
# Canonical claude-fusion-stop.sh (Codex Stop hook)
# Reviews a gated active-checkout diff and up to four oldest unreviewed codex-dw integration
# artifacts in one read-only Claude call. Artifact discovery runs on every ordinary Stop so a
# detached workflow can finish after its originating turn. Loop-safe and deliberately fail-open.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/claude-fusion-common.sh
. "$SCRIPT_DIR/claude-fusion-common.sh" 2>/dev/null || exit 0
clf_init_common "STOP"

clf_nested_fusion_active && exit 0
clf_setup_claude_runtime || exit 0

MAX_DIFF=20000
MAX_CHARS=12000
MAX_ARTIFACTS=4

INPUT="$(cat)"
[ -n "$INPUT" ] || exit 0
FIELDS="$(clf_parse_fields "$INPUT" cwd session_id turn_id stop_hook_active)"
CWD="$(clf_field "$FIELDS" 1)"
SESSION_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 2)")"
TURN_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 3)")"
STOP_ACTIVE="$(clf_field "$FIELDS" 4)"
TURN_KEY="$(clf_turn_key "$SESSION_ID" "$TURN_ID")"

# A continuation forced by this hook must never recursively review itself.
[ "$STOP_ACTIVE" = "true" ] && { clf_dbg "stop_hook_active -> exit"; exit 0; }
[ -d "$CWD" ] || CWD="$PWD"
[ -n "$SESSION_ID" ] || { clf_dbg "missing/invalid session id -> exit"; exit 0; }

MARKER="$(clf_find_complex_marker "$SESSION_ID" "$TURN_ID")"
STATE_KEY="$TURN_KEY"
[ -n "$STATE_KEY" ] || STATE_KEY="$SESSION_ID"
REVIEWED_FILE="$STATE_DIR/$SESSION_ID.reviewed"
FAILED_FILE="$STATE_DIR/$SESSION_ID.failed-review"
ARTIFACT_RECEIPTS_FILE="$STATE_DIR/$SESSION_ID.dw-reviewed"

ACTIVE_DIFF_FULL=""
ACTIVE_CHANGED=""
ACTIVE_HASH=""
ACTIVE_INCLUDED=0

# A prompt marker still controls active-checkout review. External artifacts do not need one.
if [ -n "$MARKER" ]; then
  BASE="$(cat "$MARKER" 2>/dev/null)"
  case "$BASE" in *[!0-9a-f]*) BASE="";; esac
  if [ -z "$BASE" ] || ! git -C "$CWD" rev-parse --verify --quiet "$BASE^{commit}" >/dev/null 2>&1; then
    [ -n "$BASE" ] && clf_dbg "stored base sha unresolvable -> falling back to HEAD"
    BASE="HEAD"
  fi
  ACTIVE_DIFF_FULL="$(clf_filtered_diff "$CWD" "$BASE" 2>/dev/null)"
  if [ -z "$ACTIVE_DIFF_FULL" ]; then
    clf_dbg "empty active diff -> consume marker"
    rm -f "$MARKER" 2>/dev/null
    clf_cleanup_subagent_turn "$STATE_KEY"
    MARKER=""
  else
    ACTIVE_HASH="$(printf '%s' "$ACTIVE_DIFF_FULL" | git hash-object --stdin 2>/dev/null)"
    LAST_REVIEWED="$(cat "$REVIEWED_FILE" 2>/dev/null)"
    if [ -n "$ACTIVE_HASH" ] && [ "$LAST_REVIEWED" = "$ACTIVE_HASH" ]; then
      clf_dbg "active diff already reviewed -> consume marker"
      rm -f "$MARKER" 2>/dev/null
      clf_cleanup_subagent_turn "$STATE_KEY"
      MARKER=""
      ACTIVE_DIFF_FULL=""
    else
      ACTIVE_INCLUDED=1
      ACTIVE_CHANGED="$(clf_filtered_status "$CWD")"
    fi
  fi
fi

store_artifact_receipts() {
  [ "$#" -gt 0 ] || return 0
  clf_ensure_state_dir || return 0
  umask 077
  for _sr_pair in "$@"; do
    printf '%s\n' "$_sr_pair" >>"$ARTIFACT_RECEIPTS_FILE" 2>/dev/null
  done
  chmod 600 "$ARTIFACT_RECEIPTS_FILE" 2>/dev/null || true
}

# The producer writes state below CODEX_HOME. Parse untrusted JSON in Python, require the exact
# additive protocol, bind it to this Codex session, exclude existing (artifact id, head) receipts,
# and sort oldest first. Git/repository validation remains shell-side below.
RUNS_ROOT="${CODEX_HOME:-$HOME/.codex}/dynamic-workflows/runs"
ARTIFACT_LINES=""
if [ -d "$RUNS_ROOT" ]; then
  ARTIFACT_LINES="$("$PY" - "$RUNS_ROOT" "$SESSION_ID" "$ARTIFACT_RECEIPTS_FILE" <<'PY'
import base64
import datetime as dt
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
session = sys.argv[2]
receipt_path = Path(sys.argv[3])
id_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
sha_re = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
receipts = set()
try:
    for line in receipt_path.read_text(encoding="utf-8").splitlines():
        artifact_id, head = line.split("\t", 1)
        if id_re.fullmatch(artifact_id) and sha_re.fullmatch(head):
            receipts.add((artifact_id, head))
except (OSError, ValueError):
    pass

candidates = []
seen = set(receipts)
try:
    run_dirs = list(root.iterdir())
except OSError:
    run_dirs = []
for run_dir in run_dirs:
    try:
        state = run_dir / "state.json"
        if run_dir.is_symlink() or state.is_symlink() or not state.is_file() or state.stat().st_size > 2_000_000:
            continue
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        continue
    artifacts = data.get("reviewArtifacts")
    if not isinstance(artifacts, list):
        continue
    for artifact in artifacts[:16]:
        if not isinstance(artifact, dict):
            continue
        artifact_id = artifact.get("id")
        base = artifact.get("baseCommit")
        head = artifact.get("headCommit")
        repository = artifact.get("repositoryRoot")
        branch = artifact.get("branch")
        status = artifact.get("runStatus")
        published = artifact.get("publishedAt")
        if artifact.get("protocol") != "codex-dw.review-artifact/v1" or artifact.get("kind") != "git-range":
            continue
        if artifact.get("reviewSessionId") != session or status not in ("completed", "failed", "stopped"):
            continue
        if not isinstance(artifact_id, str) or not id_re.fullmatch(artifact_id):
            continue
        if not isinstance(base, str) or not sha_re.fullmatch(base) or not isinstance(head, str) or not sha_re.fullmatch(head):
            continue
        if (
            not isinstance(repository, str)
            or not repository.startswith("/")
            or len(repository) > 4096
            or any(c in repository for c in "\r\n\t")
        ):
            continue
        if not isinstance(branch, str) or not branch or len(branch) > 512 or any(c in branch for c in "\r\n\t"):
            continue
        if not isinstance(published, str):
            continue
        try:
            timestamp = dt.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        if (artifact_id, head) in seen:
            continue
        seen.add((artifact_id, head))
        candidates.append((timestamp, artifact_id, head, repository, base, branch, status, published))

def enc(value):
    return base64.b64encode(value.encode()).decode()

for _, artifact_id, head, repository, base, branch, status, published in sorted(candidates):
    print("\t".join(enc(v) for v in (artifact_id, repository, base, head, branch, status, published)))
PY
)"
fi

ACTIVE_COMMON="$(clf_git_common_dir "$CWD")"
ARTIFACT_COUNT=0
ARTIFACT_DIFF_FULL=""
ARTIFACT_CHANGED=""
ARTIFACT_REVIEW_RECEIPTS=()
ARTIFACT_EMPTY_RECEIPTS=()

while IFS=$'\t' read -r ID64 REPO64 BASE64 HEAD64 BRANCH64 STATUS64 PUBLISHED64; do
  [ -n "$ID64" ] || continue
  ARTIFACT_ID="$(printf '%s' "$ID64" | base64 -d 2>/dev/null)"
  ARTIFACT_REPO="$(printf '%s' "$REPO64" | base64 -d 2>/dev/null)"
  ARTIFACT_BASE="$(printf '%s' "$BASE64" | base64 -d 2>/dev/null)"
  ARTIFACT_HEAD="$(printf '%s' "$HEAD64" | base64 -d 2>/dev/null)"
  ARTIFACT_BRANCH="$(printf '%s' "$BRANCH64" | base64 -d 2>/dev/null)"
  ARTIFACT_STATUS="$(printf '%s' "$STATUS64" | base64 -d 2>/dev/null)"
  ARTIFACT_PUBLISHED="$(printf '%s' "$PUBLISHED64" | base64 -d 2>/dev/null)"
  RECEIPT="$ARTIFACT_ID"$'\t'"$ARTIFACT_HEAD"

  # Match the current repository (linked worktrees share a common dir), require the producer's root
  # to be the repository top level, and verify both commit objects plus the named branch tip.
  [ -d "$ARTIFACT_REPO" ] || continue
  ARTIFACT_TOP="$(git -C "$ARTIFACT_REPO" rev-parse --show-toplevel 2>/dev/null)"
  [ -n "$ARTIFACT_TOP" ] || continue
  ARTIFACT_REAL="$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ARTIFACT_REPO" 2>/dev/null)"
  ARTIFACT_TOP_REAL="$("$PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ARTIFACT_TOP" 2>/dev/null)"
  [ "$ARTIFACT_REAL" = "$ARTIFACT_TOP_REAL" ] || continue
  ARTIFACT_COMMON="$(clf_git_common_dir "$ARTIFACT_REPO")"
  [ -n "$ACTIVE_COMMON" ] && [ "$ARTIFACT_COMMON" = "$ACTIVE_COMMON" ] || continue
  git check-ref-format --branch "$ARTIFACT_BRANCH" >/dev/null 2>&1 || continue
  git -C "$ARTIFACT_REPO" cat-file -e "$ARTIFACT_BASE^{commit}" 2>/dev/null || continue
  git -C "$ARTIFACT_REPO" cat-file -e "$ARTIFACT_HEAD^{commit}" 2>/dev/null || continue
  BRANCH_HEAD="$(git -C "$ARTIFACT_REPO" rev-parse --verify "refs/heads/$ARTIFACT_BRANCH^{commit}" 2>/dev/null)"
  [ "$BRANCH_HEAD" = "$ARTIFACT_HEAD" ] || continue
  [ "$ARTIFACT_BASE" != "$ARTIFACT_HEAD" ] || continue
  git -C "$ARTIFACT_REPO" merge-base --is-ancestor "$ARTIFACT_BASE" "$ARTIFACT_HEAD" >/dev/null 2>&1 || continue

  RANGE_DIFF="$(clf_filtered_range_diff "$ARTIFACT_REPO" "$ARTIFACT_BASE" "$ARTIFACT_HEAD" 2>/dev/null)"
  if [ -z "$RANGE_DIFF" ]; then
    ARTIFACT_EMPTY_RECEIPTS+=("$RECEIPT")
    continue
  fi
  [ "$ARTIFACT_COUNT" -lt "$MAX_ARTIFACTS" ] || continue
  ARTIFACT_COUNT=$((ARTIFACT_COUNT + 1))
  RANGE_CHANGED="$(clf_filtered_range_status "$ARTIFACT_REPO" "$ARTIFACT_BASE" "$ARTIFACT_HEAD")"
  ARTIFACT_CHANGED+="
codex-dw artifact $ARTIFACT_ID ($ARTIFACT_STATUS, $ARTIFACT_BRANCH, $ARTIFACT_BASE..$ARTIFACT_HEAD):
$RANGE_CHANGED"
  ARTIFACT_DIFF_FULL+="

### codex-dw review artifact $ARTIFACT_ID
Run status: $ARTIFACT_STATUS
Integration branch: $ARTIFACT_BRANCH
Published: $ARTIFACT_PUBLISHED
Range: $ARTIFACT_BASE..$ARTIFACT_HEAD

$RANGE_DIFF"
  ARTIFACT_REVIEW_RECEIPTS+=("$RECEIPT")
done <<<"$ARTIFACT_LINES"

# A valid range whose complete content was removed by the established sensitivity/size policy has
# no reviewable payload. Record it exactly like an empty active diff so it cannot rescan forever.
store_artifact_receipts "${ARTIFACT_EMPTY_RECEIPTS[@]}"

[ "$ACTIVE_INCLUDED" -eq 1 ] || [ "$ARTIFACT_COUNT" -gt 0 ] || { clf_dbg "no reviewable active diff or artifacts -> exit"; exit 0; }

REVIEW_FULL=""
CHANGED_FULL=""
if [ "$ACTIVE_INCLUDED" -eq 1 ]; then
  REVIEW_FULL="### active checkout diff
$ACTIVE_DIFF_FULL"
  CHANGED_FULL="Active checkout:
$ACTIVE_CHANGED"
fi
if [ "$ARTIFACT_COUNT" -gt 0 ]; then
  REVIEW_FULL+="$ARTIFACT_DIFF_FULL"
  CHANGED_FULL+="$ARTIFACT_CHANGED"
fi
DIFF="$(clf_truncate_bytes "$REVIEW_FULL" "$MAX_DIFF" "combined review artifact")"
CHANGED="$(clf_truncate_bytes "$CHANGED_FULL" 3000 "changed files")"
# Failures are bounded by the complete, untruncated combined payload. Successful artifact dedupe is
# finer-grained and uses each stable (artifact id, head commit) pair.
DIFF_HASH="$(printf '%s' "$REVIEW_FULL" | git hash-object --stdin 2>/dev/null)"

retry_limit() {
  clf_positive_int "${CLAUDE_FUSION_STOP_RETRY_LIMIT:-2}" 2
}

retry_exhausted() {
  [ -f "$FAILED_FILE" ] || return 1
  read -r _hash _count <"$FAILED_FILE" 2>/dev/null
  [ "$_hash" = "$DIFF_HASH" ] || return 1
  [ "${_count:-0}" -ge "$(retry_limit)" ]
}

record_review_failure() {
  clf_ensure_state_dir || return 0
  _old_hash=""
  _old_count=0
  [ -f "$FAILED_FILE" ] && read -r _old_hash _old_count <"$FAILED_FILE" 2>/dev/null
  if [ "$_old_hash" = "$DIFF_HASH" ]; then
    _new_count=$(( ${_old_count:-0} + 1 ))
  else
    _new_count=1
  fi
  printf '%s %s\n' "$DIFF_HASH" "$_new_count" >"$FAILED_FILE" 2>/dev/null
  clf_dbg "recorded review failure count=$_new_count hash=$DIFF_HASH"
}

clear_review_failure() {
  rm -f "$FAILED_FILE" 2>/dev/null
}

store_active_reviewed() {
  [ "$ACTIVE_INCLUDED" -eq 1 ] && [ -n "$ACTIVE_HASH" ] || return 0
  clf_ensure_state_dir && printf '%s\n' "$ACTIVE_HASH" >"$REVIEWED_FILE" 2>/dev/null
}

complete_review_state() {
  [ -n "$MARKER" ] && rm -f "$MARKER" 2>/dev/null
  clf_cleanup_subagent_turn "$STATE_KEY"
  store_active_reviewed
  store_artifact_receipts "${ARTIFACT_REVIEW_RECEIPTS[@]}"
  clear_review_failure
}

retry_exhausted && { clf_dbg "review retry cap reached for unchanged combined payload"; exit 0; }

if [ "${#CLAUDE_SAFE_ARGS[@]}" -gt 0 ] && ! clf_safe_mode_supported; then
  clf_dbg "claude lacks --safe-mode -> exit (review state kept; update Claude Code or set CLAUDE_FUSION_SAFE_MODE=0)"
  exit 0
fi

WF_NOTE=""
if clf_ultracode_enabled; then
  WF_NOTE="
Run a READ-ONLY multi-agent adversarial review (dynamic workflows / parallel subagents) for deeper
coverage: find candidate issues, then have independent agents try to refute each one before you
report it. Do not edit files or run mutating commands. This consultation is advisory and must never
merge a codex-dw integration branch."
  [ "$CUSTOM_CLAUDE_CONTEXT" -eq 1 ] || WF_NOTE="$WF_NOTE
Claude Fusion is running you in --safe-mode, so orchestrate with the built-in Task/Workflow tools
and do not rely on user/project Claude customizations, skills, plugins, saved workflows, memory,
MCP servers, or custom agents."
elif [ "$DEPTH" = "workflow" ]; then
  WF_NOTE="
Run a thorough READ-ONLY review rather than a single quick pass."
  [ "$CUSTOM_CLAUDE_CONTEXT" -eq 1 ] || WF_NOTE="$WF_NOTE
Claude Fusion is running you in --safe-mode, so do not rely on user/project Claude customizations,
skills, plugins, workflows, memory, MCP servers, or custom agents."
fi

CLAUDE_PREFIX=""
clf_ultracode_enabled && CLAUDE_PREFIX="ultracode: "
CLAUDE_PROMPT="${CLAUDE_PREFIX}You are Claude acting as an independent code reviewer for the OpenAI Codex agent.
You are running automatically from a Codex Stop hook, in READ-ONLY mode.
Do not edit files. Do not run destructive commands. Do not merge, reset, or modify any integration
branch. Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.$WF_NOTE

Review the combined final integration artifact below for SERIOUS problems only: correctness bugs,
security vulnerabilities, data-loss risks, concurrency/race issues, and broken or missing tests.
It may contain an active-checkout diff, committed codex-dw integration ranges, or both. Treat each
codex-dw base..head range as an advisory review target; the branch remains user-controlled.
Ignore pure style/formatting nits.

The VERY FIRST line of your response MUST be exactly one of:
CLAUDE_REVIEW_VERDICT: PASS
CLAUDE_REVIEW_VERDICT: ISSUES_FOUND

If ISSUES_FOUND, list each serious issue (most important first) as:
- <artifact or file:line> : <problem> : <minimal fix>
Keep it under 800 words.

Repository:
$CWD

Changed files:
$CHANGED

Combined diff:
$DIFF"

clf_build_claude_args
CLF_CONTRACT_TYPE=review
CLF_JSON_SCHEMA='{"type":"object","additionalProperties":false,"required":["verdict","findings"],"properties":{"verdict":{"type":"string","enum":["PASS","ISSUES_FOUND"]},"findings":{"type":"array","maxItems":12,"items":{"type":"string"}}}}'
CLF_KEEP_SESSION=0
CLF_RESUME_ID=""
clf_dbg "running claude review (model=$CLAUDE_MODEL, effort=$CLAUDE_EFFORT, depth=$DEPTH, ultracode=$ULTRACODE, artifacts=$ARTIFACT_COUNT)"
clf_run_claude_contract
RC="$CLF_RC"
[ "$RC" -eq 0 ] || { record_review_failure; clf_dbg "claude rc!=0 -> exit (review state kept for retry)"; exit 0; }
[ -n "$CLF_OUTPUT" ] || { record_review_failure; clf_dbg "empty review -> exit (review state kept)"; exit 0; }

if [ "$CLF_RESULT_MODE" = "structured" ]; then
  REVIEW="$(printf '%s' "$CLF_OUTPUT" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
print("CLAUDE_REVIEW_VERDICT: " + d["verdict"])
for item in d.get("findings", []):
    print("- " + item)
' 2>/dev/null)"
else
  REVIEW="$CLF_OUTPUT"
fi
[ -n "$REVIEW" ] || { record_review_failure; clf_dbg "review extraction failed (review state kept)"; exit 0; }

VERDICT_LINE="$(clf_first_nonempty_line "$REVIEW")"
if ! printf '%s' "$VERDICT_LINE" | grep -qiE 'CLAUDE_REVIEW_VERDICT:[[:space:]]*ISSUES_FOUND'; then
  complete_review_state
  clf_dbg "verdict PASS/none -> exit"
  exit 0
fi

emit_block() {
  REVIEW="$(clf_truncate_bytes "$1" 100000 "claude review")" MAX_CHARS="$MAX_CHARS" "$PY" <<'PY'
import os, json
r = os.environ.get("REVIEW", "")
try: m = int(os.environ.get("MAX_CHARS", "12000"))
except Exception: m = 12000
if len(r) > m:
    r = r[:m]
    cut = r.rfind("\n")
    if cut > 0:
        r = r[:cut]
    r += "\n\n[...review truncated at " + str(m) + " chars...]"
reason = ("AUTOMATIC CLAUDE FUSION - POST-DIFF REVIEW:\n"
          "Claude independently reviewed the final integration artifact and flagged potential "
          "issues. Address the serious problems (correctness, security, data-loss, concurrency, "
          "broken tests) before finalizing, or explicitly justify why each is not a real issue. "
          "Do not merge a codex-dw integration branch automatically; you remain the final judge.\n\n" + r)
print(json.dumps({"decision": "block", "reason": reason}))
PY
}

# Persist successful receipts only after a PASS or after the ISSUES_FOUND continuation is delivered.
BLOCK_JSON="$(emit_block "$REVIEW")"
EMIT_RC=$?
[ "$EMIT_RC" -eq 0 ] && [ -n "$BLOCK_JSON" ] || { record_review_failure; clf_dbg "block json emit failed (review state kept)"; exit 0; }
printf '%s\n' "$BLOCK_JSON" || { record_review_failure; clf_dbg "block json delivery failed (review state kept)"; exit 0; }
complete_review_state
clf_dbg "blocked with ISSUES_FOUND"
exit 0
