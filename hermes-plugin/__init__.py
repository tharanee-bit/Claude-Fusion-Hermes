"""Claude Fusion integration for Hermes Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .runtime import ClaudeFusionRuntime, Settings


_runtime: ClaudeFusionRuntime | None = None


def _configured_settings() -> Settings:
    """Load plugin-local settings without making config availability fatal."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        entries = ((config.get("plugins") or {}).get("entries") or {})
        entry: Dict[str, Any] = entries.get("claude-fusion") or {}
        return Settings.from_mapping(entry.get("settings") or {})
    except Exception:
        return Settings()


def register(ctx: Any) -> None:
    """Register Claude Fusion's native Hermes lifecycle hooks."""
    global _runtime
    _runtime = ClaudeFusionRuntime(settings=_configured_settings())
    ctx.register_hook("pre_llm_call", _runtime.pre_llm_call)
    ctx.register_hook("pre_verify", _runtime.pre_verify)
    ctx.register_hook("subagent_stop", _runtime.subagent_stop)
    ctx.register_hook("transform_tool_result", _runtime.transform_tool_result)
    ctx.register_middleware("tool_execution", _runtime.tool_execution_middleware)
    ctx.register_skill(
        "claude-fusion",
        Path(__file__).resolve().parent / "skills" / "claude-fusion" / "SKILL.md",
    )
