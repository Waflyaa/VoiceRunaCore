"""Shared pytest fixtures for the sidecar test suite.

Run tests from the sidecar directory:
    cd sidecar
    python -m pytest -q

Fixtures use a temporary VOX_DATA_DIR so no real user config is touched, and
import project modules lazily so a missing module in one area cannot break the
collection of another area's tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SIDECAR_DIR = Path(__file__).resolve().parents[1]
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated %APPDATA%\\VoiceRunaCore replacement."""
    directory = tmp_path / "appdata" / "VoiceRunaCore"
    directory.mkdir(parents=True)
    monkeypatch.setenv("VOX_DATA_DIR", str(directory))
    return directory


@pytest.fixture()
def config(data_dir: Path):
    from voiceruna.config import ConfigManager

    manager = ConfigManager(data_dir / "config.json")
    manager.load()
    return manager


TEST_APPS = {
    "dota2": {"display_name": "Dota 2", "uri": "steam://rungameid/570", "aliases": ["дота", "доту"]},
    "factorio": {"display_name": "Factorio", "uri": "steam://rungameid/427520", "aliases": ["факторио"]},
    "steam": {"display_name": "Steam", "path": "steam.exe", "aliases": ["стим"]},
    "discord": {"display_name": "Discord", "path": "discord.exe", "aliases": ["дискорд"]},
    "telegram": {"display_name": "Telegram", "path": "telegram.exe", "aliases": ["телеграм"]},
    "vscode": {"display_name": "VS Code", "path": "code.exe", "aliases": ["вс код", "vs code", "вскод"]},
    "brave": {"display_name": "Brave", "path": "brave.exe", "aliases": ["браве", "brave"]},
    "explorer": {"display_name": "Проводник", "path": "explorer.exe", "aliases": ["проводник", "мои файлы"]},
    "notepad": {"display_name": "Блокнот", "path": "notepad.exe", "aliases": ["блокнот", "нотпад"]},
    "calc": {"display_name": "Калькулятор", "path": "calc.exe", "aliases": ["калькулятор"]},
}


@pytest.fixture()
def apps(config):
    """Registry with extra test apps (the shipped template is systems-only)."""
    import json

    from voiceruna.apps import AppsRegistry

    path = config.env_path().parent / "apps.json"
    path.write_text(json.dumps(TEST_APPS, ensure_ascii=False), encoding="utf-8")
    registry = AppsRegistry(path)
    registry.load()
    return registry


@pytest.fixture()
def events():
    """Collect emitted events by type."""
    from voiceruna.events import Event

    collected: list[Event] = []

    def sink(event: Event) -> None:
        collected.append(event)

    sink.collected = collected  # type: ignore[attr-defined]
    return sink


def seed_test_apps(directory) -> None:
    """Write the game-rich test registry before a runtime/pipeline is built."""
    import json
    from pathlib import Path as _Path

    folder = _Path(directory)
    (folder / "apps.json").write_text(json.dumps(TEST_APPS, ensure_ascii=False), encoding="utf-8")
