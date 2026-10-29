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


@pytest.fixture()
def apps(config):
    from voiceruna.apps import AppsRegistry

    registry = AppsRegistry(config.env_path().parent / "apps.json")
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
