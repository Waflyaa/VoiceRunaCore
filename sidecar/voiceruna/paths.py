"""Filesystem locations used by the sidecar.

Everything the assistant writes lives under %APPDATA%\\VoiceRunaCore (see TZ §13):
config.json, .env, apps.json, voices.json, models/, logs/.
The installation directory is never written to.

Tests and portable runs can override the data root with VOX_DATA_DIR.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "VoiceRunaCore"
#: Previous product name: its data folder is migrated once on the first start.
LEGACY_APP_NAME = "VoxRunaCore"
#: What is copied from the legacy folder (models are large: avoid re-downloading).
LEGACY_ITEMS = ("config.json", ".env", "apps.json", "models")


def _migrate_legacy(base: Path, target: Path) -> None:
    """Move user data from %APPDATA%\VoxRunaCore on the first run.

    Copies config.json, .env, apps.json and the models folder, then removes the
    legacy folder. A half-written target is never overwritten, and failures keep
    the legacy folder in place instead of losing the user's settings.
    """
    legacy = base / LEGACY_APP_NAME
    try:
        if not legacy.is_dir() or legacy == target:
            return
        if (target / "config.json").exists():
            return
        copied = 0
        for name in LEGACY_ITEMS:
            source = legacy / name
            if not source.exists():
                continue
            destination = target / name
            if destination.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
            copied += 1
        if copied and (target / "config.json").exists():
            shutil.rmtree(legacy, ignore_errors=True)
    except OSError:
        # Never block startup because of a migration problem.
        return


def data_dir() -> Path:
    """Return (and create) the per-user data directory."""
    override = os.environ.get("VOX_DATA_DIR")
    base = override or os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    path = Path(base) / APP_NAME if not override else Path(base)
    path.mkdir(parents=True, exist_ok=True)
    if not override:
        _migrate_legacy(Path(base), path)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"


def env_path() -> Path:
    return data_dir() / ".env"


def apps_path() -> Path:
    return data_dir() / "apps.json"


def voices_path() -> Path:
    return data_dir() / "voices.json"


def voices_dir() -> Path:
    """Drop-in folder for third-party Piper voices (*.onnx + *.onnx.json)."""
    path = data_dir() / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    path = data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_dir() -> Path:
    """Directory with bundled read-only resources (PyInstaller onedir aware)."""
    if getattr(sys, "frozen", False):
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            return Path(bundled)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
