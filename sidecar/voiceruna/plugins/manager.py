"""Plugin manager: scan plugins/, import enabled packs, register their tools.

Plugins live in <repo>/plugins/<name>/plugin.py. At startup the manager reads
"enabled_plugins" from config.json, imports only those folders (sorted in the
configured order) and registers their tools in the global ToolRegistry.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..logging_setup import get_logger
from ..paths import resource_dir
from ..tools.registry import REGISTRY, register_tool
from .base import BasePlugin

__all__ = ["PluginManager", "plugins_dir"]

log = get_logger("plugins")

DEFAULT_PLUGINS = ["apps_launcher", "system_tools", "basic_info"]


def plugins_dir(explicit: str | Path | None = None) -> Path:
    """Where plugin folders live: repo/plugins in dev, bundled plugins/ frozen."""
    if explicit is not None:
        return Path(explicit)
    bundled = resource_dir() / "plugins"
    if bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parents[3] / "plugins"


class PluginManager:
    """Loads enabled plugins and publishes their tools through ToolRegistry."""

    def __init__(self, config: Any, directory: str | Path | None = None, app: Any = None) -> None:
        self._config = config
        self._dir = plugins_dir(directory)
        self._app = app
        self.plugins: dict[str, BasePlugin] = {}
        self.tool_names: list[str] = []

    @property
    def directory(self) -> Path:
        return self._dir

    def _enabled(self) -> list[str]:
        raw = self._config.get("enabled_plugins", DEFAULT_PLUGINS) if self._config else DEFAULT_PLUGINS
        if not isinstance(raw, list) or not raw:
            return list(DEFAULT_PLUGINS)
        return [str(item).strip() for item in raw if str(item).strip()]

    def discover(self) -> list[str]:
        """Folders with a plugin.py, sorted by name."""
        if not self._dir.is_dir():
            return []
        return sorted(
            entry.name for entry in self._dir.iterdir()
            if entry.is_dir() and (entry / "plugin.py").is_file()
        )

    def load_plugin(self, name: str) -> BasePlugin | None:
        plugin_file = self._dir / name / "plugin.py"
        if not plugin_file.is_file():
            log.warning("plugin %r has no plugin.py in %s", name, self._dir)
            return None
        module_name = f"vox_plugin_{name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(plugin_file))
            if spec is None or spec.loader is None:
                raise ImportError("no loader")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not kill startup
            log.error("plugin %r failed to import: %s: %s", name, type(exc).__name__, exc)
            return None

        plugin_cls = next(
            (
                value for value in vars(module).values()
                if isinstance(value, type) and issubclass(value, BasePlugin) and value is not BasePlugin
            ),
            None,
        )
        if plugin_cls is None:
            log.warning("plugin %r does not define a BasePlugin subclass", name)
            return None
        context = self._app if self._app is not None else SimpleNamespace(config=self._config)
        plugin = plugin_cls(context)
        plugin.plugin_file = plugin_file  # type: ignore[attr-defined]
        for definition in plugin.get_tools():
            register_tool(
                str(definition["name"]),
                str(definition.get("description", "")),
                dict(definition.get("parameters") or {"type": "object", "properties": {}}),
                definition["handler"],
            )
            self.tool_names.append(str(definition["name"]))
        self.plugins[name] = plugin
        log.info("plugin loaded: %s (%d tools)", name, len(plugin.get_tools()))
        return plugin

    def load(self, enabled: list[str] | None = None) -> list[str]:
        """Load the enabled plugins (config order) and return their tool names."""
        self.plugins.clear()
        self.tool_names.clear()
        wanted = enabled if enabled is not None else self._enabled()
        for name in wanted:
            plugin = self.load_plugin(name)
            if plugin is not None:
                continue
            if (self._dir / name).is_dir():
                log.warning("plugin %r was not loaded", name)
        return list(self.tool_names)
