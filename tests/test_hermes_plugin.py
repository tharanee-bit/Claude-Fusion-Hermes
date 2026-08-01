import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT
RUNTIME_FILE = ROOT / "hermes-plugin" / "runtime.py"
GATED_PROMPT = "Refactor the auth module to fix a race condition"


def resolve_hermes_python():
    executable = Path(shutil.which("hermes") or "")
    for _ in range(3):
        lines = executable.read_text(encoding="utf-8").splitlines()
        shebang = shlex.split(lines[0][2:]) if lines and lines[0].startswith("#!") else []
        if len(shebang) >= 2 and Path(shebang[0]).name == "env" and shebang[1] in ("bash", "sh", "zsh"):
            exec_line = next((line for line in lines[1:] if line.strip().startswith("exec ")), "")
            parts = shlex.split(exec_line)
            if len(parts) < 2:
                break
            executable = Path(parts[1])
            continue
        if len(shebang) >= 2 and Path(shebang[0]).name == "env":
            return shutil.which(shebang[1]) or shebang[1]
        if shebang:
            return shebang[0]
        break
    raise RuntimeError("Unable to resolve the Python interpreter used by Hermes")


def load_runtime_module():
    spec = importlib.util.spec_from_file_location("claude_fusion_hermes_runtime", RUNTIME_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {RUNTIME_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin_module():
    name = "claude_fusion_hermes_plugin"
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {PLUGIN_ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


class HermesPluginTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.bin = self.base / "bin"
        self.log = self.base / "claude-log.jsonl"
        self.repo.mkdir()
        self.bin.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.claude = self.bin / "claude"
        self._write_fake_claude()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_fake_claude(self):
        self.claude.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                if "--help" in sys.argv:
                    print("--safe-mode --output-format --json-schema --no-session-persistence --effort")
                    raise SystemExit(0)

                prompt = sys.stdin.read()
                with Path({str(self.log)!r}).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({{"argv": sys.argv[1:], "prompt": prompt}}) + "\\n")
                if "adversarial verifier" in prompt:
                    structured_output = {{
                        "verdict": "ISSUES_FOUND",
                        "findings": ["The subagent claims tests passed but its metadata shows no test command."],
                    }}
                elif "final integration artifact" in prompt:
                    structured_output = {{
                        "verdict": "ISSUES_FOUND",
                        "findings": ["app.py:1 changes a shared value without updating its test"],
                    }}
                else:
                    structured_output = {{
                        "analysis": "Inspect the token refresh lock before editing.",
                        "questions": [],
                    }}
                print(json.dumps({{
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": structured_output,
                }}))
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)

    def test_pre_llm_call_injects_read_only_claude_analysis(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )

        result = runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            conversation_history=[],
            is_first_turn=True,
            model="openai/gpt-5",
            platform="cli",
            parent_session_id="",
        )

        self.assertIsInstance(result, dict)
        context = result["context"]
        self.assertIn("AUTOMATIC CLAUDE FUSION CONTEXT", context)
        self.assertIn("Inspect the token refresh lock before editing.", context)
        call = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("--permission-mode", call["argv"])
        self.assertIn("plan", call["argv"])
        self.assertIn("--safe-mode", call["argv"])
        self.assertIn("--tools", call["argv"])
        self.assertNotIn("--allowedTools", call["argv"])
        self.assertIn("READ-ONLY", call["prompt"])
        self.assertIn(GATED_PROMPT, call["prompt"])

    def test_missing_claude_fails_open_without_injecting_context(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=1),
            cwd_provider=lambda: self.repo,
            which=lambda _name: None,
        )

        result = runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )

        self.assertIsNone(result)
        self.assertFalse(self.log.exists())

    def test_timeout_is_shared_across_primary_and_fallback_attempts(self):
        self.claude.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import sys
                import time
                from pathlib import Path

                if "--help" in sys.argv:
                    print("--safe-mode --output-format --json-schema --no-session-persistence --effort")
                    raise SystemExit(0)
                with Path({str(self.log)!r}).open("a", encoding="utf-8") as handle:
                    handle.write("attempt\\n")
                time.sleep(5)
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=1),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )

        started = time.monotonic()
        result = runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 1.8)
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(), ["attempt"])

    def test_capability_probe_is_inside_the_shared_timeout(self):
        self.claude.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import time
                time.sleep(5)
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=1),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )

        started = time.monotonic()
        result = runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )

        self.assertIsNone(result)
        self.assertLess(time.monotonic() - started, 1.8)

    @unittest.skipIf(os.name != "posix", "process-group timeout behavior is POSIX-specific")
    def test_timeout_kills_claude_descendant_process_group(self):
        descendant_marker = self.base / "descendant-survived"
        self.claude.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import subprocess
                import sys
                import time

                if "--help" in sys.argv:
                    print("--safe-mode --output-format --json-schema --no-session-persistence --effort")
                    raise SystemExit(0)
                subprocess.Popen([
                    sys.executable,
                    "-c",
                    "import time; from pathlib import Path; time.sleep(2); "
                    "Path({str(descendant_marker)!r}).write_text('survived')",
                ])
                time.sleep(5)
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=1),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )

        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        time.sleep(1.5)

        self.assertFalse(descendant_marker.exists())

    def test_malformed_structured_attempts_fall_back_to_fixed_text_model(self):
        self.claude.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                if "--help" in sys.argv:
                    print("--safe-mode --output-format --json-schema --no-session-persistence --effort")
                    raise SystemExit(0)
                with Path({str(self.log)!r}).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(sys.argv[1:]) + "\\n")
                if "--json-schema" in sys.argv:
                    print("{{}}")
                else:
                    print("analysis from fixed text fallback")
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )

        result = runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )

        self.assertIn("analysis from fixed text fallback", result["context"])
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertIn("fable", calls[-1])
        self.assertNotIn("--json-schema", calls[-1])

    def test_legacy_review_prompt_requests_and_strictly_parses_verdict_marker(self):
        self.claude.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                if "--help" in sys.argv:
                    print("--safe-mode --output-format --no-session-persistence --effort")
                    raise SystemExit(0)
                prompt = sys.stdin.read()
                with Path({str(self.log)!r}).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({{"prompt": prompt}}) + "\\n")
                if "coding peer" in prompt:
                    print("Inspect the implementation carefully.")
                else:
                    print("There may be a serious race condition in this change.")
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")

        result = runtime.pre_verify(
            session_id="session-1", coding=True, attempt=0, changed_paths=["app.py"]
        )

        self.assertIsNone(result, "unparseable review prose must fail open, not be classified as PASS")
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertIn("CLAUDE_REVIEW_VERDICT: PASS", calls[-1]["prompt"])

    def test_skipped_parent_turn_clears_previous_final_review_state(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-2", user_message="thanks [no-claude]", parent_session_id=""
        )

        result = runtime.pre_verify(
            session_id="session-1", coding=True, attempt=0, changed_paths=["app.py"]
        )

        self.assertIsNone(result)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)

    def test_pre_verify_continues_when_claude_finds_a_serious_diff_issue(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")

        result = runtime.pre_verify(
            session_id="session-1",
            platform="cli",
            model="openai/gpt-5",
            coding=True,
            attempt=0,
            final_response="Implemented the change.",
            changed_paths=["app.py"],
        )

        self.assertEqual(result["action"], "continue")
        self.assertIn("AUTOMATIC CLAUDE FUSION - POST-DIFF REVIEW", result["message"])
        self.assertIn("app.py:1", result["message"])
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        review_call = calls[-1]
        self.assertIn("value = 2", review_call["prompt"])
        self.assertIn("--permission-mode", review_call["argv"])

        self.assertIsNone(runtime.pre_verify(
            session_id="session-1",
            coding=True,
            attempt=1,
            changed_paths=["app.py"],
        ))

    def test_final_review_includes_untracked_code_but_excludes_sensitive_files(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )
        (self.repo / "new_module.py").write_text("new_value = 3\n", encoding="utf-8")
        (self.repo / ".env").write_text("SECRET_VALUE=do-not-send\n", encoding="utf-8")

        runtime.pre_verify(
            session_id="session-1",
            coding=True,
            attempt=0,
            changed_paths=["new_module.py", ".env"],
        )

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        review_prompt = calls[-1]["prompt"]
        self.assertIn("new_module.py", review_prompt)
        self.assertIn("new_value = 3", review_prompt)
        self.assertNotIn("do-not-send", review_prompt)
        self.assertNotIn("SECRET_VALUE", review_prompt)

    @unittest.skipIf(os.name != "posix", "symlink behavior is POSIX-specific")
    def test_untracked_symlink_content_is_not_followed_into_review_payload(self):
        outside = self.base / "outside-secret.txt"
        outside.write_text("OUTSIDE_SECRET=never-send\n", encoding="utf-8")
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        (self.repo / "linked.txt").symlink_to(outside)

        runtime.pre_verify(
            session_id="session-1", coding=True, attempt=0, changed_paths=["linked.txt"]
        )

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        review_prompt = calls[-1]["prompt"]
        self.assertIn("linked.txt", review_prompt)
        self.assertNotIn("never-send", review_prompt)
        self.assertNotIn("OUTSIDE_SECRET", review_prompt)

    def test_sensitive_staged_rename_is_excluded(self):
        (self.repo / ".env").write_text("SECRET_VALUE=rename-secret\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", ".env"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add fixture"], check=True)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )
        (self.repo / ".env").rename(self.repo / "public.txt")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)

        runtime.pre_verify(
            session_id="session-1",
            coding=True,
            attempt=0,
            changed_paths=["public.txt"],
        )

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        if len(calls) > 1:
            review_prompt = calls[-1]["prompt"]
            self.assertNotIn("rename-secret", review_prompt)
            self.assertNotIn("SECRET_VALUE", review_prompt)

    def test_subagent_findings_are_appended_to_delegate_tool_result(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            user_message=GATED_PROMPT,
            parent_session_id="",
        )

        runtime.subagent_stop(
            parent_session_id="session-1",
            parent_turn_id="turn-1",
            child_session_id="child-1",
            child_role="leaf",
            child_summary="Implemented the fix and all tests pass.",
            child_status="completed",
            tool_call_history=[{"tool_name": "read_file", "status": "ok"}],
            duration_ms=200,
        )
        runtime.subagent_stop(
            parent_session_id="session-1",
            parent_turn_id="turn-1",
            child_session_id="child-1",
            child_role="leaf",
            child_summary="Duplicate completion event.",
            child_status="completed",
            tool_call_history=[],
            duration_ms=200,
        )
        original = json.dumps({
            "results": [{"status": "completed", "summary": "Implemented the fix and all tests pass."}]
        })

        self.assertIsNone(runtime.transform_tool_result(
            tool_name="delegate_task",
            args={"goal": "different call"},
            result=original,
            session_id="session-1",
            turn_id="turn-2",
        ))
        self.assertIsNone(runtime.transform_tool_result(
            tool_name="delegate_task",
            args={"goal": "fix it"},
            result="not-json",
            session_id="session-1",
            turn_id="turn-1",
        ))

        transformed = runtime.transform_tool_result(
            tool_name="delegate_task",
            args={"goal": "fix it"},
            result=original,
            task_id="",
            session_id="session-1",
            turn_id="turn-1",
        )

        payload = json.loads(transformed)
        self.assertIn("AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW", payload["claude_fusion_reviews"][0])
        self.assertIn("metadata shows no test command", payload["claude_fusion_reviews"][0])
        self.assertIn("results", payload)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 2, "duplicate child events must not trigger another review")

    def test_subagent_review_includes_files_created_after_turn_baseline(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        (self.repo / "child_created.py").write_text("child_value = 7\n", encoding="utf-8")

        runtime.subagent_stop(
            parent_session_id="session-1",
            parent_turn_id="turn-1",
            child_session_id="child-1",
            child_role="leaf",
            child_summary="Implemented the child change.",
            child_status="completed",
            tool_call_history=[],
        )

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertIn("child_created.py", calls[-1]["prompt"])
        self.assertIn("child_value = 7", calls[-1]["prompt"])

    def test_plugin_registers_all_hermes_lifecycle_hooks(self):
        module = load_plugin_module()

        class Context:
            def __init__(self):
                self.hooks = {}
                self.middleware = {}
                self.skills = {}

            def register_hook(self, name, callback):
                self.hooks[name] = callback

            def register_middleware(self, name, callback):
                self.middleware[name] = callback

            def register_skill(self, name, path):
                self.skills[name] = Path(path)

        context = Context()
        module.register(context)

        self.assertEqual(
            set(context.hooks),
            {"pre_llm_call", "pre_verify", "subagent_stop", "transform_tool_result"},
        )
        self.assertTrue(all(callable(callback) for callback in context.hooks.values()))
        self.assertEqual(set(context.middleware), {"tool_execution"})
        self.assertTrue(callable(context.middleware["tool_execution"]))
        self.assertEqual(set(context.skills), {"claude-fusion"})
        self.assertTrue(context.skills["claude-fusion"].is_file())
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: claude-fusion", manifest)
        self.assertIn("kind: standalone", manifest)

    def test_tool_execution_middleware_attaches_review_to_delegate_result(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False),
            cwd_provider=lambda: self.repo,
            which=lambda _name: None,
        )
        key = ("session-1", "turn-1")
        summary = "child completed"
        runtime._pending_subagent_reviews[key] = [
            {
                "review": "AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW: inspect race",
                "summary_digest": module._summary_digest(summary),
                "tool_call_id": "call-1",
            }
        ]

        transformed = runtime.tool_execution_middleware(
            tool_name="delegate_task",
            args={"goal": "fix it"},
            next_call=lambda _args: json.dumps(
                {"results": [{"status": "completed", "summary": summary}]}
            ),
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )

        payload = json.loads(transformed)
        self.assertIn("inspect race", payload["claude_fusion_reviews"][0])
        self.assertNotIn(key, runtime._pending_subagent_reviews)

    def test_invalid_tools_settings_fail_closed_and_readonly_is_explicit(self):
        module = load_runtime_module()
        for value in ("typo", "", None, False, True, 1):
            settings = module.Settings(tools=value)
            self.assertEqual(settings.tools, "none", repr(value))
            runtime = module.ClaudeFusionRuntime(settings=settings)
            args = runtime._claude_args("claude", {}, "model", "high", False, False)
            self.assertIn("--tools", args)
            self.assertNotIn("--allowedTools", args)

        self.assertEqual(module.Settings(tools="none").tools, "none")
        readonly = module.Settings(tools="readonly")
        self.assertEqual(readonly.tools, "readonly")
        readonly_args = module.ClaudeFusionRuntime(settings=readonly)._claude_args(
            "claude", {}, "model", "high", False, False
        )
        self.assertIn("--allowedTools", readonly_args)

    def test_tool_call_identity_prevents_one_finding_one_pass_cross_attribution(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        key = ("session-1", "turn-1")
        summary = "identical summary"
        runtime._pending_subagent_reviews[key] = [
            {
                "review": "review that belongs to call B",
                "summary_digest": module._summary_digest(summary),
                "tool_call_id": "call-B",
            }
        ]
        delegate_result = json.dumps(
            {"results": [{"status": "completed", "summary": summary}]}
        )

        call_a = runtime.tool_execution_middleware(
            tool_name="delegate_task",
            args={"goal": "A"},
            next_call=lambda _args: delegate_result,
            session_id=key[0],
            turn_id=key[1],
            tool_call_id="call-A",
        )
        self.assertNotIn("claude_fusion_reviews", json.loads(call_a))
        self.assertEqual(len(runtime._pending_subagent_reviews[key]), 1)

        call_b = runtime.tool_execution_middleware(
            tool_name="delegate_task",
            args={"goal": "B"},
            next_call=lambda _args: delegate_result,
            session_id=key[0],
            turn_id=key[1],
            tool_call_id="call-B",
        )
        self.assertEqual(
            json.loads(call_b)["claude_fusion_reviews"],
            ["review that belongs to call B"],
        )
        self.assertNotIn(key, runtime._pending_subagent_reviews)

    def test_agent_loop_without_tool_call_id_defers_review(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        key = ("session-1", "turn-1")
        summary = "identical summary"
        runtime._pending_subagent_reviews[key] = [
            {
                "review": "unscoped review",
                "summary_digest": module._summary_digest(summary),
                "tool_call_id": "",
            }
        ]
        raw = json.dumps({"results": [{"status": "completed", "summary": summary}]})

        result = runtime.tool_execution_middleware(
            tool_name="delegate_task",
            args={"goal": "work"},
            next_call=lambda _args: raw,
            session_id=key[0],
            turn_id=key[1],
            tool_call_id="",
        )

        self.assertEqual(result, raw)
        self.assertEqual(len(runtime._pending_subagent_reviews[key]), 1)

    def test_untracked_fifo_is_omitted_without_blocking(self):
        fifo = self.repo / "review.pipe"
        os.mkfifo(fifo)
        script = textwrap.dedent(
            f"""\
            import importlib.util
            from pathlib import Path
            spec = importlib.util.spec_from_file_location('fifo_runtime', {str(RUNTIME_FILE)!r})
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            repo = Path({str(self.repo)!r})
            runtime = module.ClaudeFusionRuntime(
                settings=module.Settings(depth='single', ultracode=False),
                cwd_provider=lambda: repo,
                which=lambda _name: None,
            )
            runtime._git_result = lambda _repo, *args: (True, '')
            runtime._git = lambda _repo, *args: ''
            runtime._untracked_paths = lambda _repo: {{'review.pipe'}}
            print(repr(runtime._filtered_diff(repo, 'HEAD', ['review.pipe'], set())))
            """
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(f"FIFO review path blocked artifact generation: {exc}")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "''")

    def test_install_hermes_links_and_enables_the_plugin_in_an_isolated_home(self):
        hermes_home = self.base / "hermes-home"
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        completed = subprocess.run(
            ["bash", str(ROOT / "install-hermes.sh"), "--link"],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotIn("Grant it?", completed.stdout + completed.stderr)
        target = hermes_home / "plugins" / "claude-fusion"
        self.assertTrue(target.is_symlink())
        listed = subprocess.run(
            ["hermes", "plugins", "list"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("claude-fusion", listed.stdout)
        self.assertIn("enabled", listed.stdout.lower())

        hook_env = env.copy()
        hook_env["TERMINAL_CWD"] = str(self.repo)
        hermes_python = resolve_hermes_python()
        integration_script = textwrap.dedent(
            """\
            import json
            from agent.agent_runtime_helpers import invoke_tool
            from hermes_cli.plugins import discover_plugins, invoke_hook

            discover_plugins(force=True)
            turn = "integration:task:123e4567"
            pre = invoke_hook(
                "pre_llm_call",
                session_id="integration-session",
                turn_id=turn,
                user_message="Refactor the authentication module to remove a race condition",
                conversation_history=[],
                is_first_turn=True,
                model="test-model",
                platform="cli",
                parent_session_id="",
            )
            class Agent:
                session_id = "integration-session"
                _current_turn_id = turn
                _current_api_request_id = "integration-request"
                valid_tool_names = {"delegate_task"}
                enabled_toolsets = None
                disabled_toolsets = None
                _memory_manager = None
                _context_engine_tool_names = set()

                def _dispatch_delegate_task(self, _args):
                    # Synchronous orchestrator dispatch emits subagent_stop
                    # before the delegate result returns to middleware.
                    invoke_hook(
                        "subagent_stop",
                        parent_session_id="integration-session",
                        parent_turn_id=turn,
                        child_session_id="child:session:1",
                        child_role="leaf",
                        child_summary="Implemented the fix and all tests pass.",
                        child_status="completed",
                        tool_call_history=[],
                        duration_ms=10,
                    )
                    return json.dumps({
                        "results": [{
                            "status": "completed",
                            "summary": "Implemented the fix and all tests pass.",
                        }]
                    })

            post = invoke_tool(
                Agent(),
                "delegate_task",
                {"goal": "fix it"},
                "integration-task",
                tool_call_id="integration-call",
            )
            print(json.dumps({"pre": pre, "post": post}))
            """
        )
        invoked = subprocess.run(
            [hermes_python, "-c", integration_script],
            cwd=str(ROOT),
            env=hook_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(invoked.returncode, 0, invoked.stdout + invoked.stderr)
        hook_results = json.loads(invoked.stdout.splitlines()[-1])
        self.assertIn("AUTOMATIC CLAUDE FUSION CONTEXT", hook_results["pre"][0]["context"])
        transformed = json.loads(hook_results["post"])
        self.assertIn("AUTOMATIC CLAUDE FUSION - SUBAGENT REVIEW", transformed["claude_fusion_reviews"][0])

        doctor = subprocess.run(
            ["bash", str(ROOT / "doctor-hermes.sh")],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("Hermes port: ready", doctor.stdout)

    def test_install_force_restores_previous_plugin_when_enable_fails(self):
        hermes_home = self.base / "hermes-home"
        target = hermes_home / "plugins" / "claude-fusion"
        target.mkdir(parents=True)
        sentinel = target / "previous-install.txt"
        sentinel.write_text("keep me\n", encoding="utf-8")
        config_path = hermes_home / "config.yaml"
        config_path.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
        fake_hermes = self.bin / "hermes"
        fake_hermes.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo 'Hermes Agent v0.19.1'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"plugins enable\" ]]; then "
            "printf 'plugins:\\n  enabled: [claude-fusion]\\n' > \"$HERMES_HOME/config.yaml\"; exit 1; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        completed = subprocess.run(
            ["bash", str(ROOT / "install-hermes.sh"), "--force"],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(config_path.read_text(encoding="utf-8"), "plugins:\n  enabled: []\n")

    def test_copy_install_and_doctor_rejects_broken_installed_entrypoint(self):
        hermes_home = self.base / "hermes-home"
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        completed = subprocess.run(
            ["bash", str(ROOT / "install-hermes.sh")],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        target = hermes_home / "plugins" / "claude-fusion"
        self.assertTrue(target.is_dir())
        self.assertFalse(target.is_symlink())
        self.assertTrue((target / "hermes-plugin" / "runtime.py").is_file())
        (target / "__init__.py").write_text("raise RuntimeError('broken install')\n", encoding="utf-8")

        doctor = subprocess.run(
            ["bash", str(ROOT / "doctor-hermes.sh")],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertNotEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("installed plugin entrypoint", (doctor.stdout + doctor.stderr).lower())

    def test_real_hermes_colon_delimited_turn_id_routes_subagent_review(self):
        turn_id = "session:task:123e4567-e89b-12d3-a456-426614174000"
        summary = "Implemented the fix and all tests pass."
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1", turn_id=turn_id, user_message=GATED_PROMPT, parent_session_id=""
        )
        runtime.subagent_stop(
            parent_session_id="session-1",
            parent_turn_id=turn_id,
            child_session_id="child:session:1",
            child_role="leaf",
            child_summary=summary,
            child_status="completed",
            tool_call_history=[],
        )
        transformed = runtime.transform_tool_result(
            tool_name="delegate_task",
            result=json.dumps({"results": [{"status": "completed", "summary": summary}]}),
            session_id="session-1",
            turn_id=turn_id,
        )

        self.assertIsNotNone(transformed)
        self.assertIn("claude_fusion_reviews", json.loads(transformed))

    def test_sensitive_staged_delete_add_pair_quarantines_added_destination(self):
        (self.repo / ".env").write_text("SECRET_VALUE=rename-secret\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", ".env"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add env"], check=True)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=10),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )
        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        (self.repo / ".env").rename(self.repo / "public.txt")
        with (self.repo / "public.txt").open("a", encoding="utf-8") as handle:
            handle.write("".join("added_line_{}=value\n".format(index) for index in range(20)))
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        status = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--name-status", "-z", "-M", "HEAD", "--"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertIn(b"D\x00.env\x00", status)
        self.assertIn(b"A\x00public.txt\x00", status)

        runtime.pre_verify(
            session_id="session-1", coding=True, attempt=0, changed_paths=[".env", "public.txt"]
        )

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        if len(calls) > 1:
            review_prompt = calls[-1]["prompt"]
            self.assertNotIn("rename-secret", review_prompt)
            self.assertNotIn("SECRET_VALUE", review_prompt)

    @unittest.skipIf(os.name != "posix", "process-group timeout behavior is POSIX-specific")
    def test_capability_timeout_kills_probe_descendants(self):
        descendant_marker = self.base / "probe-descendant-survived"
        self.claude.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import subprocess
                import sys
                import time

                if "--help" in sys.argv:
                    subprocess.Popen([
                        sys.executable,
                        "-c",
                        "import time; from pathlib import Path; time.sleep(2); "
                        "Path({str(descendant_marker)!r}).write_text('survived')",
                    ])
                    time.sleep(5)
                    raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False, timeout=1),
            cwd_provider=lambda: self.repo,
            which=lambda name: str(self.claude) if name == "claude" else None,
        )

        runtime.pre_llm_call(
            session_id="session-1", turn_id="turn-1", user_message=GATED_PROMPT, parent_session_id=""
        )
        time.sleep(1.5)

        self.assertFalse(descendant_marker.exists())

    def test_doctor_rejects_failed_plugin_listing_that_mentions_plugin_name(self):
        hermes_home = self.base / "hermes-home"
        target = hermes_home / "plugins" / "claude-fusion"
        target.parent.mkdir(parents=True)
        target.symlink_to(ROOT, target_is_directory=True)
        fake_hermes = self.bin / "hermes"
        fake_hermes.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo 'Hermes Agent v0.19.1'; exit 0; fi\n"
            "echo 'claude-fusion listing failed' >&2\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        doctor = subprocess.run(
            ["bash", str(ROOT / "doctor-hermes.sh")],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertNotEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("plugin listing failed", (doctor.stdout + doctor.stderr).lower())

    def test_transform_correlates_concurrent_delegate_reviews_by_child_summary(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        key = ("session-1", "session:task:turn")
        runtime._pending_subagent_reviews[key] = [
            {"review": "review A", "summary_digest": module._summary_digest("summary A")},
            {"review": "review B", "summary_digest": module._summary_digest("summary B")},
        ]

        transformed = runtime.transform_tool_result(
            tool_name="delegate_task",
            result=json.dumps({"results": [{"summary": "summary B"}]}),
            session_id=key[0],
            turn_id=key[1],
        )

        payload = json.loads(transformed)
        self.assertEqual(payload["claude_fusion_reviews"], ["review B"])
        self.assertEqual(runtime._pending_subagent_reviews[key], [
            {"review": "review A", "summary_digest": module._summary_digest("summary A")}
        ])

    def test_transform_does_not_drop_review_appended_during_serialization(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        key = ("session-1", "session:task:turn")
        first = {"review": "review A", "summary_digest": module._summary_digest("summary A")}
        second = {"review": "review B", "summary_digest": module._summary_digest("summary B")}
        runtime._pending_subagent_reviews[key] = [first]
        serialization_started = threading.Event()
        release_serialization = threading.Event()
        original_dumps = module.json.dumps

        def slow_dumps(*args, **kwargs):
            serialization_started.set()
            release_serialization.wait(timeout=2)
            return original_dumps(*args, **kwargs)

        module.json.dumps = slow_dumps
        transformed = []
        worker = threading.Thread(target=lambda: transformed.append(runtime.transform_tool_result(
            tool_name="delegate_task",
            result=original_dumps({"results": [{"summary": "summary A"}]}),
            session_id=key[0],
            turn_id=key[1],
        )))
        appender = threading.Thread(
            target=lambda: self._append_pending_review(runtime, key, second)
        )
        try:
            worker.start()
            self.assertTrue(serialization_started.wait(timeout=2))
            appender.start()
            time.sleep(0.05)
            self.assertTrue(appender.is_alive(), "append must wait for atomic transform")
            release_serialization.set()
            worker.join(timeout=2)
            appender.join(timeout=2)
        finally:
            module.json.dumps = original_dumps
            release_serialization.set()

        self.assertIsNotNone(transformed[0])
        self.assertEqual(runtime._pending_subagent_reviews[key], [second])

    @staticmethod
    def _append_pending_review(runtime, key, review):
        with runtime._lock:
            runtime._pending_subagent_reviews.setdefault(key, []).append(review)

    def test_sensitive_copy_provenance_is_excluded_from_filtered_diff(self):
        (self.repo / ".env").write_text("SECRET_VALUE=copy-secret\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", ".env"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add env"], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        shutil.copyfile(self.repo / ".env", self.repo / "public.txt")
        subprocess.run(["git", "-C", str(self.repo), "add", "public.txt"], check=True)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(settings=module.Settings(max_file_bytes=409600))

        diff = runtime._filtered_diff(self.repo, baseline, ["public.txt"], set())

        self.assertNotIn("copy-secret", diff)
        self.assertNotIn("SECRET_VALUE", diff)

    def test_sensitive_delete_modified_destination_is_quarantined(self):
        (self.repo / ".env").write_text("SECRET_VALUE=overwrite-secret\n", encoding="utf-8")
        (self.repo / "public.txt").write_text("safe = True\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", ".env", "public.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add fixtures"], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        (self.repo / "public.txt").write_bytes((self.repo / ".env").read_bytes())
        (self.repo / ".env").unlink()
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(settings=module.Settings(max_file_bytes=409600))

        diff = runtime._filtered_diff(self.repo, baseline, [".env", "public.txt"], set())

        self.assertNotIn("overwrite-secret", diff)
        self.assertNotIn("SECRET_VALUE", diff)

    def test_deleted_oversized_baseline_blob_is_excluded(self):
        (self.repo / "large.txt").write_text("baseline-secret\n" * 150, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "large.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "add large file"], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        (self.repo / "large.txt").unlink()
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(settings=module.Settings(max_file_bytes=32))

        diff = runtime._filtered_diff(self.repo, baseline, ["large.txt"], set())

        self.assertNotIn("baseline-secret", diff)

    def test_duplicate_summary_ambiguity_is_deferred_instead_of_cross_attributed(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        key = ("session-1", "session:task:turn")
        digest = module._summary_digest("identical summary")
        queued = [
            {"review": "review A", "summary_digest": digest},
            {"review": "review B", "summary_digest": digest},
        ]
        runtime._pending_subagent_reviews[key] = list(queued)

        transformed = runtime.transform_tool_result(
            tool_name="delegate_task",
            result=json.dumps({"results": [{"summary": "identical summary"}]}),
            session_id=key[0],
            turn_id=key[1],
        )

        self.assertIsNone(transformed)
        self.assertEqual(runtime._pending_subagent_reviews[key], queued)

    def test_installer_rejects_failed_claude_help_even_when_output_mentions_safe_mode(self):
        hermes_home = self.base / "hermes-home"
        fake_hermes = self.bin / "hermes"
        fake_hermes.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo 'Hermes Agent v0.19.1'; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
        self.claude.write_text(
            "#!/usr/bin/env bash\necho 'unsupported --safe-mode' >&2\nexit 1\n",
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        completed = subprocess.run(
            ["bash", str(ROOT / "install-hermes.sh")],
            cwd=str(ROOT), env=env, check=False, capture_output=True, text=True, timeout=30,
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse((hermes_home / "plugins" / "claude-fusion").exists())

    def test_runtime_state_is_bounded_across_many_unverified_sessions(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        runtime._repo_root = lambda: self.repo
        runtime._git = lambda *_args: "a" * 40
        runtime._filtered_status = lambda _repo: "(clean)"
        runtime._untracked_paths = lambda _repo: set()
        runtime._run_contract = lambda *_args, **_kwargs: {"analysis": "ok", "questions": []}
        for index in range(600):
            session = "session-{}".format(index)
            turn = "{}:task:turn".format(session)
            runtime.pre_llm_call(
                session_id=session, turn_id=turn, user_message=GATED_PROMPT, parent_session_id=""
            )
            runtime._pending_subagent_reviews[(session, turn)] = [
                {"review": "queued", "summary_digest": "digest"}
            ]

        self.assertLessEqual(len(runtime._turns), 512)
        self.assertLessEqual(len(runtime._latest_turn), 512)
        self.assertLessEqual(len(runtime._pending_subagent_reviews), 512)

    def test_doctor_rejects_failed_version_and_help_commands_despite_matching_output(self):
        hermes_home = self.base / "hermes-home"
        target = hermes_home / "plugins" / "claude-fusion"
        target.parent.mkdir(parents=True)
        target.symlink_to(ROOT, target_is_directory=True)
        fake_hermes = self.bin / "hermes"
        fake_hermes.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo 'Hermes Agent v0.19.1'; exit 1; fi\n"
            "if [[ \"$1 $2\" == \"plugins list\" ]]; then "
            "echo '[{\"name\":\"claude-fusion\",\"status\":\"enabled\",\"source\":\"user\"}]'; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
        self.claude.write_text(
            "#!/usr/bin/env bash\necho '--safe-mode --json-schema --output-format'\nexit 1\n",
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        doctor = subprocess.run(
            ["bash", str(ROOT / "doctor-hermes.sh")],
            cwd=str(ROOT), env=env, check=False, capture_output=True, text=True, timeout=30,
        )

        self.assertNotEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertNotIn("Hermes port: ready", doctor.stdout)

    def test_failed_copy_provenance_query_fails_closed_for_tracked_paths(self):
        (self.repo / "public.txt").write_text("SECRET_VALUE=unknown-copy\n", encoding="utf-8")
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()

        def fake_git_result(_repo, *args):
            if "--name-status" in args:
                return False, ""
            if "--name-only" in args:
                return True, "public.txt\0"
            return True, ""

        runtime._git_result = fake_git_result
        runtime._untracked_paths = lambda _repo: set()

        diff = runtime._filtered_diff(self.repo, "a" * 40, ["public.txt"], set())

        self.assertEqual(diff, "")

    def test_installer_rolls_back_when_post_enable_discovery_fails(self):
        hermes_home = self.base / "hermes-home"
        target = hermes_home / "plugins" / "claude-fusion"
        target.mkdir(parents=True)
        sentinel = target / "previous-install.txt"
        sentinel.write_text("keep me\n", encoding="utf-8")
        config_path = hermes_home / "config.yaml"
        config_path.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
        fake_hermes = self.bin / "hermes"
        fake_hermes.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo 'Hermes Agent v0.19.1'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"plugins enable\" ]]; then "
            "printf 'plugins:\\n  enabled: [claude-fusion]\\n' > \"$HERMES_HOME/config.yaml\"; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"plugins list\" ]]; then echo '[]'; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        completed = subprocess.run(
            ["bash", str(ROOT / "install-hermes.sh"), "--force"],
            cwd=str(ROOT), env=env, check=False, capture_output=True, text=True, timeout=30,
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(sentinel.is_file())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(config_path.read_text(encoding="utf-8"), "plugins:\n  enabled: []\n")

    def test_both_failed_provenance_queries_fail_closed_for_changed_tracked_paths(self):
        (self.repo / "public.txt").write_text("SECRET_VALUE=provenance-failure-leak\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "public.txt"], check=True)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        original_git = runtime._git

        def fake_git_result(_repo, *args):
            if "--name-status" in args or "--name-only" in args:
                return False, ""
            return True, original_git(_repo, *args)

        runtime._git_result = fake_git_result
        runtime._untracked_paths = lambda _repo: set()

        diff = runtime._filtered_diff(self.repo, "HEAD", ["public.txt"], set())

        self.assertNotIn("provenance-failure-leak", diff)

    def test_malformed_name_status_record_fails_closed(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime(
            settings=module.Settings(depth="single", ultracode=False),
            cwd_provider=lambda: self.repo,
            which=lambda _name: None,
        )
        public = self.repo / "public.txt"
        public.write_text("SECRET_VALUE=malformed-status-leak\n", encoding="utf-8")

        def malformed_git_result(_repo, *args):
            if "--name-status" in args:
                return True, "R100\0.env\0"  # missing rename destination
            if "--name-only" in args:
                return True, "public.txt\0"
            return True, ""

        runtime._git_result = malformed_git_result
        runtime._git = lambda _repo, *args: (
            "diff --git a/public.txt b/public.txt\n+SECRET_VALUE=malformed-status-leak\n"
            if "diff" in args else ""
        )
        runtime._untracked_paths = lambda _repo: set()

        diff = runtime._filtered_diff(self.repo, "HEAD", ["public.txt"], set())

        self.assertNotIn("malformed-status-leak", diff)

    def test_equal_duplicate_summary_multiplicity_is_still_deferred(self):
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()
        key = ("session-1", "session:task:turn")
        digest = module._summary_digest("identical summary")
        queued = [
            {"review": "review from call A", "summary_digest": digest},
            {"review": "review from call B", "summary_digest": digest},
        ]
        runtime._pending_subagent_reviews[key] = list(queued)

        transformed = runtime.transform_tool_result(
            tool_name="delegate_task",
            result=json.dumps({
                "results": [
                    {"summary": "identical summary"},
                    {"summary": "identical summary"},
                ]
            }),
            session_id=key[0],
            turn_id=key[1],
        )

        self.assertIsNone(transformed)
        self.assertEqual(runtime._pending_subagent_reviews[key], queued)

    def test_runtime_capability_probe_rejects_failed_help_output(self):
        self.claude.write_text(
            "#!/usr/bin/env bash\necho 'unsupported --safe-mode --output-format --json-schema'\nexit 1\n",
            encoding="utf-8",
        )
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        module = load_runtime_module()
        runtime = module.ClaudeFusionRuntime()

        capabilities = runtime._capabilities(str(self.claude), 2.0)

        self.assertEqual(capabilities, "")
        self.assertEqual(runtime._capability_cache, {})

    def test_copy_installer_omits_untracked_sensitive_checkout_files(self):
        checkout = self.base / "checkout"
        shutil.copytree(
            ROOT,
            checkout,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        (checkout / ".env").write_text("INSTALL_SECRET=must-not-copy\n", encoding="utf-8")
        (checkout / "hermes-plugin" / ".env").write_text(
            "NESTED_INSTALL_SECRET=must-not-copy\n", encoding="utf-8"
        )
        hermes_home = self.base / "hermes-home"
        fake_hermes = self.bin / "hermes"
        fake_hermes.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"--version\" ]]; then echo 'Hermes Agent v0.19.1'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"plugins enable\" ]]; then exit 0; fi\n"
            "if [[ \"$1 $2\" == \"plugins list\" ]]; then "
            "echo '[{\"name\":\"claude-fusion\",\"status\":\"enabled\",\"source\":\"user\"}]'; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_hermes.chmod(fake_hermes.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

        completed = subprocess.run(
            ["bash", str(checkout / "install-hermes.sh")],
            cwd=str(checkout), env=env, check=False, capture_output=True, text=True, timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        installed = hermes_home / "plugins" / "claude-fusion"
        self.assertFalse((installed / ".env").exists())
        self.assertFalse((installed / "hermes-plugin" / ".env").exists())
        self.assertTrue((installed / "hermes-plugin" / "runtime.py").is_file())

    def test_subagent_review_limit_is_clamped(self):
        module = load_runtime_module()

        settings = module.Settings(subagent_review_limit=1000000)

        self.assertLessEqual(settings.subagent_review_limit, 8)


if __name__ == "__main__":
    unittest.main()
