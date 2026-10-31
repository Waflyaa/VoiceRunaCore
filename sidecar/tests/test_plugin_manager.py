"""Dynamic plugin loading: %APPDATA%/plugins and the first-run bootstrap."""

from __future__ import annotations

import shutil
from pathlib import Path

from voiceruna.plugins.manager import PluginManager
from voiceruna.tools import tool_names

FAKE_PLUGIN = '''
from voiceruna.plugins.base import BasePlugin
from voiceruna.tools.base import ToolResult


class FakePlugin(BasePlugin):
    name = "fake"
    title = "Фейковый плагин"
    description = "Тестовый плагин из временной папки."

    def get_tools(self):
        async def handler(executor, args):
            return ToolResult(ok=True, speech="fake ok")

        return [
            {
                "name": "fake_tool",
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
                "handler": handler,
            }
        ]
'''


def _write_plugin(folder: Path, name: str = "fake") -> Path:
    plugin_dir = folder / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(FAKE_PLUGIN, encoding="utf-8")
    return plugin_dir


def test_manager_loads_a_plugin_from_the_given_folder(config, tmp_path):
    folder = tmp_path / "plugins"
    _write_plugin(folder, "fake")
    manager = PluginManager(config, directory=folder)

    loaded = manager.load(enabled=["fake"])

    assert "fake" in manager.plugins
    assert loaded == ["fake_tool"]
    assert "fake_tool" in tool_names()
    assert manager.catalog()[0]["title"] == "Фейковый плагин"


def test_bootstrap_copies_defaults_into_the_appdata_folder(config, data_dir, tmp_path):
    bundled = tmp_path / "bundled"
    _write_plugin(bundled, "fake")
    # The autouse test fixture already bootstrapped the real defaults; start clean.
    shutil.rmtree(data_dir / "plugins", ignore_errors=True)
    manager = PluginManager(config, bundled=bundled)

    copied = manager.bootstrap()
    manager.load(enabled=["fake"])

    target = data_dir / "plugins" / "fake" / "plugin.py"
    assert copied == ["fake"]
    assert target.is_file()
    assert "fake" in manager.plugins
    assert "fake_tool" in tool_names()


def test_bootstrap_does_not_overwrite_user_plugins(config, data_dir, tmp_path):
    bundled = tmp_path / "bundled"
    _write_plugin(bundled, "fake")
    shutil.rmtree(data_dir / "plugins", ignore_errors=True)
    user_plugin = data_dir / "plugins" / "fake" / "plugin.py"
    user_plugin.parent.mkdir(parents=True)
    user_plugin.write_text("# user edited", encoding="utf-8")
    manager = PluginManager(config, bundled=bundled)

    copied = manager.bootstrap()

    assert copied == []
    assert user_plugin.read_text(encoding="utf-8") == "# user edited"
