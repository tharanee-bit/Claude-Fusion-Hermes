# Claude Fusion

**Automatic peer review for OpenAI Codex and Hermes Agent, powered by your local Claude Code.**

Claude Fusion makes [OpenAI Codex](https://github.com/openai/codex) or
[Hermes Agent](https://hermes-agent.nousresearch.com/docs) automatically consult
[Claude Code](https://claude.com/claude-code) as an independent second opinion on non-trivial coding
tasks - *without* a slash command, and *without* you typing anything. It is the mirror image of
[Codex Fusion](https://github.com/tharanee-bit/Codex-Fusion): there Claude is primary and Codex
advises; here **Codex or Hermes is primary and Claude advises**.

The original Codex integration uses Codex **hooks**:

- **Before** Codex plans or edits, a `UserPromptSubmit` hook runs Claude **read-only** over your
  repo and injects Claude's independent analysis into Codex's context. Codex then reconciles its own
  plan with Claude's (consensus / disagreements / Claude-only insights) before touching code.
- **After** Codex finishes a complex, file-changing task, a `Stop` hook runs Claude **read-only**
  over the resulting `git diff` and any matching final integration ranges published by
  [Codex Dynamic Workflows](https://github.com/tharanee-bit/Codex-Dynamic-Workflows). If Claude flags
  serious problems (correctness, security, data-loss, concurrency, broken tests), Codex is asked to
  address them before finalizing.
- **Whenever a subagent finishes** under that gated parent turn, a `SubagentStop` hook runs Claude as
  an **adversarial verifier** over its capped final message and the filtered parent-turn diff when
  present: Claude tries to refute the subagent's claims instead of summarizing them. Research-only
  agents are included, and the same verification runs for
  [Codex Dynamic Workflows](https://github.com/tharanee-bit/Codex-Dynamic-Workflows) workers during
  multiagent orchestration. Duplicate events are deduplicated and at most two unique subagents are
  verified per parent turn by default, under a separate budget from workflow workers; the main
  final-diff review still runs.

Codex stays the editor and the final judge. Claude only advises and reviews - it is always
**read-only** and never edits your files.

> **No credential games.** Claude Fusion shells out to the official `claude` CLI that you are
> already logged into. It does not use browser cookies, scraping, private APIs, or token extraction.

---

## How it works

```
                       you type a prompt to Codex
                                  |
                                  v
   UserPromptSubmit hook -- gate? --no--> (silent, Codex proceeds normally)
        | yes (non-trivial)
        v
   claude -p  (read-only, JSON schema) --> analysis + optional questions injected as context
        |
        v
   Codex synthesizes Codex + Claude, then edits
        |
        +--> SubagentStop adversarial verification (2 unique agents per turn, plus a
        |    separate codex-dw worker budget) --> PASS or continue that subagent
        |
        v
   Stop hook -- active diff or matching codex-dw artifact? --no--> (Codex finishes)
        | yes
        v
   claude -p  (read-only) over the combined final artifact
        |
        +-- verdict PASS         --> Codex finishes
        +-- verdict ISSUES_FOUND --> Codex must address them first
                                     (decision:block -> Codex continues with the review as a new prompt)
```

The three hooks coordinate through small session-plus-turn marker files in
`${TMPDIR:-/tmp}/claude-fusion-state-<uid>/`. The marker records the prompt-time `HEAD`, and the Stop
hook diffs the working tree against that commit, so work Codex commits mid-turn still gets reviewed.
On every ordinary Stop, the hook also scans `${CODEX_HOME:-$HOME/.codex}/dynamic-workflows/runs` for
`codex-dw.review-artifact/v1` records bound to the current Codex session. This scan is independent of
the prompt marker, so a detached workflow that finishes later is discovered on a subsequent turn.
The hook strictly validates the repository, commit objects, integration branch tip, and ancestor
range before including up to the four oldest unreviewed artifacts in the same Claude call as the
active diff.

Each definitive active review stores a diff hash, while workflow artifacts are receipted by stable
artifact ID plus head commit. Unchanged inputs are not re-reviewed, and repeated transient failures
on the same combined payload stop after `CLAUDE_FUSION_STOP_RETRY_LIMIT` attempts. Legacy
session-only markers remain readable during upgrades. Invalid or unavailable artifact metadata is
ignored fail-open; Claude Fusion never merges or modifies the integration branch.

Claude Code clients that support `--output-format json` and `--json-schema` use validated JSON
envelopes for both analysis and review. Non-success envelopes, `is_error`, malformed JSON, and
missing or invalid `structured_output` are failed attempts. Older clients keep the two-attempt text
path. After two malformed structured attempts, Claude Fusion makes one final fixed-fallback text
attempt. Timeouts are never retried.

## Hermes Agent port

The repository is also an installable, native Hermes plugin. It keeps Claude Code as the independent
reviewer rather than routing the review through Hermes's active model.

| Claude Fusion behavior | Hermes lifecycle integration |
|---|---|
| Analyze a complex prompt before work begins | `pre_llm_call` injects ephemeral context into the API-bound user message |
| Verify a completed subagent | `subagent_stop` reviews the child; `tool_execution` middleware attaches synchronous findings on Hermes's real agent-loop path, with `transform_tool_result` retained for registry-tool compatibility |
| Review the completed implementation | `pre_verify` reviews the prompt-time-HEAD-to-working-tree diff and returns one bounded continuation on serious findings |

Claude remains advisory: the plugin invokes the authenticated `claude` CLI with plan permission
mode, no session persistence, safe mode, and no Claude tools by default. Missing Claude,
timeouts, malformed output, Git failures, and unsupported safe mode all fail open so Hermes can
continue normally. Add `[no-claude]` to a prompt to skip its pre-prompt consultation.

### Hermes requirements

- **Hermes Agent 0.19.1 or newer** (`hermes`).
- **Claude Code** (`claude`) installed, signed in, and new enough to support `--safe-mode`.
- **Git** and Python 3.9 or newer.

The Hermes runtime is Python-based and does not require GNU `timeout` or `grep -z`, so it works on
macOS as well as Linux. Those GNU utilities remain requirements of the original Codex shell hooks.

### Install for Hermes

Install directly from GitHub with Hermes's native plugin manager:

```bash
hermes plugins install tharanee-bit/Claude-Fusion --enable
```

Or install from a checkout:

```bash
git clone https://github.com/tharanee-bit/Claude-Fusion.git
cd Claude-Fusion
./install-hermes.sh          # copy into the active HERMES_HOME
./doctor-hermes.sh           # read-only diagnostics
```

Use `./install-hermes.sh --link` while developing the plugin. Copy mode installs an explicit
allowlist of Hermes runtime, manifest, skill, and documentation files rather than archiving the
checkout, so local untracked files are not included. Add `--force` to replace an existing
installation. Restart running Hermes CLI, desktop, or gateway processes after installation so they
rediscover the plugin.

> **Review before enabling.** Hermes plugins execute in-process with your user permissions. The
> native GitHub installer and `install-hermes.sh` both enable the plugin; inspect `__init__.py` and
> `hermes-plugin/` first if this is not your checkout.

### Hermes configuration

Hermes-specific settings live in the active profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - claude-fusion
  entries:
    claude-fusion:
      settings:
        model: claude-opus-5
        effort: xhigh
        fallback_model: fable
        fallback_effort: xhigh
        depth: single            # workflow requires readonly tools
        ultracode: false         # requires workflow + readonly tools
        tools: none              # safest default; exact explicit opt-in: readonly
        safe_mode: true
        timeout: 600             # shared across retries; capped at 630
        pre_prompt: true
        final_review: true
        subagent_review: true
        subagent_review_limit: 2  # clamped to 1-8
        max_file_bytes: 409600
        exclude: []              # additional sensitive-path globs
```

The defaults above are used when `settings` is absent. `tools: none` gives Claude only the injected
task/status/diff payload. Only the exact string `readonly` opts into tools; unknown strings, empty or
null values, booleans, and other malformed values normalize to `none`. `tools: readonly` enables
unrestricted Claude Code `Read`, `Grep`, `Glob`,
selected read-oriented Bash commands, and optional Task/Workflow fan-out. Claude Code cannot sandbox
those tools to the filtered paths: when `readonly` is enabled, Claude can autonomously read denied
files such as `.env` and any other file available to the user's process despite prompt instructions.
Only enable it in a checkout whose complete readable contents are safe to disclose to Claude.

In the default tool-free mode, final-review artifacts use copy-aware Git provenance checks and omit
sensitive rename/copy sources and uncertain destinations. `max_file_bytes` applies to both the
working-tree object and its baseline Git blob; provenance or baseline-size lookup failures omit the
affected tracked content rather than forwarding an unsafe partial artifact.

The plugin bundles the optional namespaced skill `claude-fusion:claude-fusion`, which explains how a
Hermes agent should reconcile the injected peer review. Essential behavior is included directly in
the hook context and does not depend on loading the skill.

### Hermes compatibility limits

- `subagent_stop` is observer-only in Hermes. Claude cannot reopen the child; serious findings are
  surfaced to the parent agent instead. Background child findings are queued for the next parent
  turn because the original asynchronous `delegate_task` result has already returned.
- In Hermes 0.19.1, `pre_verify` is reached after Hermes-observed `write_file`/`patch` edits. Mutations
  performed only through terminal commands, MCP/plugin tools, or the Codex app-server path may not
  trigger the final gate.
- Hermes 0.19.1 routes `delegate_task` around `transform_tool_result`, so Claude Fusion also uses the
  supported `tool_execution` middleware seam. This preserves valid delegate JSON while attaching
  synchronous orchestrator-child findings before the parent model receives the result.
- Hermes's `tool_execution` middleware exposes the active delegate `tool_call_id`, which Claude
  Fusion retains through synchronous `subagent_stop` review so concurrent calls cannot consume one
  another's findings. Hook-only or asynchronous reviews without that identity are deferred when
  identical summaries make attribution ambiguous.
- There is no Hermes equivalent of Claude Fusion's detached Codex Dynamic Workflows artifact scan.

The rest of this README's installer, environment-variable configuration, hook trust instructions,
and Dynamic Workflows details apply to the **Codex integration**.

## Requirements

- **OpenAI Codex** CLI (`codex`) with hooks and plugins (verified on `codex-cli 0.142.0`).
- **Claude Code** (`claude`) installed and signed in.
- **python3**, **git**, **bash**, **GNU grep** (the gate uses `grep -z`), and the usual text
  utilities (`timeout`, `base64`, `sed`, `head`, `wc`, `tr`). `jq` is *not* required - JSON is
  handled with python3.

Tested on Linux / WSL2.

## Install

```bash
git clone https://github.com/tharanee-bit/Claude-Fusion.git
cd Claude-Fusion
./install.sh
```

The default installer adds or updates the `tharanee-bit/Claude-Fusion` marketplace through
`codex plugin marketplace`, installs `claude-fusion@claude-fusion`, verifies that Codex reports it
installed, and only then removes a legacy copy-and-merge installation. Any changed legacy
`hooks.json` is backed up first. It never edits Codex marketplace configuration directly and it
respects `CODEX_HOME`.

For development or compatibility:

```bash
./install.sh --local   # plugin from this checkout
./install.sh --legacy  # copy canonical hooks/skill and merge ~/.codex/hooks.json
```

Plugin registration and legacy registration must never be enabled at the same time. Each installer
path runs the bundled read-only doctor automatically.

### Required: trust the hooks

Codex will **not run a hook until you review and trust it**. After installing:

1. Start `codex`.
2. You'll see a banner that hooks need review.
3. Run `/hooks` and **trust all three** Claude Fusion hooks.

This is a one-time step (per hook). If you later change a hook script, Codex may ask you to review it
again.

### Manual install

Copy `plugins/claude-fusion/hooks/*.sh` into `~/.codex/hooks/` (and `chmod +x` the three event
scripts), copy `plugins/claude-fusion/skills/claude-fusion-auto/` into `~/.codex/skills/`, then
merge `hooks.snippet.json` into `~/.codex/hooks.json`. (Alternatively,
append `config-hooks.snippet.toml` to `~/.codex/config.toml` - both load paths work; don't use both.)
The snippets reference the hook scripts as `$HOME/.codex/hooks/...`; if your Codex build does not
expand `$HOME` in a hook command, substitute your absolute home path. (`install.sh` always writes
absolute paths in `--legacy` mode, so this caveat only applies to a manual merge.)

## Configuration

| Knob | Default | Effect |
|---|---|---|
| `[no-claude]` in your prompt | - | Skips Claude entirely for that prompt. |
| `CLAUDE_FUSION_MODEL` | `claude-opus-5` | Primary Claude model. Pinned to Opus 5 because the `opus` alias can lag new releases; overrides affect only the first attempt. |
| `CLAUDE_FUSION_EFFORT` | `xhigh` | Primary reasoning effort (`low` / `medium` / `high` / `xhigh` / `max`); overrides affect only the first attempt. |
| `CLAUDE_FUSION_DEPTH` | `workflow` | `workflow` = ask Claude for a deeper read-only consultation; `single` = one-shot analysis (faster, and disables Ultra Code). This is Claude consultation depth, not the `codex-dw` runtime. |
| `CLAUDE_FUSION_ULTRACODE` | `1` | `1` = Ultra Code: prefix the consultation with `ultracode:` and allow the built-in `Task` / `Workflow` / `ToolSearch` tools so Claude answers with a read-only multi-agent pass. `0` = one Claude agent. Requires `CLAUDE_FUSION_DEPTH=workflow`; a Claude Code build without those tools degrades to a single deep pass. |
| `CLAUDE_FUSION_TOOLS` | `readonly` | `readonly` = Claude can read/grep/glob + read-only git to explore the repo; `none` = `--tools ""` (analyze only the injected prompt + git status/diff). |
| `CLAUDE_FUSION_SAFE_MODE` | `1` | `1` = run Claude with `--safe-mode`, preventing `CLAUDE.md`, memory, skills, plugins, *saved* workflows, MCP servers, and custom agents from leaking into the consult. If your Claude Code build does not support `--safe-mode`, the hook skips rather than falling back to custom context. `0` = allow those local Claude customizations. Ultra Code does not need this: it uses built-in tools only. |
| `CLAUDE_FUSION_CONTINUITY` | `0` | `1` = persist and resume only the UserPromptSubmit Claude session. Invalid saved sessions are discarded and retried fresh. Stop and SubagentStop reviews always remain fresh. |
| `CLAUDE_FUSION_SUBAGENT_REVIEW` | `1` | `0` disables all SubagentStop verification (interactive turns and workflow workers) without disabling pre-prompt or final review. |
| `CLAUDE_FUSION_SUBAGENT_REVIEW_LIMIT` | `2` | Maximum unique subagent verifications atomically reserved per gated parent turn. |
| `CLAUDE_FUSION_WORKFLOW_VERIFY` | `1` | `1` = also verify `codex-dw` worker subagents during multiagent orchestration. `0` = restore the previous behaviour, where a `CODEX_DW_ACTIVE=1` environment suppresses every lifecycle hook. |
| `CLAUDE_FUSION_WORKFLOW_VERIFY_LIMIT` | `2` | Maximum unique worker verifications reserved per workflow run when `codex-dw` exports `CODEX_DW_RUN_ID`, otherwise per worker session - so a wide fan-out can exceed this in total. Lower it to `1` if orchestration cost matters more than coverage. |
| `CLAUDE_FUSION_TIMEOUT` | `600` (workflow) / `300` (single) | Shared whole-hook budget (seconds) across all Claude attempts; values above 630 are capped so cleanup completes before Codex's 660s hook timeout. |
| `CLAUDE_FUSION_STOP_RETRY_LIMIT` | `2` | Transient failed Stop-review attempts for an unchanged diff before skipping until the diff changes. |
| `CLAUDE_FUSION_EXCLUDE` | - | Extra space-separated globs to exclude from the status/diff sent to Claude, on top of the built-in sensitive-path denylist (globs containing spaces are unsupported). |
| `CLAUDE_FUSION_MAX_FILE_BYTES` | `409600` | Per-file size cap; changed files larger than this are dropped from the diff payload. |
| `CLAUDE_FUSION_DEBUG=1` | off | Logs gate/flow to `${TMPDIR:-/tmp}/claude-fusion-state-<uid>/debug.log`. |

> **Opus 5 at `xhigh` in Ultra Code by default, latest Fable as the fixed fallback, isolated.**
> Every automatic consultation asks `claude-opus-5` for an `xhigh`-effort *dynamic workflows*
> pass: Claude Fusion prefixes the prompt with `ultracode:` and allows the built-in
> `Task` / `Workflow` / `ToolSearch` tools, so analysis and review come from a read-only multi-agent
> run instead of one agent. Those tools are Claude Code built-ins, so Ultra Code does not require
> turning safe mode off; a build that does not expose them simply degrades to a single deep pass.
> On a fast failure, structured-capable clients retry with the latest Fable alias at `xhigh`,
> followed by one final Fable text attempt after malformed structured exhaustion. Older clients
> retain the original two-attempt Opus/Fable text path. The fallback is fixed even when the primary
> model or effort is overridden. All attempts share one bounded wall-clock budget; timeouts are not
> retried. Claude Code `--safe-mode` keeps the consult focused on the injected task and repository
> context rather than your personal Claude setup.
> **This costs latency and tokens:** a complex prompt waits for a multi-agent Opus pass (often
> several minutes) before Codex responds. To trade quality for speed, set
> `CLAUDE_FUSION_ULTRACODE=0` (single agent), `CLAUDE_FUSION_DEPTH=single` (one-shot, also disables
> Ultra Code), lower `CLAUDE_FUSION_EFFORT`, or use `[no-claude]` to skip a given prompt. To
> deliberately trade isolation for your local Claude setup — saved workflows, skills, memory, MCP
> servers — set `CLAUDE_FUSION_SAFE_MODE=0`. The hook-registration timeout in `hooks.json` (660s)
> sits above the maximum 630-second shared budget; if your Codex build caps hook timeouts lower and
> `workflow` analyses get killed, use `single` mode or a smaller `CLAUDE_FUSION_TIMEOUT`.

### The trigger gate

The `UserPromptSubmit` hook uses an **aggressive** gate: it consults Claude on most substantive
prompts and only skips when a prompt is clearly trivial or conversational - `[no-claude]`, fewer than
3 words, a typo/rename/format/lint/comment edit, a greeting, or a short pure question with no coding
verb. To make it conservative instead, edit the gate block in
`plugins/claude-fusion/hooks/claude-fusion-userprompt.sh`.

An explicit Dynamic Workflows prompt (`$dynamic-workflows`, `codex-dw`, `dynamic workflows`, or
`ultracode`) changes Claude's role to workflow-design critic. Claude checks coverage, independent
roles, budgets, barriers, authority, verification, stop gates, and terminal artifacts without
launching duplicate Claude fan-out or a nested `codex-dw` run. Dynamic Workflows remains the
orchestrator.

## Safety model

- Claude always runs `--permission-mode plan` (it cannot edit files) and, by default,
  `--no-session-persistence`, `--safe-mode`, and read-only tools. The only persistence exception is
  explicit `CLAUDE_FUSION_CONTINUITY=1`, and that applies only to pre-prompt analysis, never either
  review hook. Safe mode prevents Claude Code from loading `CLAUDE.md`, memory, skills, plugins,
  *saved* workflows, MCP servers, and custom agents into the automatic consult. Ultra Code is
  unaffected by that isolation because it only uses the built-in `Task` / `Workflow` / `ToolSearch`
  tools, and every agent those spawn inherits the same `--permission-mode plan` sandbox and tool
  allowlist. (The `--safe-mode` capability probe is cached per resolved `claude` binary, so it does
  not re-run `claude --help` on every prompt; updates invalidate the cache automatically.)
- **Sensitive paths never reach Claude in the harness payload.** The `git status` and diff the hooks
  embed are filtered at the source against a denylist of secret-bearing paths (env files, keys and
  certificates, `credentials*`/`secrets*`, shell history, `.netrc`/`.npmrc`/`.pypirc`, `auth.json`,
  SQLite databases, `.ssh`/`.aws`/`.gnupg` contents - extensible via `CLAUDE_FUSION_EXCLUDE`).
  Status lines for such paths are redacted; their diffs are dropped entirely, with a visible
  exclusion note. The prompt additionally tells Claude not to inspect credentials, but the guarantee
  is the source-level exclusion, not that instruction. Known limit: the filter is path-based, so if
  a turn renames a secret file to a non-denylisted name, the content appears under its new name.
  Committed `codex-dw` ranges use the same path policy and per-file size cap, reading endpoint sizes
  from Git objects so external integration branches do not need to be checked out.
- All three hooks **never block** Codex on the no-action/failure path - they always exit 0. If Claude is missing,
  not logged in, times out, or errors, the hook silently skips. A `timeout` wrapper bounds every
  `claude` call so a hook can never hang Codex.
- The `Stop` hook only ever asks Codex to continue (`decision: block`) when Claude explicitly returns
  `CLAUDE_REVIEW_VERDICT: ISSUES_FOUND`, and it is loop-safe via `stop_hook_active` - it reviews at
  most once per task.
- `SubagentStop` never reads `agent_transcript_path`. It sends only agent metadata, at most 12,000
  characters of `last_assistant_message`, and a size-capped diff that uses the same sensitive-path
  filtering. Unique-agent and slot directories are created atomically so duplicate events cannot
  consume a second slot. `ISSUES_FOUND` continues that subagent; failures remain fail-open. Claude is
  told to refute claims but to report only defects it can point to concretely, so an unverifiable
  claim is reported inside a `PASS` rather than blocking the subagent. Verifying a native Codex
  subagent uses Ultra Code like any other consultation; verifying a `codex-dw` worker is forced to a
  single Claude agent with the `Task` / `Workflow` / `ToolSearch` tools withheld, so fan-out is never
  nested inside fan-out. An explicit Dynamic Workflows prompt has those tools withheld for the same
  reason.
- Claude may propose at most three structured `required` or `advisory` questions. Injected context
  and the bundled skill require Codex to inspect repo truth first, merge duplicates, ask all
  remaining required questions, and omit `autoResolutionMs` entirely. Without an interactive
  question tool, Codex ends the turn with the questions and waits.
- **Loop-safe with Codex Fusion and Dynamic Workflows.** If you also run
  [Codex Fusion](https://github.com/tharanee-bit/Codex-Fusion) (Claude -> Codex), Claude Fusion
  exports `CLAUDE_FUSION_ACTIVE=1` and `CODEX_FUSION_ACTIVE=1` when it calls Claude and
  short-circuits at the top of every hook when either variable is set. That guard is absolute and no
  hook may ignore it. Dynamic Workflows marks SDK leaf workers with `CODEX_DW_ACTIVE=1`; in that
  environment `UserPromptSubmit` analysis and the final `Stop` review stay suppressed (the workflow
  coordinator owns those), and `SubagentStop` verification is the single exception - it runs against
  worker results under its own slot budget, never launches a Claude workflow or nested `codex-dw`
  run, and can be disabled with `CLAUDE_FUSION_WORKFLOW_VERIFY=0`. That budget is shared across the
  whole run when Dynamic Workflows exports `CODEX_DW_RUN_ID`; otherwise it is per worker session, so
  a wide fan-out can verify more than `CLAUDE_FUSION_WORKFLOW_VERIFY_LIMIT` times in total. Because Claude Fusion
  exports only the two Fusion-active flags when it shells out, a verification pass can never
  recursively trigger another one. (Independently, `codex exec` - which Codex Fusion uses - does not
  fire Codex lifecycle hooks.)

## Test it

```bash
# Triggers Claude (you'll see "AUTOMATIC CLAUDE FUSION CONTEXT" injected into Codex):
#   Refactor the auth middleware to eliminate the token-refresh race condition.
# Skips (trivial):       Fix the typo in the README heading.
# Skips (escape hatch):  Refactor the payment retry logic [no-claude]
```

You can also exercise the hook directly without Codex:

```bash
echo '{"prompt":"Refactor the auth module to fix a race condition","cwd":"'"$PWD"'","session_id":"s1","turn_id":"t1"}' \
  | CLAUDE_FUSION_DEBUG=1 CLAUDE_FUSION_DEPTH=single \
    plugins/claude-fusion/hooks/claude-fusion-userprompt.sh
```

(That prints the `{"hookSpecificOutput":{...}}` JSON Codex consumes. Use `CLAUDE_FUSION_DEPTH=single`
for a faster check.)

## Health check

```bash
./doctor.sh
```

The self-contained doctor is read-only. It checks plugin and legacy detection, duplicate
registration, source/cache parity when discoverable, executable bits, dependency versions, Claude
capabilities, read-only flags, timeout ordering, state-directory safety, and manifest shape. It
always reports `/hooks` trust as the remaining mandatory human gate and never changes files.

`AGENTS.snippet.md` remains only as a historical/manual reference. Do not merge shared `AGENTS.md`
or `CLAUDE.md` instructions for Claude Fusion: the injected context and bundled
`claude-fusion-auto` skill are authoritative, preserving `--safe-mode` isolation.

## Uninstall

```bash
./uninstall.sh
./uninstall.sh --purge-marketplace  # also remove the configured marketplace
```

Removes plugin and legacy installations safely. The marketplace remains configured by default for
easy reinstallation; `--purge-marketplace` removes it through `codex plugin marketplace remove`.
Other hooks and settings are untouched, and changed `hooks.json` files are backed up.

## Layout

```
.agents/plugins/marketplace.json                    # repo marketplace named claude-fusion
plugins/claude-fusion/.codex-plugin/plugin.json     # v0.1.4 plugin manifest
plugins/claude-fusion/hooks/hooks.json              # default-discovered three-hook registration
plugins/claude-fusion/hooks/*.sh                    # canonical runtime
plugins/claude-fusion/skills/claude-fusion-auto/    # authoritative synthesis/question skill
plugins/claude-fusion/scripts/doctor.sh              # canonical read-only doctor
hooks/*.sh                                           # checkout compatibility wrappers
hooks.snippet.json / config-hooks.snippet.toml       # legacy manual registration snippets
doctor.sh / install.sh / uninstall.sh                # root operational entry points
tests/test_hooks.py                                  # fake-Claude/fake-Codex unit and migration suite
```

For a manual legacy install, copy the canonical common helper alongside all three event scripts;
the event scripts source it from their own directory and fail open if it is missing.

## How the Codex hook contract was verified

Codex's hook system turned out to be a close port of Claude Code's, confirmed against
`codex-cli 0.142.0`:

- `~/.codex/hooks.json` (the wrapped
  `{"hooks":{"UserPromptSubmit":[...],"SubagentStop":[...],"Stop":[...]}}` form)
  auto-loads; inline `[hooks]` tables in `config.toml` also work.
- Hook **input** (stdin JSON) uses snake_case fields including `session_id`, `turn_id`, `cwd`,
  `prompt`, `stop_hook_active`, `agent_id`, `agent_type`, `agent_transcript_path`, and
  `last_assistant_message`.
- Hook **output** uses `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
  "additionalContext":"..."}}` to inject model context, top-level `systemMessage` for user-facing
  warnings, and `{"decision":"block","reason":"..."}` to
  intervene - identical to Claude Code. For the `Stop` event, `decision:block` makes Codex *continue*
  using `reason` as a new prompt.
- Codex gates hooks behind explicit `/hooks` review/trust before they run.
- Plugin hook discovery expands `${PLUGIN_ROOT}` from `plugins/claude-fusion/hooks/hooks.json`.
- Codex command hooks are synchronous; Claude Fusion does not request asynchronous execution.
- `codex exec` does not fire these lifecycle hooks; they fire in interactive `codex`
  sessions (which is exactly when you want a second opinion).

## License

[MIT](LICENSE)
