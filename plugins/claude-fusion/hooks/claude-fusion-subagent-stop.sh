#!/usr/bin/env bash
# Canonical claude-fusion-subagent-stop.sh  (Codex SubagentStop hook)
# Adversarially verify the capped final message from every subagent under a gated parent turn, and
# from codex-dw workers during multiagent orchestration. At most the configured number of unique
# agents are verified per parent turn (or per workflow run); failures are deliberately fail-open.
# The event's agent_transcript_path is never read or transmitted.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/claude-fusion-common.sh
. "$SCRIPT_DIR/claude-fusion-common.sh" 2>/dev/null || exit 0
clf_init_common "SUBSTOP"

# The claude<->codex loop guard is absolute. The codex-dw worker flag is not: verification is the one
# lifecycle behavior Claude Fusion still offers inside an orchestration worker, opt-out per env.
clf_peer_fusion_active && exit 0
clf_enabled "$SUBAGENT_REVIEW" || { clf_dbg "disabled -> exit"; exit 0; }
WORKFLOW_MODE=0
if clf_workflow_worker; then
  clf_enabled "$WORKFLOW_VERIFY" || { clf_dbg "workflow verification disabled -> exit"; exit 0; }
  WORKFLOW_MODE=1
fi
clf_setup_claude_runtime || exit 0

MAX_DIFF=12000
MAX_MESSAGE_CHARS=12000
MAX_REVIEW_CHARS=12000

INPUT="$(cat)"
[ -n "$INPUT" ] || exit 0
# Deliberately exclude agent_transcript_path from this field list. Claude Fusion never opens it.
FIELDS="$(clf_parse_fields "$INPUT" cwd session_id turn_id stop_hook_active agent_id agent_type last_assistant_message)"
CWD="$(clf_field "$FIELDS" 1)"
SESSION_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 2)")"
TURN_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 3)")"
STOP_ACTIVE="$(clf_field "$FIELDS" 4)"
AGENT_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 5)")"
AGENT_TYPE="$(clf_field "$FIELDS" 6)"
LAST_MESSAGE="$(clf_field "$FIELDS" 7)"
TURN_KEY="$(clf_turn_key "$SESSION_ID" "$TURN_ID")"

[ "$STOP_ACTIVE" = "true" ] && { clf_dbg "stop_hook_active -> exit"; exit 0; }
[ -d "$CWD" ] || CWD="$PWD"
MARKER="$(clf_find_complex_marker "$SESSION_ID" "$TURN_ID")"
if [ "$WORKFLOW_MODE" -eq 1 ]; then
  # A workflow worker never runs its own UserPromptSubmit hook, so there is no parent marker to
  # gate on. The workflow run itself is the gate; budget the verification separately from any
  # interactive turn so orchestration cannot spend the parent turn's slots.
  BUDGET_KEY="$(clf_workflow_budget_key "$SESSION_ID" "$TURN_ID")"
  [ -n "$BUDGET_KEY" ] || { clf_dbg "no workflow budget key -> exit"; exit 0; }
  RESERVE_NS="workflow"
  REVIEW_LIMIT="$WORKFLOW_VERIFY_LIMIT"
else
  [ -n "$MARKER" ] || { clf_dbg "no complex parent marker -> exit"; exit 0; }
  [ -n "$TURN_KEY" ] || exit 0
  BUDGET_KEY="$TURN_KEY"
  RESERVE_NS="subagent"
  REVIEW_LIMIT="$SUBAGENT_REVIEW_LIMIT"
fi

# Keep the cap in Unicode characters, not bytes, then explicitly mark truncation.
LAST_MESSAGE="$(printf '%s' "$LAST_MESSAGE" | MAX_MESSAGE_CHARS="$MAX_MESSAGE_CHARS" "$PY" -c '
import os, sys
text = sys.stdin.read()
limit = int(os.environ.get("MAX_MESSAGE_CHARS", "12000"))
if len(text) > limit:
    text = text[:limit]
    cut = text.rfind("\n")
    if cut > 0: text = text[:cut]
    text += "\n\n[...subagent final message truncated at 12000 characters...]"
sys.stdout.write(text)
' 2>/dev/null)"
[ -n "$LAST_MESSAGE" ] || LAST_MESSAGE="(subagent returned no final assistant message)"

clf_ensure_state_dir || exit 0
[ "$WORKFLOW_MODE" -eq 1 ] && clf_cleanup_stale_workflow_reservations
if [ -z "$AGENT_ID" ]; then
  AGENT_ID="unknown-$(printf '%s\n%s' "$AGENT_TYPE" "$LAST_MESSAGE" | git hash-object --stdin 2>/dev/null)"
fi
DEDUPE_DIR="$(clf_reserve_dir "$RESERVE_NS" "$BUDGET_KEY" agent "$AGENT_ID")"
mkdir "$DEDUPE_DIR" 2>/dev/null || { clf_dbg "duplicate agent event -> exit"; exit 0; }

SLOT=""
_slot=1
while [ "$_slot" -le "$REVIEW_LIMIT" ]; do
  _candidate="$(clf_reserve_dir "$RESERVE_NS" "$BUDGET_KEY" slot "$_slot")"
  if mkdir "$_candidate" 2>/dev/null; then SLOT="$_slot"; break; fi
  _slot=$((_slot + 1))
done
[ -n "$SLOT" ] || { clf_dbg "subagent verification cap reached -> exit"; exit 0; }

BASE="$(cat "$MARKER" 2>/dev/null)"
case "$BASE" in *[!0-9a-f]*) BASE="";; esac
if [ -z "$BASE" ] || ! git -C "$CWD" rev-parse --verify --quiet "$BASE^{commit}" >/dev/null 2>&1; then
  BASE="HEAD"
fi
DIFF_FULL="$(clf_filtered_diff "$CWD" "$BASE" 2>/dev/null)"
if [ -n "$DIFF_FULL" ]; then
  DIFF="$(clf_truncate_bytes "$DIFF_FULL" "$MAX_DIFF" "parent-turn diff")"
else
  DIFF="(no reviewable repository diff yet; review the subagent's final message and reasoning claims)"
fi

if [ "${#CLAUDE_SAFE_ARGS[@]}" -gt 0 ] && ! clf_safe_mode_supported; then
  clf_dbg "claude lacks --safe-mode -> fail open"
  exit 0
fi

WF_NOTE=""
if [ "$WORKFLOW_MODE" -eq 1 ]; then
  # Already inside a codex-dw fan-out: verify as exactly one independent agent. Nesting a Claude
  # workflow inside a workflow worker would multiply cost outside the coordinator's budget.
  CLF_ALLOW_FANOUT=0
  WF_NOTE="
This subagent is a worker inside a Codex Dynamic Workflows (codex-dw) run, so you are the
independent verification stage for one worker's result. Verify this worker only: do not launch a
Claude workflow, Task fan-out, or nested codex-dw run, and do not review the whole orchestration.
The codex-dw coordinator owns execution and remains the final judge."
fi

CLAUDE_PREFIX=""
clf_ultracode_enabled && [ "$CLF_ALLOW_FANOUT" = "1" ] && CLAUDE_PREFIX="ultracode: "
CLAUDE_PROMPT="${CLAUDE_PREFIX}You are Claude acting as the adversarial verifier for an OpenAI Codex subagent.
You are running automatically from a synchronous Codex SubagentStop hook, in READ-ONLY mode.
Do not edit files or run mutating commands. Do not inspect credentials, tokens, .env files,
keychains, shell history, or auth files. The transcript is intentionally unavailable.$WF_NOTE

Your job is to try to REFUTE the subagent's result, not to summarize or approve it. Identify its
central claims, then check each one against the repository and the filtered diff below. Verify at
minimum: does the change do what the subagent says it does; are the cited files, symbols, and
behaviors real; do the stated tests exist and actually cover the claim; are there unhandled inputs,
security, data-loss, or concurrency consequences the subagent did not mention. Research-only
subagents still require verification for unsupported conclusions and invented evidence.

Report ISSUES_FOUND only for a SERIOUS defect you can point to concretely in the repository, the
diff, or the subagent's own reasoning. A claim you merely could not confirm is not a defect: say so
inside a PASS instead of blocking on it. Ignore style nits. Keep findings concise and actionable.

The VERY FIRST line of a text response MUST be exactly one of:
CLAUDE_REVIEW_VERDICT: PASS
CLAUDE_REVIEW_VERDICT: ISSUES_FOUND

If ISSUES_FOUND, list each issue as:
- <location if known> : <refuted claim or defect> : <minimal fix>

Repository:
$CWD

Agent id: $AGENT_ID
Agent type: ${AGENT_TYPE:-unknown}
Reserved verification slot: $SLOT of $REVIEW_LIMIT

Subagent final assistant message:
$LAST_MESSAGE

Filtered parent-turn diff:
$DIFF"

clf_build_claude_args
CLF_CONTRACT_TYPE=review
CLF_JSON_SCHEMA='{"type":"object","additionalProperties":false,"required":["verdict","findings"],"properties":{"verdict":{"type":"string","enum":["PASS","ISSUES_FOUND"]},"findings":{"type":"array","maxItems":12,"items":{"type":"string"}}}}'
CLF_KEEP_SESSION=0
CLF_RESUME_ID=""
clf_dbg "verifying subagent agent=$AGENT_ID slot=$SLOT/$REVIEW_LIMIT key=$BUDGET_KEY workflow=$WORKFLOW_MODE"
clf_run_claude_contract
[ "$CLF_RC" -eq 0 ] && [ -n "$CLF_OUTPUT" ] || { clf_dbg "verification failed -> fail open"; exit 0; }

if [ "$CLF_RESULT_MODE" = "structured" ]; then
  REVIEW="$(printf '%s' "$CLF_OUTPUT" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
print("CLAUDE_REVIEW_VERDICT: " + d["verdict"])
for item in d.get("findings", []): print("- " + item)
' 2>/dev/null)"
else
  REVIEW="$CLF_OUTPUT"
fi
VERDICT_LINE="$(clf_first_nonempty_line "$REVIEW")"
printf '%s' "$VERDICT_LINE" | grep -qiE 'CLAUDE_REVIEW_VERDICT:[[:space:]]*ISSUES_FOUND' || {
  clf_dbg "subagent verdict PASS/none"
  exit 0
}

REVIEW="$(clf_truncate_bytes "$REVIEW" 100000 "claude subagent review")" MAX_CHARS="$MAX_REVIEW_CHARS" "$PY" <<'PY'
import json, os
review = os.environ.get("REVIEW", "")
limit = int(os.environ.get("MAX_CHARS", "12000"))
if len(review) > limit:
    review = review[:limit]
    cut = review.rfind("\n")
    if cut > 0: review = review[:cut]
    review += "\n\n[...review truncated at 12000 characters...]"
reason = ("AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW:\n"
          "Claude independently verified this subagent's result and refuted part of it. Correct the "
          "finding before the subagent finishes, or explicitly justify why it does not hold.\n\n"
          + review)
print(json.dumps({"decision": "block", "reason": reason}))
PY
clf_dbg "blocked subagent with ISSUES_FOUND"
exit 0
