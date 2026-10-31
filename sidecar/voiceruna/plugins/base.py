"""Plugin base class for the VoiceRunaCore microkernel.

A plugin is a folder with plugin.py that exposes one BasePlugin subclass.
The manager imports it at startup, and the plugin registers its tools in the
global ToolRegistry. Audio, LLM and the UI stay outside the plugin contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

__all__ = ["BasePlugin", "load_module"]


def load_module(path: Path, name: str) -> Any:
    """Import a helper module that lives next to plugin.py (dev and frozen)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BasePlugin:
    """One feature pack: a name and a list of tool definitions."""

    name = "base"
    title = "Плагин"
    description = ""

    def __init__(self, app: Any = None) -> None:
        self.app = app

    def get_name(self) -> str:
        return str(self.name)

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions: name, description, parameters, handler."""
        return []

    def load_helper(self, filename: str, module_name: str) -> Any:
        """Import a module from this plugin folder (weather.py, volume.py...)."""
        plugin_file = Path(getattr(self, "plugin_file", ""))
        return load_module(plugin_file.parent / filename, module_name)
