---
name: claude-fusion-auto
description: "Synthesize Codex's own coding plan with the independent Claude analysis injected by Claude Fusion. Use whenever an 'AUTOMATIC CLAUDE FUSION CONTEXT', 'AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW', or 'AUTOMATIC CLAUDE FUSION - POST-DIFF REVIEW' block is present: complex coding tasks, architecture, debugging, refactors, migrations, security-sensitive changes, API design, and substantial code review. The user does not need to name this skill."
---

# Claude Fusion - automatic synthesis

When the conversation contains **AUTOMATIC CLAUDE FUSION CONTEXT** (injected by the
`claude-fusion-userprompt.sh` Codex hook), treat Claude as an independent peer reviewer whose
analysis you must reconcile with your own **before making any edits**.

## Process
1. Form your own plan first - do not anchor on Claude.
2. Compare against Claude's analysis and explicitly identify:
   - **Consensus** - where you and Claude agree.
   - **Disagreements** - where you differ, and which side you choose and why.
   - **Claude-only insights** - useful points Claude raised that you missed.
   - **Codex-only concerns** - risks/considerations Claude missed.
   - **Final decision** - your chosen approach.
3. You remain the final judge. Do **not** blindly obey Claude; reject its suggestions when you have
   a sound reason, and say so.
4. Prefer **minimal, testable** changes.
5. After editing, run the relevant **tests / lint / typecheck** for the project.

When the user's task explicitly invokes Dynamic Workflows (`$dynamic-workflows`, `codex-dw`,
`dynamic workflows`, or `ultracode`), treat Claude's analysis as a **workflow-design critique**.
Reconcile its advice about coverage, independent roles, budgets, barriers, authority, verification,
stop gates, and terminal artifacts with the Codex/Dynamic Workflows plan. Claude Fusion must not
launch duplicate fan-out or a nested `codex-dw` run; Dynamic Workflows remains the coordinator.

By default Claude is consulted as the latest Opus at `xhigh` effort in Ultra Code mode, so its
analysis and reviews come from a read-only multi-agent pass rather than a single agent.
`CLAUDE_FUSION_DEPTH=workflow` and `CLAUDE_FUSION_ULTRACODE=1` describe that Claude-side
consultation only. Neither invokes nor replaces the `codex-dw` runtime.

## Clarification questions
Claude may attach up to three structured questions to its analysis. Before asking the user:

1. Inspect the repository and remove questions Codex can answer from repo truth.
2. Merge duplicates and overlapping choices.
3. Ask every remaining `required` question before editing. An `advisory` question may be promoted
   when its answer materially reduces implementation risk.
4. Ask no more than three questions total.
5. Never configure automatic resolution. When `request_user_input` is available, omit
   `autoResolutionMs` entirely. If no interactive question tool is available, end the turn with the
   unresolved questions and wait for the user.

## Subagent review (adversarial verification)
A **SUBAGENT REVIEW** block means Claude ran as an independent adversarial verifier against that
subagent's result and refuted part of it. Correct the finding before allowing the subagent to finish,
or explicitly justify why it does not hold. Claude is instructed to report only defects it can point
to concretely, so treat a finding as a real refutation rather than a style opinion — but you remain
the final judge.

This verification also runs for `codex-dw` workers during multiagent orchestration, where Claude
verifies one worker's result per reserved slot and never fans out or reviews the whole run. It is a
per-worker gate, not a substitute for the main Stop-hook review of the final repository diff.

## Post-diff review (Stop hook)
If a **POST-DIFF REVIEW** from Claude appears, address every serious issue (correctness, security,
data-loss, concurrency, broken tests) before finalizing, or explicitly justify why each is not a
real problem. The reviewed final artifact may combine an active-checkout diff with committed
`codex-dw` base-to-head integration ranges. Treat those branches as user-controlled review targets:
Claude Fusion is advisory and must never merge them automatically.

## Required final summary
End your response with a short **Claude Fusion summary**:
- Whether Claude was consulted automatically.
- What Claude suggested (key points).
- What you accepted vs. rejected, and why.
- Files changed.
- Tests / checks run and their result.
- Remaining risks or follow-ups.
