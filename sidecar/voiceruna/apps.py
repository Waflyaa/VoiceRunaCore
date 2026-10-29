"""apps.json registry (TZ §9).

The registry maps a short application id to its launch command (either a
'path' or an 'uri'), a human-readable display name and the aliases the router
and the system prompt use. It lives in %APPDATA%\\VoiceRunaCore\\apps.json and is
created from DEFAULT_APPS on the first run; writes are atomic.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from . import paths
from .logging_setup import get_logger
from .security import normalize_alias

__all__ = ["DEFAULT_APPS", "AppsRegistry"]

logger = get_logger("apps")

# Template from TZ §9. Paths depend on the machine and are edited by the owner.
DEFAULT_APPS: dict[str, dict] = {
    "dota2": {
        "display_name": "Dota 2",
        "uri": "steam://rungameid/570",
        "aliases": ["дота", "доту", "доту 2", "dota", "dota2"],
    },
    "factorio": {
        "display_name": "Factorio",
        "uri": "steam://rungameid/427520",
        "aliases": ["факторио", "фактори"],
    },
    "steam": {
        "display_name": "Steam",
        "path": r"C:\Program Files (x86)\Steam\steam.exe",
        "aliases": ["стим"],
    },
    "discord": {
        "display_name": "Discord",
        "path": r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe",
        "aliases": ["дискорд"],
    },
    "telegram": {
        "display_name": "Telegram",
        "path": r"%APPDATA%\Telegram Desktop\Telegram.exe",
        "aliases": ["телеграм", "телега"],
    },
    "vscode": {
        "display_name": "VS Code",
        "path": r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        "aliases": ["вс код", "vs code", "вскод"],
    },
    "brave": {
        "display_name": "Brave",
        "path": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        "aliases": ["браве", "brave"],
    },
    "explorer": {
        "display_name": "Проводник",
        "path": "explorer.exe",
        "aliases": ["проводник", "мои файлы"],
    },
    "notepad": {
        "display_name": "Блокнот",
        "path": "notepad.exe",
        "aliases": ["блокнот", "нотпад"],
    },
    "calc": {
        "display_name": "Калькулятор",
        "path": "calc.exe",
        "aliases": ["калькулятор"],
    },
}

_APP_ID_RE = re.compile(r"^[a-z0-9_-]+$")

_ENTRY_KEYS = ("display_name", "path", "uri", "aliases")


def _validate_app_id(app_id: Any) -> str:
    if not isinstance(app_id, str) or not app_id.strip():
        raise ValueError("id программы должен быть непустой строкой")
    if app_id != app_id.strip():
        raise ValueError(f"id программы не должен содержать пробелы по краям: {app_id!r}")
    if not _APP_ID_RE.match(app_id):
        raise ValueError(f"id программы может содержать только a-z, 0-9, _ и -: {app_id!r}")
    return app_id


def _validate_entry(app_id: Any, entry: Any) -> dict[str, Any]:
    """Return a normalized copy of one registry entry or raise ValueError."""
    app_id = _validate_app_id(app_id)
    if not isinstance(entry, dict):
        raise ValueError(f"{app_id}: запись должна быть объектом")

    display_name = entry.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError(f"{app_id}: не задан display_name")
    display_name = display_name.strip()

    has_path = entry.get("path") is not None
    has_uri = entry.get("uri") is not None
    if has_path == has_uri:
        raise ValueError(f"{app_id}: нужен ровно один из path или uri")

    normalized: dict[str, Any] = {"display_name": display_name}
    if has_path:
        path = entry["path"]
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"{app_id}: path должен быть непустой строкой")
        normalized["path"] = path.strip()
    else:
        uri = entry["uri"]
        if not isinstance(uri, str) or not uri.strip():
            raise ValueError(f"{app_id}: uri должен быть непустой строкой")
        normalized["uri"] = uri.strip()

    aliases = entry.get("aliases", [])
    if aliases is None:
        aliases = []
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise ValueError(f"{app_id}: aliases должен быть списком строк")
    normalized["aliases"] = [alias.strip() for alias in aliases if alias.strip()]

    # Keep unknown extra keys so a hand-edited file is not silently truncated
    # by the settings editor; only the known keys are validated.
    for key, value in entry.items():
        if key not in _ENTRY_KEYS:
            normalized[key] = copy.deepcopy(value)

    return normalized


class AppsRegistry:
    """Thread-safe accessor for apps.json."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.apps_path()
        self._lock = threading.RLock()
        self._apps: dict[str, dict] = {}

    # ---------------------------------------------------------------- loading
    def load(self) -> dict[str, dict]:
        """Read apps.json, creating it from DEFAULT_APPS on the first run."""
        with self._lock:
            if not self._path.exists():
                self._apps = copy.deepcopy(DEFAULT_APPS)
                self._write_locked()
                return self.snapshot()

            try:
                raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("apps.json не читается, использую шаблон: %s", type(exc).__name__)
                self._apps = copy.deepcopy(DEFAULT_APPS)
                self._write_locked()
                return self.snapshot()

            if not isinstance(raw, dict):
                logger.warning("apps.json должен содержать объект, использую шаблон")
                self._apps = copy.deepcopy(DEFAULT_APPS)
                self._write_locked()
                return self.snapshot()

            apps: dict[str, dict] = {}
            for app_id, entry in raw.items():
                try:
                    apps[app_id] = _validate_entry(app_id, entry)
                except ValueError as exc:
                    logger.warning("apps.json: запись отброшена: %s", exc)
            self._apps = apps
            return self.snapshot()

    def save(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._apps, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # ---------------------------------------------------------------- access
    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._apps)

    def replace(self, data: dict[str, dict]) -> dict[str, dict]:
        """Validate a whole registry (PUT /apps), persist it and return it."""
        if not isinstance(data, dict):
            raise ValueError("apps.json должен быть объектом")
        cleaned: dict[str, dict] = {}
        for app_id, entry in data.items():
            cleaned[app_id] = _validate_entry(app_id, entry)
        with self._lock:
            self._apps = cleaned
            self._write_locked()
            return self.snapshot()

    def get(self, app_id: str) -> dict | None:
        with self._lock:
            entry = self._apps.get(app_id) if isinstance(app_id, str) else None
            return copy.deepcopy(entry) if entry is not None else None

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._apps)

    def aliases(self) -> dict[str, str]:
        """Return normalized alias -> app id (first owner wins on duplicates)."""
        with self._lock:
            result: dict[str, str] = {}
            for app_id, entry in self._apps.items():
                for alias in entry.get("aliases", []):
                    key = normalize_alias(alias)
                    if key and key not in result:
                        result[key] = app_id
            return result

    def describe_for_prompt(self) -> str:
        """Return 'dota2 (Dota 2), factorio (Factorio), ...' for the LLM prompt."""
        with self._lock:
            return ", ".join(
                f"{app_id} ({entry.get('display_name', app_id)})"
                for app_id, entry in self._apps.items()
            )
