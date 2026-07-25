#!/usr/bin/env bash
# Canonical claude-fusion-userprompt.sh  (Codex UserPromptSubmit hook)
# Auto-consult Claude Code (READ-ONLY) as an independent peer on non-trivial coding prompts and
# inject its analysis into Codex's context. Mirror image of Codex Fusion, reversed: here Codex is
# the primary agent and Claude is the advisor.
# GUARANTEE: never blocks Codex on the no-action path -- always exits 0. Escape hatch: [no-claude].
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/claude-fusion-common.sh
. "$SCRIPT_DIR/claude-fusion-common.sh" 2>/dev/null || exit 0
clf_init_common "UPS"

# Recursion guard: if we are already inside a Fusion -> peer call (env inherited through the process
# tree), do nothing. This breaks any claude<->codex hook loop when both Fusions are installed.
clf_nested_fusion_active && exit 0

clf_setup_claude_runtime || exit 0

MAX_CHARS=12000

INPUT="$(cat)"
[ -n "$INPUT" ] || exit 0
FIELDS="$(clf_parse_fields "$INPUT" prompt cwd session_id turn_id)"
PROMPT="$(clf_field "$FIELDS" 1)"
CWD="$(clf_field "$FIELDS" 2)"
SESSION_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 3)")"
TURN_ID="$(clf_sanitize_session_id "$(clf_field "$FIELDS" 4)")"
TURN_KEY="$(clf_turn_key "$SESSION_ID" "$TURN_ID")"
[ -n "$PROMPT" ] || exit 0
[ -d "$CWD" ] || CWD="$PWD"

# Outside a git repo the Stop-hook diff review can never run; that must be visible, not silent
# (mirror of Codex Fusion's one-time notice). Codex documents systemMessage for user-visible hook
# status; additionalContext remains reserved for analysis and questions visible to the model.
# Keyed on the bare session id so the warning occurs once across turns.
NOGIT_MARKER="$STATE_DIR/$SESSION_ID.nogit-warned"
NONGIT_WARNING=""
if [ -n "$SESSION_ID" ] && [ ! -f "$NOGIT_MARKER" ] && ! git -C "$CWD" rev-parse --verify HEAD >/dev/null 2>&1; then
  NONGIT_WARNING="Claude Fusion: $CWD is not a git repository (or has no commits), so the Stop-hook diff review is disabled for this session. Open a repository folder to re-enable it."
fi

mark_nogit_warned() {
  [ -n "$NONGIT_WARNING" ] || return 0
  clf_ensure_state_dir && : >"$NOGIT_MARKER" 2>/dev/null
}

finish_skip() {
  if [ -n "$NONGIT_WARNING" ]; then
    mark_nogit_warned
    NONGIT_WARNING="$NONGIT_WARNING" "$PY" -c 'import os, json; print(json.dumps({"systemMessage": os.environ.get("NONGIT_WARNING", "")}))' 2>/dev/null
  fi
  exit 0
}

# --- escape hatch ---
case "$PROMPT" in *"[no-claude]"*) clf_dbg "skip: [no-claude]"; finish_skip;; esac
# --- re-fire guard: this prompt is our own Stop-hook continuation, not a fresh task ---
case "$PROMPT" in *"AUTOMATIC CLAUDE FUSION"*) clf_dbg "skip: own continuation"; finish_skip;; esac

# --- AGGRESSIVE gate: trigger unless clearly trivial / conversational / tiny ---
WORDS="$(printf '%s' "$PROMPT" | wc -w | tr -d ' ')"; WORDS="${WORDS:-0}"
[ "$WORDS" -lt 3 ] 2>/dev/null && { clf_dbg "skip: too short ($WORDS)"; finish_skip; }
FIRSTLINE="$(printf '%s' "$PROMPT" | sed -n '1p')"

# Action verbs. ACTION_RE gates the question check; STRONG_ACTION_RE (the unambiguous subset) means
# "real work" and overrides the trivial-edit heuristics below.
ACTION_RE='\b(implement|build|create|write|add|fix|debug|refactor|migrat|optimi[sz]|design|review|change|update|integrat|deploy|test|rewrite|redesign|secure|harden|delete|remove|configure|set ?up|wire|generate|scaffold|investigate|diagnose|profile)\b'
STRONG_ACTION_RE='\b(implement|build|create|refactor|redesign|rewrite|architect|design|deploy|scaffold|generate|debug|diagnose|profile|investigate|secure|harden|wire)\b|\b(migrat|optimi[sz]|integrat)[a-z]*'
HAS_STRONG=0
printf '%s' "$PROMPT" | grep -iqE "$STRONG_ACTION_RE" && HAS_STRONG=1

# 1) Whole-prompt conversational acknowledgement.
ACK_RE='^[[:space:]]*((thanks|thank you|thx|ok|okay|cool|nice|great|got it|hi|hello|hey|yo|sup|yes|no|sure|nvm|never ?mind|lgtm)[[:punct:][:space:]]*)+$'
printf '%s' "$PROMPT" | grep -ziqE "$ACK_RE" && { clf_dbg "skip: conversational"; finish_skip; }

# 1b) Short message opening with an acknowledgement and no action verb.
LEADING_ACK_RE='^[[:space:]]*(thanks|thank you|thx|ok|okay|cool|nice|great|got it|hi|hello|hey|yo|sup|yes|no|sure|nvm|never ?mind|lgtm)\b'
if [ "$WORDS" -lt 6 ] && [ "$HAS_STRONG" -eq 0 ] && printf '%s' "$FIRSTLINE" | grep -iqE "$LEADING_ACK_RE" && ! printf '%s' "$PROMPT" | grep -iqE "$ACTION_RE"; then
  clf_dbg "skip: conversational"; finish_skip
fi

# 2) Trivial micro-edits -- only when no strong action verb is present.
if [ "$HAS_STRONG" -eq 0 ]; then
  TRIVIAL_RE='fix(ing)? (a |the )?typo|\btypo\b|\brewor[dk]|\bwording\b|formatting|reindent|indentation|whitespace|\blint(ing)?\b|prettier|one[- ]?liner|spelling|capitali[sz]'
  printf '%s' "$PROMPT" | grep -iqE "$TRIVIAL_RE" && { clf_dbg "skip: trivial"; finish_skip; }
  printf '%s' "$PROMPT" | grep -ziqE '(add|fix|update|edit) (a |the )?comments?[[:space:]]*$' && { clf_dbg "skip: trivial"; finish_skip; }
  printf '%s' "$FIRSTLINE" | grep -iqE '^[[:space:]]*(rename|reformat|format)\b' && { clf_dbg "skip: trivial"; finish_skip; }
fi

# 3) Short, pure question with no coding-action verb -> skip.
QUESTION_RE='^[[:space:]]*(what|why|how|when|who|where|which|is|are|was|were|does|do|did|can|could|should|would|will|explain|describe|summari[sz]e|tell me|define|meaning of)\b'
if [ "$WORDS" -lt 16 ] && printf '%s' "$FIRSTLINE" | grep -iqE "$QUESTION_RE" && ! printf '%s' "$PROMPT" | grep -iqE "$ACTION_RE"; then
  clf_dbg "skip: short question"; finish_skip
fi
# -> everything else TRIGGERS Claude

if [ "${#CLAUDE_SAFE_ARGS[@]}" -gt 0 ] && ! clf_safe_mode_supported; then
  clf_dbg "skip: claude lacks --safe-mode; update Claude Code or set CLAUDE_FUSION_SAFE_MODE=0 to allow local Claude customizations"
  finish_skip
fi

# Gate says complex -> mark this session+turn NOW, before consulting Claude, so the Stop hook still
# reviews the resulting diff even if this pre-edit analysis later times out or errors. The marker
# records the prompt-time HEAD so the Stop hook diffs against it even if Codex commits mid-turn
# (empty marker = non-git cwd; the Stop hook then falls back to HEAD).
if [ -n "$TURN_KEY" ] && clf_ensure_state_dir; then
  git -C "$CWD" rev-parse --verify HEAD >"$STATE_DIR/$TURN_KEY.complex" 2>/dev/null || true
fi

GITSTATUS="$(clf_filtered_status "$CWD" | head -c 4000)"
[ -n "$GITSTATUS" ] || GITSTATUS="(clean or not a git repository)"

EXPLICIT_DW=0
printf '%s' "$PROMPT" | grep -iqE '(\$dynamic-workflows\b|\bcodex-dw\b|\bdynamic[ -]workflows?\b|\bultracode\b)' && EXPLICIT_DW=1
WF_NOTE=""
if [ "$EXPLICIT_DW" -eq 1 ]; then
  # The user is already asking Codex to orchestrate. Withhold the fan-out tools as well as telling
  # Claude not to use them, so "no duplicate fan-out" is structural rather than an instruction.
  CLF_ALLOW_FANOUT=0
  WF_NOTE="
Treat this explicit Dynamic Workflows request as a workflow-design review. Critique coverage, role
independence, budgets, parallel barriers, authority boundaries, verification, stop gates, and the
terminal artifact. Do not launch a duplicate Claude workflow, Task fan-out, or nested codex-dw run;
the Codex/Dynamic Workflows coordinator owns execution."
elif clf_ultracode_enabled; then
  WF_NOTE="
When the task is non-trivial, run a thorough READ-ONLY multi-agent analysis (dynamic workflows /
parallel subagents) rather than a single quick pass. Do not edit files or run mutating commands."
  [ "$CUSTOM_CLAUDE_CONTEXT" -eq 1 ] || WF_NOTE="$WF_NOTE
Claude Fusion is running you in --safe-mode, so orchestrate with the built-in Task/Workflow tools
and do not rely on user/project Claude customizations, skills, plugins, saved workflows, memory,
MCP servers, or custom agents."
elif [ "$DEPTH" = "workflow" ]; then
  WF_NOTE="
When the task is non-trivial, run a thorough READ-ONLY analysis rather than a single quick pass."
  [ "$CUSTOM_CLAUDE_CONTEXT" -eq 1 ] || WF_NOTE="$WF_NOTE
Claude Fusion is running you in --safe-mode, so do not rely on user/project Claude customizations,
skills, plugins, workflows, memory, MCP servers, or custom agents."
fi

# Keep the keyword and the tools inseparable: withholding fan-out must never leave a prompt that
# still asks Claude to orchestrate one.
CLAUDE_PREFIX=""
clf_ultracode_enabled && [ "$CLF_ALLOW_FANOUT" = "1" ] && CLAUDE_PREFIX="ultracode: "
CLAUDE_PROMPT="${CLAUDE_PREFIX}You are Claude acting as an independent coding peer for the OpenAI Codex agent.

You are running automatically from a Codex UserPromptSubmit hook, in READ-ONLY mode.
Do not edit files. Do not run destructive or mutating commands.
Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.
Focus only on the user's coding task and the repository context.$WF_NOTE

User task:
$PROMPT

Repository:
$CWD

Quick repo state:
$GITSTATUS

Return a concise analysis with:
1. Problem understanding
2. Recommended approach
3. Files or modules likely involved
4. Edge cases
5. Tests/checks Codex should run
6. Security or data-loss risks
7. Assumptions
8. Concise implementation strategy
9. Anything Codex should be skeptical about

Also return at most three genuinely useful clarification questions. Label each question required
only when Codex cannot safely proceed without the user's choice; otherwise label it advisory.
Do not ask anything Codex can answer by inspecting the repository. Merge overlapping questions.
Each question needs a short header, a direct prompt, 2-3 mutually exclusive options with concise
tradeoffs, and the recommended option's exact label. An empty questions list is preferred when the
task is already actionable.

Keep the final response under 1200 words. Prefer conservative, minimal changes.
Do not produce a full patch unless asked."

clf_build_claude_args
CLF_CONTRACT_TYPE=analysis
CLF_JSON_SCHEMA='{"type":"object","additionalProperties":false,"required":["analysis","questions"],"properties":{"analysis":{"type":"string","minLength":1},"questions":{"type":"array","maxItems":3,"items":{"type":"object","additionalProperties":false,"required":["importance","header","prompt","options","recommendation"],"properties":{"importance":{"type":"string","enum":["required","advisory"]},"header":{"type":"string","minLength":1},"prompt":{"type":"string","minLength":1},"options":{"type":"array","minItems":2,"maxItems":3,"items":{"type":"object","additionalProperties":false,"required":["label","description"],"properties":{"label":{"type":"string","minLength":1},"description":{"type":"string","minLength":1}}}},"recommendation":{"type":"string","minLength":1}}}}}}'
CLF_KEEP_SESSION=0
CLF_RESUME_ID=""
CLF_RESUME_FILE=""
if clf_enabled "$CONTINUITY" && [ -n "$SESSION_ID" ] && clf_resume_supported; then
  CLF_KEEP_SESSION=1
  CLF_RESUME_FILE="$STATE_DIR/$SESSION_ID.claude-session"
  if [ -f "$CLF_RESUME_FILE" ]; then
    CLF_RESUME_ID="$(clf_sanitize_session_id "$(cat "$CLF_RESUME_FILE" 2>/dev/null)")"
    [ -n "$CLF_RESUME_ID" ] || rm -f "$CLF_RESUME_FILE" 2>/dev/null
  fi
fi
clf_dbg "running claude (model=$CLAUDE_MODEL, effort=$CLAUDE_EFFORT, depth=$DEPTH, ultracode=$ULTRACODE, tools=$TOOLSMODE, cwd=$CWD, words=$WORDS)"
clf_run_claude_contract
RC="$CLF_RC"
[ "$RC" -eq 0 ] || { clf_dbg "claude rc=$RC -> skip"; finish_skip; }
[ -n "$CLF_OUTPUT" ] || { clf_dbg "empty analysis -> skip"; finish_skip; }

QUESTIONS=""
if [ "$CLF_RESULT_MODE" = "structured" ]; then
  _ANALYSIS_FIELDS="$(printf '%s' "$CLF_OUTPUT" | "$PY" -c '
import base64, json, sys
d = json.load(sys.stdin)
analysis = d["analysis"]
blocks = []
for i, q in enumerate(d.get("questions", []), 1):
    lines = ["Question {} [{}]".format(i, q["importance"]), "Header: " + q["header"], "Prompt: " + q["prompt"], "Options:"]
    lines.extend("- {}: {}".format(o["label"], o["description"]) for o in q["options"])
    lines.append("Recommendation: " + q["recommendation"])
    blocks.append("\n".join(lines))
for value in (analysis, "\n\n".join(blocks)):
    print(base64.b64encode(value.encode()).decode())
' 2>/dev/null)"
  ANALYSIS="$(printf '%s' "$_ANALYSIS_FIELDS" | sed -n '1p' | base64 -d 2>/dev/null)"
  QUESTIONS="$(printf '%s' "$_ANALYSIS_FIELDS" | sed -n '2p' | base64 -d 2>/dev/null)"
  [ -n "$ANALYSIS" ] || { clf_dbg "structured analysis extraction failed -> skip"; finish_skip; }
  [ "$CLF_KEEP_SESSION" = "1" ] && clf_store_session_mapping "$CLF_RESUME_FILE" "$CLF_CLAUDE_SESSION_ID"
else
  ANALYSIS="$CLF_OUTPUT"
fi

PREAMBLE="AUTOMATIC CLAUDE FUSION CONTEXT:
Claude was automatically consulted (read-only) because this prompt matched the complex-coding gate.
Codex: before editing, form your own plan first, then reconcile it with Claude's analysis. Explicitly
note consensus, disagreements, Claude-only insights, and your final decision. You remain the final
judge and are not required to follow Claude.

If Claude proposed questions, first inspect the repository and remove anything answerable there.
Merge duplicates. You MUST ask every remaining required question before editing; you MAY promote an
advisory question when it materially reduces risk. Ask no more than three questions total. Never set
or pass an automatic-resolution timer: when using request_user_input, omit autoResolutionMs entirely.
If no interactive question tool is available, end the turn with the unresolved questions and wait for
the user's response. End your eventual reply with a short Claude Fusion synthesis summary."

# Emit Codex's hook output. Codex's hook contract mirrors Claude Code's: a UserPromptSubmit hook
# injects model-visible context via hookSpecificOutput.additionalContext.
# Shell-side cap BEFORE the env handoff: a single env string over ~128KiB fails execve (E2BIG) and
# the python emitter (with its own finer truncation) would never run at all.
mark_nogit_warned
CLAUDE_ANALYSIS="$(clf_truncate_bytes "$ANALYSIS" 100000 "claude analysis")" CLAUDE_QUESTIONS="$(clf_truncate_bytes "$QUESTIONS" 20000 "claude questions")" PREAMBLE="$PREAMBLE" MAX_CHARS="$MAX_CHARS" NONGIT_WARNING="$NONGIT_WARNING" "$PY" <<'PY'
import os, json
a = os.environ.get("CLAUDE_ANALYSIS", "")
q = os.environ.get("CLAUDE_QUESTIONS", "")
p = os.environ.get("PREAMBLE", "")
w = os.environ.get("NONGIT_WARNING", "")
try: m = int(os.environ.get("MAX_CHARS", "12000"))
except Exception: m = 12000
if len(a) > m:
    a = a[:m]
    cut = a.rfind("\n")
    if cut > 0:
        a = a[:cut]
    a += "\n\n[...Claude output truncated at " + str(m) + " chars...]"
ctx = p + "\n\n--- BEGIN CLAUDE ANALYSIS ---\n" + a + "\n--- END CLAUDE ANALYSIS ---"
if q:
    ctx += "\n\n--- BEGIN CLAUDE QUESTIONS ---\n" + q + "\n--- END CLAUDE QUESTIONS ---"
result = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}
if w:
    result["systemMessage"] = w
print(json.dumps(result))
PY
clf_dbg "injected $(printf '%s' "$ANALYSIS" | wc -c) chars"
exit 0
