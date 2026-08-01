---
name: claude-fusion
description: Use when Claude Fusion is active in Hermes coding turns.
version: 0.1.4
author: Claude Fusion
license: MIT
metadata:
  hermes:
    tags: [hermes-agent, claude, code-review, verification]
    related_skills: []
---

# Claude Fusion for Hermes

## Overview

Claude Fusion gives Hermes a read-only second opinion from the user's authenticated Claude Code CLI. The plugin automatically consults Claude before complex coding work, verifies completed subagents, and reviews the final diff before Hermes finishes. Claude supplies evidence and criticism; Hermes remains responsible for every decision and edit.

## When to Use

Load this skill when:

- The conversation includes an `AUTOMATIC CLAUDE FUSION CONTEXT` block.
- A subagent result includes an `AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW` block.
- A verification continuation includes an `AUTOMATIC CLAUDE FUSION - POST-DIFF REVIEW` block.
- The user asks how Claude Fusion behaves inside Hermes.

Do not invoke Claude manually just because this skill is loaded. The plugin owns consultation timing and recursion guards.

## Working with Pre-Prompt Analysis

1. **Form an independent view.** Inspect the repository and derive the likely implementation before accepting Claude's proposal. Completion criterion: the relevant definitions, usages, tests, and manifests have been inspected directly.
2. **Reconcile.** Compare your view with the injected Claude analysis. Keep points supported by repository evidence; reject stale paths, invented APIs, or unnecessary scope. Completion criterion: every material disagreement is resolved by source or test evidence.
3. **Clarify sparingly.** Claude questions are advisory. Ask the user only when the missing choice materially changes implementation and cannot be recovered from the repository. Completion criterion: no question asks the user for information a tool can retrieve.
4. **Implement normally.** Claude is a peer, not an authority. Follow Hermes project instructions, tool discipline, and verification requirements.

## Working with Subagent Review

Subagent reviews are adversarial checks of a child summary, tool metadata, and the current filtered diff.

- Treat each finding as a hypothesis to verify.
- Inspect the relevant source or test output before changing code.
- Do not repeat a child claim merely because both the child and Claude assert it.
- If the finding is valid, fix the root cause and rerun the affected verification.
- If invalid, continue without ceremony; Hermes is the final judge.

Completion criterion: each serious finding is either fixed and verified or rejected with concrete evidence.

## Working with Final Diff Review

A `pre_verify` finding keeps the current Hermes turn alive once. It is not permission to loop indefinitely.

1. Map every finding to a changed path and exact behavior.
2. Reproduce or prove the issue where practical.
3. Apply the smallest root-cause correction.
4. Run the targeted test and the relevant broader suite.
5. Reinspect the final diff before responding.

Completion criterion: correctness, security, data-loss, race, and test findings are accounted for, and real command output supports the final claim.

## Controls

- Add `[no-claude]` anywhere in a user prompt to skip automatic pre-prompt consultation for that turn.
- The plugin fails open when Claude is unavailable, times out, returns malformed output, or lacks `--safe-mode`; Hermes continues without injected advice.
- Claude runs with plan permission mode, no session persistence, safe mode, and no tools by default.
- Sensitive paths and oversized files are excluded from injected status/diff payloads. The optional `tools: readonly` mode is not path-sandboxed and must only be enabled in a checkout whose readable contents are safe to disclose.
- Nested Hermes child turns do not trigger another pre-prompt consultation.

Configuration lives under `plugins.entries.claude-fusion.settings` in the active Hermes `config.yaml`. Supported keys include `enabled`, `model`, `effort`, `fallback_model`, `fallback_effort`, `depth`, `ultracode`, `tools`, `safe_mode`, `timeout`, `pre_prompt`, `final_review`, `subagent_review`, `subagent_review_limit`, `max_file_bytes`, and `exclude`.

## Common Pitfalls

1. **Blind deference.** Claude can hallucinate repository details. Resolve claims against code and test output.
2. **Duplicate consultation.** Do not shell out to Claude again for the same automatic analysis unless the user explicitly asks.
3. **Treating absence as approval.** Fail-open means no finding may indicate an unavailable verifier, not a clean review.
4. **Style churn.** Final review is scoped to serious defects. Ignore formatting and taste disagreements.
5. **Secret exposure.** Never work around path filtering or ask Claude to inspect credentials.
6. **Review loops.** The final gate is one-shot per consulted turn. Fix verified issues, then finish with actual evidence.

## Verification Checklist

- [ ] Claude advice was independently checked against repository evidence.
- [ ] Every serious subagent or final-diff finding was resolved.
- [ ] No sensitive content was copied into prompts or summaries.
- [ ] Relevant tests/builds were run after the final change.
- [ ] The final response distinguishes executed evidence from peer recommendations.
