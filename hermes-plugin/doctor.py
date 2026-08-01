#!/usr/bin/env python3
"""Validate an installed Claude Fusion plugin entry point without running Claude."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


EXPECTED_HOOKS = {
    "pre_llm_call",
    "pre_verify",
    "subagent_stop",
    "transform_tool_result",
}
EXPECTED_MIDDLEWARE = {"tool_execution"}


class ProbeContext:
    def __init__(self) -> None:
        self.hooks = {}
        self.middleware = {}
        self.skills = {}

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

    def register_middleware(self, name, callback) -> None:
        self.middleware[name] = callback

    def register_skill(self, name, path) -> None:
        self.skills[name] = Path(path)


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--plugin-list":
        try:
            payload = json.load(sys.stdin)
        except (TypeError, ValueError) as exc:
            print(f"Hermes plugin list is not valid JSON: {exc}", file=sys.stderr)
            return 1
        matches = [
            item for item in payload
            if isinstance(item, dict)
            and item.get("name") == "claude-fusion"
            and item.get("status") == "enabled"
            and item.get("source") != "bundled"
        ] if isinstance(payload, list) else []
        if len(matches) != 1:
            print("Hermes did not report exactly one enabled user claude-fusion plugin", file=sys.stderr)
            return 1
        return 0
    if len(sys.argv) != 2:
        print("usage: doctor.py PLUGIN_DIR | doctor.py --plugin-list", file=sys.stderr)
        return 2
    plugin_dir = Path(sys.argv[1]).expanduser().resolve()
    entrypoint = plugin_dir / "__init__.py"
    if not entrypoint.is_file():
        print(f"installed plugin entrypoint is missing: {entrypoint}", file=sys.stderr)
        return 1

    module_name = "claude_fusion_installed_doctor"
    spec = importlib.util.spec_from_file_location(
        module_name,
        entrypoint,
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        print("installed plugin entrypoint could not be loaded", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        context = ProbeContext()
        module.register(context)
    except Exception as exc:
        print(f"installed plugin entrypoint failed to register: {exc}", file=sys.stderr)
        return 1
    finally:
        sys.modules.pop(module_name, None)

    if set(context.hooks) != EXPECTED_HOOKS or not all(callable(callback) for callback in context.hooks.values()):
        print(
            "installed plugin entrypoint registered unexpected or non-callable hooks: "
            + ", ".join(sorted(context.hooks)),
            file=sys.stderr,
        )
        return 1
    if set(context.middleware) != EXPECTED_MIDDLEWARE or not all(
        callable(callback) for callback in context.middleware.values()
    ):
        print(
            "installed plugin entrypoint registered unexpected or non-callable middleware: "
            + ", ".join(sorted(context.middleware)),
            file=sys.stderr,
        )
        return 1
    skill = context.skills.get("claude-fusion")
    expected_skill = (plugin_dir / "hermes-plugin" / "skills" / "claude-fusion" / "SKILL.md").resolve()
    if not isinstance(skill, Path) or not skill.is_file() or skill.resolve() != expected_skill:
        print("installed plugin entrypoint did not register its bundled skill", file=sys.stderr)
        return 1
    print("installed plugin entrypoint, hooks, and middleware registration are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
