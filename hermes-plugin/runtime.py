"""Hermes hook runtime for Claude Fusion.

The plugin shells out to the user's authenticated Claude Code CLI. Claude is
always invoked in plan mode and receives no tools by default; hook failures are
best-effort and never prevent Hermes from continuing.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import stat as stat_module
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)


def _default_cwd() -> Path:
    """Resolve Hermes's logical session cwd, with portable fallbacks."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return Path(resolve_agent_cwd())
    except Exception:
        terminal_cwd = os.environ.get("TERMINAL_CWD", "").strip()
        return Path(terminal_cwd).expanduser() if terminal_cwd else Path.cwd()

_DEFAULT_SENSITIVE_GLOBS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials*",
    "*credentials.json",
    "secrets*",
    "secret.*",
    "*history",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".htpasswd",
    "auth.json",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    ".ssh/*",
    ".aws/*",
    ".gnupg/*",
)

_ACTION_RE = re.compile(
    r"\b(implement|build|create|write|add|fix|debug|refactor|migrat|optimi[sz]|"
    r"design|review|change|update|integrat|deploy|test|rewrite|redesign|secure|"
    r"harden|delete|remove|configure|set ?up|wire|generate|scaffold|investigate|"
    r"diagnose|profile)\b",
    re.IGNORECASE,
)
_STRONG_ACTION_RE = re.compile(
    r"\b(implement|build|create|refactor|redesign|rewrite|architect|design|deploy|"
    r"scaffold|generate|debug|diagnose|profile|investigate|secure|harden|wire)\b|"
    r"\b(migrat|optimi[sz]|integrat)[a-z]*",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"^\s*(what|why|how|when|who|where|which|is|are|was|were|does|do|did|can|"
    r"could|should|would|will|explain|describe|summari[sz]e|tell me|define|"
    r"meaning of)\b",
    re.IGNORECASE,
)
_ACK_RE = re.compile(
    r"^\s*((thanks|thank you|thx|ok|okay|cool|nice|great|got it|hi|hello|hey|"
    r"yo|sup|yes|no|sure|nvm|never ?mind|lgtm)[\W_]*)+$",
    re.IGNORECASE | re.DOTALL,
)
_TRIVIAL_RE = re.compile(
    r"fix(ing)? (a |the )?typo|\btypo\b|\brewor[dk]|\bwording\b|formatting|"
    r"reindent|indentation|whitespace|\blint(ing)?\b|prettier|one[- ]?liner|"
    r"spelling|capitali[sz]",
    re.IGNORECASE,
)

_ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["analysis", "questions"],
    "properties": {
        "analysis": {"type": "string", "minLength": 1},
        "questions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["importance", "header", "prompt", "options", "recommendation"],
                "properties": {
                    "importance": {"type": "string", "enum": ["required", "advisory"]},
                    "header": {"type": "string", "minLength": 1},
                    "prompt": {"type": "string", "minLength": 1},
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "description"],
                            "properties": {
                                "label": {"type": "string", "minLength": 1},
                                "description": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "recommendation": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "ISSUES_FOUND"]},
        "findings": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
    },
}


class Settings:
    """Behavioral settings loaded from ``plugins.entries.claude-fusion.settings``."""

    def __init__(
        self,
        enabled: bool = True,
        model: str = "claude-opus-5",
        effort: str = "xhigh",
        fallback_model: str = "fable",
        fallback_effort: str = "xhigh",
        depth: str = "single",
        ultracode: bool = False,
        tools: str = "none",
        safe_mode: bool = True,
        timeout: int = 600,
        pre_prompt: bool = True,
        final_review: bool = True,
        subagent_review: bool = True,
        subagent_review_limit: int = 2,
        max_file_bytes: int = 409600,
        exclude: Optional[Sequence[str]] = None,
    ) -> None:
        # Safety controls keep their documented defaults unless YAML supplies
        # an exact boolean. Privileged modes require exact opt-in values.
        self.enabled = enabled if isinstance(enabled, bool) else True
        self.model = str(model or "claude-opus-5")
        self.effort = str(effort or "xhigh")
        self.fallback_model = str(fallback_model or "fable")
        self.fallback_effort = str(fallback_effort or "xhigh")
        self.depth = "workflow" if depth == "workflow" else "single"
        self.ultracode = ultracode is True and self.depth == "workflow"
        # Filesystem-capable tools are an explicit opt-in. Unknown strings,
        # empty values, nulls, booleans, and other malformed YAML values all
        # fail closed to the tool-free default.
        self.tools = "readonly" if tools == "readonly" else "none"
        self.safe_mode = safe_mode if isinstance(safe_mode, bool) else True
        try:
            parsed_timeout = int(timeout)
        except (TypeError, ValueError):
            parsed_timeout = 600 if self.depth == "workflow" else 300
        self.timeout = min(630, max(1, parsed_timeout))
        self.pre_prompt = pre_prompt if isinstance(pre_prompt, bool) else True
        self.final_review = final_review if isinstance(final_review, bool) else True
        self.subagent_review = subagent_review if isinstance(subagent_review, bool) else True
        try:
            self.subagent_review_limit = min(8, max(1, int(subagent_review_limit)))
        except (TypeError, ValueError):
            self.subagent_review_limit = 2
        try:
            self.max_file_bytes = max(1, int(max_file_bytes))
        except (TypeError, ValueError):
            self.max_file_bytes = 409600
        self.exclude = tuple(str(item) for item in (exclude or ()) if str(item).strip())

    @classmethod
    def from_mapping(cls, value: Any) -> "Settings":
        if not isinstance(value, dict):
            return cls()
        allowed = {
            "enabled",
            "model",
            "effort",
            "fallback_model",
            "fallback_effort",
            "depth",
            "ultracode",
            "tools",
            "safe_mode",
            "timeout",
            "pre_prompt",
            "final_review",
            "subagent_review",
            "subagent_review_limit",
            "max_file_bytes",
            "exclude",
        }
        return cls(**{key: item for key, item in value.items() if key in allowed})


class ClaudeFusionRuntime:
    _STATE_LIMIT = 512
    _STATE_TRIM_TARGET = 384

    def __init__(
        self,
        settings: Optional[Settings] = None,
        cwd_provider: Optional[Callable[[], Path]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.settings = settings or Settings()
        self._cwd_provider = cwd_provider or _default_cwd
        self._which = which or shutil.which
        self._lock = threading.RLock()
        self._turns: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._latest_turn: Dict[str, str] = {}
        self._pending_subagent_reviews: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
        self._capability_cache: Dict[Tuple[str, int, int], str] = {}
        self._active_delegate_call = threading.local()

    def pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, str]]:
        if not self.settings.enabled or not self.settings.pre_prompt:
            return None
        if kwargs.get("parent_session_id"):
            return None
        if os.environ.get("CLAUDE_FUSION_ACTIVE") == "1" or os.environ.get("CODEX_FUSION_ACTIVE") == "1":
            return None

        session_id = _safe_id(kwargs.get("session_id"))
        turn_id = _safe_id(kwargs.get("turn_id"))
        user_message = kwargs.get("user_message")
        # Every parent turn supersedes the previous final-review candidate,
        # even when the new prompt is trivial or explicitly opts out. Keep the
        # old turn record for late background subagent events, but never let a
        # later pre_verify consume stale baseline state.
        if session_id:
            with self._lock:
                self._latest_turn.pop(session_id, None)
        if not isinstance(user_message, str) or not self._should_consult(user_message):
            return self._take_pending_context(session_id)

        repo = self._repo_root()
        if repo is None:
            return self._take_pending_context(session_id)

        baseline = self._git(repo, "rev-parse", "--verify", "HEAD").strip()
        if session_id:
            key = (session_id, turn_id)
            with self._lock:
                self._turns[key] = {
                    "repo": repo,
                    "baseline": baseline,
                    "untracked_baseline": self._untracked_paths(repo),
                    "consulted": True,
                    "subagent_reviews": 0,
                    "reviewed_children": set(),
                }
                self._latest_turn[session_id] = turn_id
                self._prune_state_locked(preserve=key)

        status = self._filtered_status(repo)
        analysis_prompt = self._analysis_prompt(user_message, repo, status)
        parsed = self._run_contract(analysis_prompt, _ANALYSIS_SCHEMA, "analysis", allow_fanout=True, repo=repo)
        pending = self._take_pending_context(session_id)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("analysis"), str):
            return pending

        context = self._format_analysis_context(parsed)
        if pending and pending.get("context"):
            context = pending["context"] + "\n\n" + context
        return {"context": context}

    def pre_verify(self, **kwargs: Any) -> Optional[Dict[str, str]]:
        """Run one final diff review and keep Hermes going on serious findings."""
        if not self.settings.enabled or not self.settings.final_review:
            return None
        if not kwargs.get("coding") or int(kwargs.get("attempt") or 0) != 0:
            return None
        if os.environ.get("CLAUDE_FUSION_ACTIVE") == "1" or os.environ.get("CODEX_FUSION_ACTIVE") == "1":
            return None

        session_id = _safe_id(kwargs.get("session_id"))
        if not session_id:
            return None
        with self._lock:
            turn_id = self._latest_turn.get(session_id, "")
            state = self._turns.get((session_id, turn_id))
        if not state or not state.get("consulted"):
            return None

        repo = state.get("repo")
        if not isinstance(repo, Path):
            return None
        changed_paths = kwargs.get("changed_paths")
        diff = self._filtered_diff(
            repo,
            str(state.get("baseline") or "HEAD"),
            changed_paths if isinstance(changed_paths, list) else [],
            state.get("untracked_baseline"),
        )
        if not diff.strip():
            self._consume_turn(session_id, turn_id)
            return None

        review_prompt = self._review_prompt(repo, diff, changed_paths or [])
        parsed = self._run_contract(
            review_prompt,
            _REVIEW_SCHEMA,
            "review",
            allow_fanout=True,
            repo=repo,
        )
        self._consume_turn(session_id, turn_id)
        if not isinstance(parsed, dict) or parsed.get("verdict") != "ISSUES_FOUND":
            return None
        findings = parsed.get("findings")
        if not isinstance(findings, list) or not findings:
            return None
        rendered = "\n".join("- " + item for item in findings if isinstance(item, str))
        if not rendered:
            return None
        return {
            "action": "continue",
            "message": (
                "AUTOMATIC CLAUDE FUSION - POST-DIFF REVIEW:\n"
                "Claude independently reviewed the final integration artifact and flagged "
                "potentially serious issues. Address each correctness, security, data-loss, "
                "concurrency, or test failure below before finalizing, or explicitly justify "
                "why it does not apply. You remain the final judge.\n\n" + rendered
            )[:12000],
        }

    def _consume_turn(self, session_id: str, turn_id: str) -> None:
        with self._lock:
            state = self._turns.get((session_id, turn_id))
            if state is not None:
                state["final_reviewed"] = True
            if self._latest_turn.get(session_id) == turn_id:
                self._latest_turn.pop(session_id, None)

    def subagent_stop(self, **kwargs: Any) -> None:
        """Independently review completed child output and queue serious findings."""
        if not self.settings.enabled or not self.settings.subagent_review:
            return None
        if os.environ.get("CLAUDE_FUSION_ACTIVE") == "1" or os.environ.get("CODEX_FUSION_ACTIVE") == "1":
            return None
        if str(kwargs.get("child_status") or "").lower() not in ("completed", "success"):
            return None
        parent_session = _safe_id(kwargs.get("parent_session_id"))
        parent_turn = _safe_id(kwargs.get("parent_turn_id"))
        child_session = _safe_id(kwargs.get("child_session_id"))
        summary = kwargs.get("child_summary")
        if not parent_session or not parent_turn or not isinstance(summary, str) or not summary.strip():
            return None

        key = (parent_session, parent_turn)
        with self._lock:
            state = self._turns.get(key)
            if not state or not state.get("consulted"):
                return None
            reviewed_children = state.setdefault("reviewed_children", set())
            if child_session and child_session in reviewed_children:
                return None
            count = int(state.get("subagent_reviews") or 0)
            if count >= self.settings.subagent_review_limit:
                return None
            if child_session:
                reviewed_children.add(child_session)
            state["subagent_reviews"] = count + 1
            repo = state.get("repo")
            baseline = str(state.get("baseline") or "HEAD")
        if not isinstance(repo, Path):
            return None

        diff = self._filtered_diff(repo, baseline, [], state.get("untracked_baseline"))
        prompt = self._subagent_review_prompt(
            repo=repo,
            summary=summary,
            child_role=str(kwargs.get("child_role") or "unknown"),
            child_status=str(kwargs.get("child_status") or "unknown"),
            tool_history=kwargs.get("tool_call_history"),
            diff=diff,
        )
        parsed = self._run_contract(
            prompt,
            _REVIEW_SCHEMA,
            "review",
            allow_fanout=False,
            repo=repo,
        )
        if not isinstance(parsed, dict) or parsed.get("verdict") != "ISSUES_FOUND":
            return None
        findings = parsed.get("findings")
        if not isinstance(findings, list) or not findings:
            return None
        rendered = "\n".join("- " + item for item in findings if isinstance(item, str))
        if not rendered:
            return None
        review = (
            "AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW:\n"
            "Claude independently reviewed the completed Hermes subagent result and found "
            "potentially serious issues. Check these before relying on the child summary:\n\n" + rendered
        )[:12000]
        active_call = getattr(self._active_delegate_call, "identity", None)
        tool_call_id = ""
        if (
            isinstance(active_call, tuple)
            and len(active_call) == 3
            and active_call[0] == parent_session
            and active_call[1] == parent_turn
        ):
            tool_call_id = _safe_id(active_call[2])
        with self._lock:
            self._pending_subagent_reviews.setdefault(key, []).append(
                {
                    "review": review,
                    "summary_digest": _summary_digest(summary),
                    "tool_call_id": tool_call_id,
                }
            )
            self._prune_state_locked(preserve=key)
        return None

    def transform_tool_result(self, **kwargs: Any) -> Optional[str]:
        """Attach queued synchronous child reviews to delegate_task's returned result."""
        if kwargs.get("tool_name") != "delegate_task":
            return None
        session_id = _safe_id(kwargs.get("session_id"))
        turn_id = _safe_id(kwargs.get("turn_id"))
        if not session_id or not turn_id:
            return None
        key = (session_id, turn_id)
        result = kwargs.get("result")
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        results = payload.get("results")
        summary_counts = Counter(
            _summary_digest(item.get("summary"))
            for item in results if isinstance(item, dict) and isinstance(item.get("summary"), str)
        ) if isinstance(results, list) else Counter()
        if not summary_counts:
            return None
        tool_call_id = _safe_id(kwargs.get("tool_call_id"))
        if kwargs.get("_require_tool_identity") and not tool_call_id:
            # Agent-loop middleware should always receive a native call ID. If
            # it does not, defer the review rather than reverting to summary
            # correlation under concurrent dispatch.
            return None
        with self._lock:
            queued = self._pending_subagent_reviews.get(key, [])
            if tool_call_id:
                # Synchronous Hermes agent-loop dispatch is correlated by the
                # middleware's active tool call. Never let an unscoped async
                # review or another concurrent call attach by summary alone.
                eligible = [item for item in queued if item.get("tool_call_id") == tool_call_id]
            else:
                # Compatibility for hook-only callers that provide no native
                # tool identity. Scoped production reviews are ineligible.
                eligible = [item for item in queued if not item.get("tool_call_id")]
            queued_counts = Counter(item.get("summary_digest") for item in eligible)
            if any(queued_counts[digest] > 1 for digest in summary_counts):
                # Hook-only compatibility callers have no native tool identity.
                # Identical summaries there remain ambiguous, so defer them to
                # parent context instead of cross-attributing reviews.
                return None
            remaining_slots = dict(summary_counts)
            matched: List[Dict[str, str]] = []
            for item in eligible:
                digest = item.get("summary_digest", "")
                if remaining_slots.get(digest, 0) > 0:
                    matched.append(item)
                    remaining_slots[digest] -= 1
            if not matched:
                return None
            existing = payload.get("claude_fusion_reviews")
            merged = list(existing) if isinstance(existing, list) else []
            merged.extend(item["review"] for item in matched if item.get("review"))
            payload["claude_fusion_reviews"] = merged
            serialized = json.dumps(payload, ensure_ascii=False)
            remaining = [item for item in queued if item not in matched]
            if remaining:
                self._pending_subagent_reviews[key] = remaining
            else:
                self._pending_subagent_reviews.pop(key, None)
            return serialized

    def tool_execution_middleware(self, **kwargs: Any) -> Any:
        """Attach reviews through Hermes's agent-loop tool execution seam.

        Hermes 0.19.1 dispatches ``delegate_task`` directly from its agent
        runtime, so that tool does not pass through ``transform_tool_result``.
        Tool-execution middleware wraps both agent-loop and registry tools and
        can safely preserve the delegate JSON contract after downstream
        execution completes.
        """
        next_call = kwargs.get("next_call")
        if not callable(next_call):
            return None
        args = kwargs.get("args")
        effective_args = args if isinstance(args, dict) else {}
        if kwargs.get("tool_name") != "delegate_task":
            return next_call(effective_args)
        session_id = _safe_id(kwargs.get("session_id"))
        turn_id = _safe_id(kwargs.get("turn_id"))
        tool_call_id = _safe_id(kwargs.get("tool_call_id"))
        previous = getattr(self._active_delegate_call, "identity", None)
        self._active_delegate_call.identity = (session_id, turn_id, tool_call_id)
        try:
            result = next_call(effective_args)
        finally:
            if previous is None:
                try:
                    del self._active_delegate_call.identity
                except AttributeError:
                    pass
            else:
                self._active_delegate_call.identity = previous
        transformed = self.transform_tool_result(
            **{**kwargs, "result": result, "_require_tool_identity": True}
        )
        return transformed if transformed is not None else result

    def _prune_state_locked(self, preserve: Optional[Tuple[str, str]] = None) -> None:
        """Bound long-lived session and pending-review state under ``self._lock``."""
        if len(self._turns) > self._STATE_LIMIT:
            for stale_key in list(self._turns):
                if stale_key == preserve:
                    continue
                self._turns.pop(stale_key, None)
                session_id, turn_id = stale_key
                if self._latest_turn.get(session_id) == turn_id:
                    self._latest_turn.pop(session_id, None)
                self._pending_subagent_reviews.pop(stale_key, None)
                if len(self._turns) <= self._STATE_TRIM_TARGET:
                    break
        if len(self._latest_turn) > self._STATE_LIMIT:
            for session_id in list(self._latest_turn):
                turn_id = self._latest_turn.get(session_id, "")
                key = (session_id, turn_id)
                if key == preserve:
                    continue
                self._latest_turn.pop(session_id, None)
                self._turns.pop(key, None)
                self._pending_subagent_reviews.pop(key, None)
                if len(self._latest_turn) <= self._STATE_TRIM_TARGET:
                    break
        if len(self._pending_subagent_reviews) > self._STATE_LIMIT:
            for stale_key in list(self._pending_subagent_reviews):
                if stale_key == preserve:
                    continue
                self._pending_subagent_reviews.pop(stale_key, None)
                if len(self._pending_subagent_reviews) <= self._STATE_TRIM_TARGET:
                    break

    def _take_pending_context(self, session_id: str) -> Optional[Dict[str, str]]:
        if not session_id:
            return None
        with self._lock:
            reviews: List[str] = []
            for key in [key for key in self._pending_subagent_reviews if key[0] == session_id]:
                reviews.extend(
                    item["review"]
                    for item in self._pending_subagent_reviews.pop(key, [])
                    if item.get("review")
                )
        if not reviews:
            return None
        return {"context": "\n\n".join(reviews)}

    @staticmethod
    def _should_consult(prompt: str) -> bool:
        if "[no-claude]" in prompt or "AUTOMATIC CLAUDE FUSION" in prompt:
            return False
        words = prompt.split()
        if len(words) < 3 or _ACK_RE.match(prompt):
            return False
        first_line = prompt.splitlines()[0] if prompt.splitlines() else prompt
        strong = bool(_STRONG_ACTION_RE.search(prompt))
        if not strong and _TRIVIAL_RE.search(prompt):
            return False
        if len(words) < 16 and _QUESTION_RE.search(first_line) and not _ACTION_RE.search(prompt):
            return False
        return True

    def _repo_root(self) -> Optional[Path]:
        try:
            cwd = Path(self._cwd_provider()).expanduser().resolve()
        except Exception:
            return None
        output = self._git(cwd, "rev-parse", "--show-toplevel")
        if not output.strip():
            return None
        try:
            return Path(output.strip()).resolve()
        except OSError:
            return None

    @staticmethod
    def _git_result(repo: Path, *args: str) -> Tuple[bool, str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False, ""
        return completed.returncode == 0, completed.stdout if completed.returncode == 0 else ""

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        return ClaudeFusionRuntime._git_result(repo, *args)[1]

    def _sensitive_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        base = normalized.rsplit("/", 1)[-1]
        for pattern in _DEFAULT_SENSITIVE_GLOBS + self.settings.exclude:
            lowered = pattern.replace("\\", "/").lower()
            if fnmatch.fnmatch(base, lowered) or fnmatch.fnmatch(normalized, lowered) or fnmatch.fnmatch(normalized, "*/" + lowered):
                return True
        return False

    def _filtered_status(self, repo: Path) -> str:
        raw = self._git(repo, "status", "--short")
        lines: List[str] = []
        for line in raw.splitlines():
            path = line[3:] if len(line) > 3 else ""
            candidates = [part.strip('"') for part in path.split(" -> ")]
            if any(self._sensitive_path(item) for item in candidates):
                lines.append((line[:2] if len(line) >= 2 else "??") + " [redacted sensitive path]")
            else:
                lines.append(line)
        return "\n".join(lines)[:4000] or "(clean)"

    def _untracked_paths(self, repo: Path) -> set:
        raw = self._git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        return {item for item in raw.split("\0") if item}

    def _filtered_diff(
        self,
        repo: Path,
        baseline: str,
        changed_paths: Sequence[Any],
        untracked_baseline: Any = None,
    ) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", baseline or ""):
            baseline = "HEAD"

        # Name-status preserves both sides of staged renames/copies. Path-only
        # output loses the sensitive origin and can leak `.env -> public.txt`.
        status_ok, raw_status = self._git_result(
            repo,
            "diff",
            "--name-status",
            "-z",
            "-M1%",
            "-C1%",
            "--find-copies-harder",
            baseline,
            "--",
        )
        tokens = [item for item in raw_status.split("\0") if item]
        candidates: List[str] = []
        tracked_candidates = set()
        tracked_additions = set()
        tracked_modifications = set()
        baseline_paths = set()
        blocked = set()
        provenance_uncertain = False
        sensitive_tracked_change = False
        sensitive_tracked_deletion = False

        def add_candidate(path: str, tracked: bool = False) -> None:
            if path and path not in candidates:
                candidates.append(path)
            if tracked and path:
                tracked_candidates.add(path)

        index = 0
        while index < len(tokens):
            status = tokens[index]
            index += 1
            if not re.fullmatch(r"(?:[RC][0-9]+|[ACDMTUXB])", status):
                provenance_uncertain = True
                break
            if status.startswith(("R", "C")) and index + 1 < len(tokens):
                paths = [tokens[index], tokens[index + 1]]
                index += 2
                baseline_paths.add(paths[0])
            elif index < len(tokens):
                paths = [tokens[index]]
                index += 1
                if status.startswith(("D", "M")):
                    baseline_paths.add(paths[0])
            else:
                provenance_uncertain = True
                break
            for path in paths:
                add_candidate(path, tracked=True)
                if status.startswith("A"):
                    tracked_additions.add(path)
                elif status.startswith("M"):
                    tracked_modifications.add(path)
            if any(self._sensitive_path(path) for path in paths):
                blocked.update(paths)
                sensitive_tracked_change = True
                if status.startswith("D"):
                    sensitive_tracked_deletion = True

        # If name-status was unavailable, retain a conservative path-only
        # fallback rather than silently skipping the final review.
        if not tokens:
            fallback_ok, fallback_raw = self._git_result(
                repo, "diff", "--name-only", "-z", baseline, "--"
            )
            fallback_paths = [
                path
                for path in fallback_raw.split("\0")
                if path
            ]
            provenance_uncertain = not status_ok or not fallback_ok or bool(fallback_paths)
            for path in fallback_paths:
                if path:
                    add_candidate(path, tracked=True)
                    if self._sensitive_path(path):
                        blocked.add(path)
                        sensitive_tracked_change = True

        baseline_untracked = untracked_baseline if isinstance(untracked_baseline, set) else set()
        current_untracked = self._untracked_paths(repo)
        for path in sorted(current_untracked - baseline_untracked):
            add_candidate(path)

        for value in changed_paths:
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                path = Path(value)
                absolute = path.resolve() if path.is_absolute() else (repo / path).resolve()
                relative = absolute.relative_to(repo).as_posix()
            except (OSError, ValueError):
                continue
            add_candidate(relative)

        allowed: List[str] = []
        excluded = 0
        for relative in candidates:
            if relative in blocked or self._sensitive_path(relative):
                excluded += 1
                continue
            if provenance_uncertain and relative not in current_untracked:
                # A failed/empty provenance query cannot safely distinguish a
                # benign addition from a copy of a filtered sensitive source.
                excluded += 1
                continue
            if relative in (tracked_additions | tracked_modifications) and sensitive_tracked_deletion:
                # A low-similarity sensitive rename is reported as separate
                # D/A or D/M records. Git provides no certain provenance for
                # the destination, so quarantine it rather than risk disclosure.
                excluded += 1
                continue
            if relative in current_untracked and sensitive_tracked_change:
                # Git cannot pair an unstaged sensitive deletion with its
                # untracked destination. Quarantine all such destinations.
                excluded += 1
                continue
            path = repo / relative
            try:
                if path.exists() and path.stat().st_size > self.settings.max_file_bytes:
                    excluded += 1
                    continue
            except OSError:
                excluded += 1
                continue
            if relative in baseline_paths:
                baseline_size = self._git(
                    repo, "cat-file", "-s", "{}:{}".format(baseline, relative)
                ).strip()
                if not baseline_size.isdigit() or int(baseline_size) > self.settings.max_file_bytes:
                    excluded += 1
                    continue
            allowed.append(relative)
        if not allowed:
            return ""

        tracked: List[str] = []
        untracked: List[str] = []
        for relative in allowed:
            if relative in tracked_candidates:
                tracked.append(relative)
                continue
            if relative in current_untracked:
                untracked.append(relative)
                continue
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", relative],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            (tracked if result.returncode == 0 else untracked).append(relative)

        parts: List[str] = []
        if excluded:
            parts.append("### note: {} changed file(s) excluded (sensitive path or size cap)".format(excluded))
        if tracked:
            specs = [":(literal)" + item for item in tracked]
            tracked_diff = self._git(repo, "diff", baseline, "--", *specs)
            if tracked_diff:
                parts.append(tracked_diff.rstrip())
        for relative in untracked:
            path = repo / relative
            if path.is_symlink():
                parts.append(
                    "diff --git a/{0} b/{0}\nnew symbolic link (target and content omitted)".format(relative)
                )
                continue
            descriptor = None
            try:
                # Reject FIFOs/devices before open (which may block), then use
                # O_NOFOLLOW + O_NONBLOCK and fstat the opened descriptor to
                # close the lstat/open race without following replacements.
                before_open = path.lstat()
                if not stat_module.S_ISREG(before_open.st_mode):
                    continue
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                if hasattr(os, "O_NONBLOCK"):
                    flags |= os.O_NONBLOCK
                descriptor = os.open(path, flags)
                metadata = os.fstat(descriptor)
                if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_size > self.settings.max_file_bytes:
                    os.close(descriptor)
                    descriptor = None
                    continue
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = None
                    data = handle.read(self.settings.max_file_bytes + 1)
                if len(data) > self.settings.max_file_bytes:
                    continue
            except OSError:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                continue
            if b"\0" in data:
                parts.append("diff --git a/{0} b/{0}\nnew binary file (content omitted)".format(relative))
                continue
            text = data.decode("utf-8", "replace")
            body = "\n".join("+" + line for line in text.splitlines())
            parts.append(
                "diff --git a/{0} b/{0}\nnew file mode 100644\n--- /dev/null\n+++ b/{0}\n@@ -0,0 +1,{1} @@\n{2}".format(
                    relative,
                    len(text.splitlines()),
                    body,
                )
            )
        return "\n\n".join(parts)[:20000]

    def _analysis_prompt(self, task: str, repo: Path, status: str) -> str:
        workflow_note = ""
        prefix = ""
        if self.settings.tools == "readonly" and self.settings.ultracode:
            prefix = "ultracode: "
            workflow_note = (
                "\nRun a thorough READ-ONLY multi-agent analysis using built-in Task/Workflow "
                "tools. Do not edit files or run mutating commands."
            )
        elif self.settings.tools == "readonly" and self.settings.depth == "workflow":
            workflow_note = "\nRun a thorough READ-ONLY analysis rather than a quick pass."
        return f"""{prefix}You are Claude acting as an independent coding peer for the Hermes Agent.
You are running automatically from a Hermes pre_llm_call hook, in READ-ONLY mode.
Do not edit files. Do not run destructive or mutating commands.
Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.
Focus only on the user's coding task and repository context.{workflow_note}

User task:
{task}

Repository:
{repo}

Quick repo state:
{status}

Return a concise analysis with the problem understanding, recommended approach, likely files,
edge cases, tests, security/data-loss risks, assumptions, implementation strategy, and anything
Hermes should be skeptical about. Also return at most three genuinely useful clarification
questions. Do not ask anything Hermes can answer by inspecting the repository. Keep the analysis
under 1200 words and prefer conservative, minimal changes."""

    def _review_prompt(self, repo: Path, diff: str, changed_paths: Sequence[Any]) -> str:
        workflow_note = ""
        prefix = ""
        if self.settings.tools == "readonly" and self.settings.ultracode:
            prefix = "ultracode: "
            workflow_note = (
                "\nRun a READ-ONLY multi-agent adversarial review. Have independent reviewers "
                "try to refute candidate findings before reporting them."
            )
        elif self.settings.tools == "readonly" and self.settings.depth == "workflow":
            workflow_note = "\nRun a thorough READ-ONLY review rather than a quick pass."
        rendered_paths = []
        for item in changed_paths[:100]:
            text = str(item)
            path_candidates = [part.strip('"') for part in text.split(" -> ")]
            rendered_paths.append(
                "[redacted sensitive path]"
                if any(self._sensitive_path(path) for path in path_candidates)
                else text
            )
        changed = "\n".join(rendered_paths) or "(unknown)"
        return f"""{prefix}You are Claude acting as an independent code reviewer for Hermes Agent.
You are running automatically from a Hermes pre_verify hook, in READ-ONLY mode.
Do not edit files or run destructive or mutating commands. Do not inspect credentials, tokens,
.env files, keychains, shell history, or auth files.{workflow_note}

Review the final integration artifact below for SERIOUS problems only: correctness bugs, security
vulnerabilities, data-loss risks, concurrency/race issues, and broken or missing tests. Ignore pure
style and formatting nits. Report ISSUES_FOUND only for concrete defects.

Repository:
{repo}

Changed paths reported by Hermes:
{changed}

Final integration artifact:
{diff}"""

    @staticmethod
    def _subagent_review_prompt(
        repo: Path,
        summary: str,
        child_role: str,
        child_status: str,
        tool_history: Any,
        diff: str,
    ) -> str:
        try:
            history = json.dumps(tool_history, ensure_ascii=False, default=str)[:8000]
        except (TypeError, ValueError):
            history = str(tool_history)[:8000]
        return f"""You are Claude acting as an independent adversarial verifier for Hermes Agent.
You are running automatically from a Hermes subagent_stop hook, in READ-ONLY mode.
Do not edit files or run destructive or mutating commands. Do not launch subagents.
Do not inspect credentials, tokens, .env files, keychains, shell history, or auth files.

Review the completed child agent result for SERIOUS problems only: unsupported claims, correctness
bugs, security or data-loss risks, race conditions, and missing verification. Cross-check the
summary against the tool metadata and current diff. Ignore style nits. Report ISSUES_FOUND only
for concrete defects.

Repository: {repo}
Child role: {child_role}
Child status: {child_status}

Child summary:
{summary[:12000]}

Tool metadata:
{history}

Current filtered diff:
{diff[:12000] if diff else "(no diff available)"}"""

    @staticmethod
    def _format_analysis_context(parsed: Dict[str, Any]) -> str:
        parts = [
            "AUTOMATIC CLAUDE FUSION CONTEXT:",
            "Claude was automatically consulted read-only because this prompt matched the complex-coding gate.",
            "Hermes: form your own plan, then reconcile it with Claude's analysis. You remain the final judge.",
            "",
            "--- BEGIN CLAUDE ANALYSIS ---",
            str(parsed.get("analysis", "")).strip(),
            "--- END CLAUDE ANALYSIS ---",
        ]
        questions = parsed.get("questions")
        if isinstance(questions, list) and questions:
            parts.extend(["", "--- BEGIN CLAUDE QUESTIONS ---"])
            for index, question in enumerate(questions[:3], 1):
                if not isinstance(question, dict):
                    continue
                parts.append("Question {} [{}]".format(index, question.get("importance", "advisory")))
                parts.append("Header: {}".format(question.get("header", "")))
                parts.append("Prompt: {}".format(question.get("prompt", "")))
                options = question.get("options")
                if isinstance(options, list):
                    for option in options[:3]:
                        if isinstance(option, dict):
                            parts.append("- {}: {}".format(option.get("label", ""), option.get("description", "")))
                parts.append("Recommendation: {}".format(question.get("recommendation", "")))
            parts.append("--- END CLAUDE QUESTIONS ---")
        # Keep this below Hermes's default 10k per-hook spill threshold so the
        # peer analysis remains directly visible to the model.
        return "\n".join(parts)[:8500]

    def _claude_binary(self) -> Optional[str]:
        binary = self._which("claude")
        if not binary:
            return None
        try:
            path = str(Path(binary).resolve())
            return path if os.access(path, os.X_OK) else None
        except OSError:
            return None

    def _capabilities(self, binary: str, timeout: float) -> str:
        try:
            stat_result = os.stat(binary)
            key = (binary, stat_result.st_mtime_ns, stat_result.st_size)
        except OSError:
            key = (binary, 0, 0)
        with self._lock:
            cached = self._capability_cache.get(key)
        if cached is not None:
            return cached
        process = None
        try:
            process = subprocess.Popen(
                [binary, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name == "posix",
            )
            stdout, stderr = process.communicate(timeout=max(0.05, timeout))
            help_text = (stdout or "") + (stderr or "") if process.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            try:
                if process is not None:
                    if os.name == "posix":
                        os.killpg(process.pid, 9)
                    else:
                        process.kill()
                    process.communicate()
            except (OSError, subprocess.SubprocessError):
                pass
            help_text = ""
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        if help_text:
            with self._lock:
                self._capability_cache = {key: help_text}
        return help_text

    def _claude_args(
        self,
        binary: str,
        schema: Dict[str, Any],
        model: str,
        effort: str,
        allow_fanout: bool,
        structured: bool,
    ) -> List[str]:
        args = [binary, "-p"]
        if self.settings.safe_mode:
            args.append("--safe-mode")
        args.extend(["--permission-mode", "plan", "--no-session-persistence"])
        if structured:
            args.extend(["--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":"))])
        else:
            args.extend(["--output-format", "text"])
        if model:
            args.extend(["--model", model])
        if effort:
            args.extend(["--effort", effort])
        if self.settings.tools == "none":
            args.extend(["--tools", ""])
        else:
            allowed = "Read Grep Glob Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(ls:*) Bash(cat:*)"
            if self.settings.ultracode and allow_fanout:
                allowed += " Task Workflow ToolSearch"
            args.extend(["--allowedTools", allowed])
        return args

    def _run_once(
        self,
        args: Sequence[str],
        prompt: str,
        repo: Path,
        timeout: float,
    ) -> Optional[str]:
        env = os.environ.copy()
        env["CLAUDE_FUSION_ACTIVE"] = "1"
        env["CODEX_FUSION_ACTIVE"] = "1"
        try:
            process = subprocess.Popen(
                list(args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(repo),
                env=env,
                start_new_session=os.name == "posix",
            )
            stdout, _stderr = process.communicate(prompt, timeout=max(0.05, timeout))
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, 9)
                else:
                    process.kill()
                process.communicate()
            except (OSError, subprocess.SubprocessError):
                pass
            raise
        except (OSError, subprocess.SubprocessError):
            return None
        if process.returncode != 0 or not stdout.strip():
            return None
        return stdout.strip()

    @staticmethod
    def _parse_envelope(raw: str, contract: str) -> Optional[Dict[str, Any]]:
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get("type") != "result" or envelope.get("subtype") != "success" or envelope.get("is_error") is not False:
            return None
        output = envelope.get("structured_output")
        if not isinstance(output, dict):
            return None
        if contract == "analysis":
            if not isinstance(output.get("analysis"), str) or not output["analysis"].strip():
                return None
            questions = output.get("questions", [])
            if not isinstance(questions, list) or len(questions) > 3:
                return None
        elif contract == "review":
            if output.get("verdict") not in ("PASS", "ISSUES_FOUND"):
                return None
            findings = output.get("findings", [])
            if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
                return None
            if output["verdict"] == "ISSUES_FOUND" and not findings:
                return None
        else:
            return None
        return output

    @staticmethod
    def _text_review_prompt(prompt: str) -> str:
        return prompt + (
            "\n\nTEXT FALLBACK FORMAT: Put exactly one of "
            "CLAUDE_REVIEW_VERDICT: PASS or "
            "CLAUDE_REVIEW_VERDICT: ISSUES_FOUND on the first non-empty line. "
            "For ISSUES_FOUND, follow it with one concrete '- ' finding per line."
        )

    @staticmethod
    def _parse_text_review(raw: str) -> Optional[Dict[str, Any]]:
        lines = [line.rstrip() for line in raw.splitlines()]
        first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
        if first_index is None:
            return None
        first = lines[first_index].strip()
        if re.fullmatch(r"CLAUDE_REVIEW_VERDICT:\s*PASS", first, re.IGNORECASE):
            return {"verdict": "PASS", "findings": []}
        if not re.fullmatch(r"CLAUDE_REVIEW_VERDICT:\s*ISSUES_FOUND", first, re.IGNORECASE):
            return None
        findings = [
            line[2:].strip()
            for line in lines[first_index + 1 :]
            if line.startswith("- ") and line[2:].strip()
        ]
        return {"verdict": "ISSUES_FOUND", "findings": findings} if findings else None

    def _run_contract(
        self,
        prompt: str,
        schema: Dict[str, Any],
        contract: str,
        allow_fanout: bool,
        repo: Path,
    ) -> Optional[Dict[str, Any]]:
        binary = self._claude_binary()
        if not binary:
            return None
        deadline = time.monotonic() + self.settings.timeout
        help_text = self._capabilities(binary, deadline - time.monotonic())
        if self.settings.safe_mode and "--safe-mode" not in help_text:
            logger.warning("Claude Fusion skipped: Claude Code does not support --safe-mode")
            return None
        structured = "--output-format" in help_text and "--json-schema" in help_text
        invocation_prompt = prompt if structured or contract == "analysis" else self._text_review_prompt(prompt)
        attempts = [
            (self.settings.model, self.settings.effort),
            (self.settings.fallback_model, self.settings.fallback_effort),
        ]
        for model, effort in attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            args = self._claude_args(binary, schema, model, effort, allow_fanout, structured)
            try:
                raw = self._run_once(args, invocation_prompt, repo, remaining)
            except subprocess.TimeoutExpired:
                # A timeout consumes the whole hook budget. Do not make Hermes
                # wait through a second full timeout on the fallback model.
                break
            if raw is None:
                continue
            if structured:
                parsed = self._parse_envelope(raw, contract)
                if parsed is not None:
                    return parsed
            elif contract == "analysis":
                return {"analysis": raw, "questions": []}
            else:
                parsed = self._parse_text_review(raw)
                if parsed is not None:
                    return parsed
        if structured:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                fallback_prompt = self._text_review_prompt(prompt) if contract == "review" else prompt
                args = self._claude_args(
                    binary,
                    schema,
                    self.settings.fallback_model,
                    self.settings.fallback_effort,
                    allow_fanout,
                    False,
                )
                try:
                    raw = self._run_once(args, fallback_prompt, repo, remaining)
                except subprocess.TimeoutExpired:
                    raw = None
                if raw:
                    if contract == "analysis":
                        return {"analysis": raw, "questions": []}
                    parsed = self._parse_text_review(raw)
                    if parsed is not None:
                        return parsed
        return None


def _summary_digest(summary: Any) -> str:
    text = str(summary or "")
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest() if text else ""


def _safe_id(value: Any) -> str:
    text = str(value or "")
    # Hermes turn/session IDs are in-memory correlation keys, not paths. Native
    # turn IDs contain colons (for example ``session:task:uuid``).
    return text if re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", text) else ""
