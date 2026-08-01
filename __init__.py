"""Hermes Agent entry point for the Claude Fusion repository plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_hermes_plugin() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parent / "hermes-plugin"
    module_name = (__package__ or "claude_fusion") + "._hermes_impl"
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Unable to load the Claude Fusion Hermes implementation")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_impl = _load_hermes_plugin()


def register(ctx: Any) -> None:
    """Register the repository's native Hermes hooks and bundled skill."""
    _impl.register(ctx)
