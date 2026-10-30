"""The five tools of TZ §8 and their executor.

Only these tools exist: launch_application, open_folder, open_file, get_time,
get_weather. Arguments are validated before anything runs; anything unknown or
invalid becomes ToolResult(ok=False, speech=<errors.tool_refused_phrase>).
Blocking calls (subprocess.Popen, os.startfile) run in asyncio.to_thread and
never reach a shell.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import security
from ..logging_setup import get_logger
from ..security import ValidationError
from . import files, volume
from .base import ToolResult
from .registry import REGISTRY, register_tool
from .weather import WeatherService

if TYPE_CHECKING:  # pragma: no cover
    from ..apps import AppsRegistry
    from ..config import ConfigManager

__all__ = ["ToolExecutor", "ToolResult", "WeatherService", "TOOL_NAMES"]

logger = get_logger("tools")

TOOL_NAMES = ("launch_application", "open_folder", "open_file", "get_time", "get_weather")

DEFAULT_FOLDER_IDS = ["desktop", "downloads", "documents"]

FOLDER_LABELS = {
    "desktop": "Рабочий стол",
    "downloads": "Загрузки",
    "documents": "Документы",
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_FALLBACK_PHRASE = "не могу это сделать"


class ToolExecutor:
    """Validates arguments and runs the five whitelisted tools."""

    def __init__(self, config: ConfigManager, apps: AppsRegistry) -> None:
        self._config = config
        self._apps = apps
        self._weather = WeatherService(config)

    # ------------------------------------------------------------------ metadata
    def app_ids(self) -> list[str]:
        return self._apps.ids()

    def specs(self) -> list[dict]:
        """OpenAI tools JSON schema for exactly the five tools of TZ §8."""
        folder_ids = self._folder_ids()
        return [
            {
                "type": "function",
                "function": {
                    "name": "launch_application",
                    "description": "Launch a registered application by its id.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "application_id": {
                                "type": "string",
                                "description": "Application id from the registry.",
                                "enum": self.app_ids(),
                            }
                        },
                        "required": ["application_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "open_folder",
                    "description": "Open one of the user's known folders.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_id": {
                                "type": "string",
                                "description": "Known folder id.",
                                "enum": folder_ids,
                            }
                        },
                        "required": ["folder_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "open_file",
                    "description": "Open a file that lives in one of the known folders.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_id": {
                                "type": "string",
                                "description": "Known folder id.",
                                "enum": folder_ids,
                            },
                            "filename": {
                                "type": "string",
                                "description": "Plain file name, without a path.",
                            },
                        },
                        "required": ["folder_id", "filename"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Current local time.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "City name; the default city when omitted.",
                                "maxLength": 100,
                            }
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
        ] + REGISTRY.specs()

    # ------------------------------------------------------------------ dispatch
    async def execute(self, name: str, args: dict) -> ToolResult:
        """Validate and run one tool call; never raises."""
        try:
            if not isinstance(name, str):
                return self._refuse("имя инструмента должно быть строкой")
            if REGISTRY.has(name):
                return await REGISTRY.run(name, self, args or {})
            if name not in TOOL_NAMES:
                return self._refuse(f"неизвестный инструмент: {name!r}")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return self._refuse("аргументы должны быть объектом")
            if name == "launch_application":
                return await self._launch_application(args)
            if name == "open_folder":
                return await self._open_folder(args)
            if name == "open_file":
                return await self._open_file(args)
            if name == "get_time":
                return await self._get_time(args)
            return await self._get_weather(args)
        except ValidationError as exc:
            return self._refuse(str(exc))
        except Exception as exc:  # noqa: BLE001 - an error must never escape a tool
            logger.exception("инструмент %s завершился ошибкой", name)
            return self._refuse(type(exc).__name__)

    # ------------------------------------------------------------------ tools
    async def _launch_application(self, args: dict[str, Any]) -> ToolResult:
        self._reject_unknown_args(args, {"application_id"})
        application_id = args.get("application_id")
        if not isinstance(application_id, str) or not application_id.strip():
            raise ValidationError("не задан application_id")
        application_id = application_id.strip()

        entry = self._apps.get(application_id)
        if entry is None:
            raise ValidationError(f"программа не найдена: {application_id}")
        display_name = str(entry.get("display_name") or application_id)

        uri = entry.get("uri")
        if uri is not None:
            allowed_schemes = self._config.get("apps.allowed_uri_schemes", ["steam:"])
            target = security.validate_uri(str(uri), allowed_schemes)
            try:
                await self._startfile(target)
            except OSError as exc:
                return self._failure("os_error", exc)
            return ToolResult(
                ok=True,
                speech=f"Открываю {display_name}",
                payload={"application_id": application_id, "uri": target},
            )

        executable, arguments = security.expand_app_command(entry)
        if not self._launchable(executable):
            return ToolResult(
                ok=True,
                speech=f"Не нашёл {display_name}",
                payload={"application_id": application_id, "found": False},
            )
        try:
            await asyncio.to_thread(subprocess.Popen, [executable, *arguments], shell=False)
        except FileNotFoundError:
            return ToolResult(
                ok=True,
                speech=f"Не нашёл {display_name}",
                payload={"application_id": application_id, "found": False},
            )
        except OSError as exc:
            return self._failure("os_error", exc)
        return ToolResult(
            ok=True,
            speech=f"Открываю {display_name}",
            payload={"application_id": application_id, "command": [executable, *arguments]},
        )

    async def _open_folder(self, args: dict[str, Any]) -> ToolResult:
        self._reject_unknown_args(args, {"folder_id"})
        folder_id = security.validate_folder_id(args.get("folder_id", ""), self._folder_ids())
        path = security.known_folder(folder_id)
        label = FOLDER_LABELS.get(folder_id, folder_id)
        try:
            await self._startfile(str(path))
        except OSError as exc:
            return self._failure("os_error", exc)
        return ToolResult(
            ok=True,
            speech=f"Открываю {label}",
            payload={"folder_id": folder_id, "path": str(path)},
        )

    async def _open_file(self, args: dict[str, Any]) -> ToolResult:
        self._reject_unknown_args(args, {"folder_id", "filename"})
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise ValidationError("не задан filename")
        path = security.resolve_file(
            args.get("folder_id", ""),
            filename,
            allowed_folders=self._folder_ids(),
            allowed_extensions=self._config.get("files.allowed_extensions", []),
            lnk_allowed_folders=self._config.get("files.lnk_allowed_folders", ["desktop"]),
        )
        if not path.exists():
            return ToolResult(ok=False, speech=f"Не нашёл {path.name}", error="not_found")
        try:
            await self._startfile(str(path))
        except OSError as exc:
            return self._failure("os_error", exc)
        return ToolResult(
            ok=True,
            speech=f"Открываю {path.name}",
            payload={"folder_id": str(args.get("folder_id", "")), "path": str(path)},
        )

    async def _get_time(self, args: dict[str, Any]) -> ToolResult:
        self._reject_unknown_args(args, set())
        now = datetime.now()
        return ToolResult(
            ok=True,
            speech=f"Сейчас {now:%H:%M}",
            payload={"time": f"{now:%H:%M}"},
        )

    async def _get_weather(self, args: dict[str, Any]) -> ToolResult:
        self._reject_unknown_args(args, {"city"})
        city = args.get("city")
        if city is not None and not isinstance(city, str):
            raise ValidationError("city должен быть строкой")
        if isinstance(city, str) and len(city.strip()) > 100:
            raise ValidationError("слишком длинное название города")
        return await self._weather.forecast(city)

    # ------------------------------------------------------------------ helpers
    def _folder_ids(self) -> list[str]:
        allowed = self._config.get("folders.allowed", DEFAULT_FOLDER_IDS)
        if not isinstance(allowed, list) or not allowed:
            return list(DEFAULT_FOLDER_IDS)
        return [str(item) for item in allowed]

    @staticmethod
    def _reject_unknown_args(args: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValidationError(f"лишний аргумент: {unknown[0]}")

    @staticmethod
    def _launchable(executable: str) -> bool:
        """A bare executable name is resolved through PATH by Popen."""
        target = executable.strip()
        if not target:
            return False
        if os.path.isabs(target) or _DRIVE_RE.match(target) or "\\" in target or "/" in target:
            return Path(target).exists()
        return True

    @staticmethod
    async def _startfile(target: str) -> None:
        starter = getattr(os, "startfile", None)
        if starter is None:  # pragma: no cover - non-Windows
            raise OSError("os.startfile is not available")
        await asyncio.to_thread(starter, target)

    def _refuse(self, reason: str) -> ToolResult:
        return ToolResult(ok=False, speech=self._phrase(), error=reason)

    def _failure(self, error: str, exc: BaseException) -> ToolResult:
        logger.warning("ошибка инструмента: %s: %s", error, type(exc).__name__)
        return ToolResult(ok=False, speech=self._phrase(), error=f"{error}: {type(exc).__name__}")

    def _phrase(self) -> str:
        phrase = self._config.get("errors.tool_refused_phrase", _FALLBACK_PHRASE)
        return str(phrase) if isinstance(phrase, str) and phrase.strip() else _FALLBACK_PHRASE

# ---------------------------------------------------------------------- v2 tools
# Registered through ToolRegistry: one spec + one handler per tool.
_VOLUME_ACTIONS = {"up", "down", "mute", "unmute", "set"}


async def _set_volume_tool(executor: ToolExecutor, args: dict[str, Any]) -> ToolResult:
    executor._reject_unknown_args(args, {"action", "level"})
    action = args.get("action")
    if not isinstance(action, str) or action.strip().lower() not in _VOLUME_ACTIONS:
        raise ValidationError("action: up, down, mute, unmute или set")
    action = action.strip().lower()

    level = args.get("level")
    if action == "set":
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
            raise ValidationError("level: целое число от 0 до 100")
    elif level is not None:
        raise ValidationError("level допустим только для action=set")

    try:
        percent, muted = await asyncio.to_thread(volume.apply, action, level)
    except Exception as exc:  # noqa: BLE001 - audio driver may vanish
        logger.warning("громкость недоступна: %s", type(exc).__name__)
        return ToolResult(
            ok=False,
            speech="Не могу изменить громкость",
            error=f"volume_unavailable: {type(exc).__name__}",
        )

    if action == "mute":
        speech = "Звук выключен"
    elif action == "unmute":
        speech = "Звук включён"
    elif action == "set":
        speech = f"Громкость {percent}%"
    elif action == "up":
        speech = "Сделал громче"
    else:
        speech = "Сделал тише"
    return ToolResult(ok=True, speech=speech, payload={"percent": percent, "muted": muted})


async def _create_file_tool(executor: ToolExecutor, args: dict[str, Any]) -> ToolResult:
    executor._reject_unknown_args(args, {"filename", "content"})
    filename = args.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValidationError("не задано имя файла")
    content = args.get("content")
    if content is not None and not isinstance(content, str):
        raise ValidationError("content должен быть строкой")
    try:
        path = await asyncio.to_thread(files.create_text_file, filename, content)
    except ValueError as exc:
        return ToolResult(ok=False, speech=str(exc), error="refused")
    except OSError as exc:
        logger.warning("не удалось создать файл: %s", type(exc).__name__)
        return ToolResult(ok=False, speech="Не удалось создать файл", error="os_error")
    return ToolResult(
        ok=True,
        speech=f"Создал файл {path.name}",
        payload={"filename": path.name, "path": str(path)},
    )


register_tool(
    "set_volume",
    "Change the Windows output volume: up, down, mute, unmute or set an exact level.",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["up", "down", "mute", "unmute", "set"],
                "description": "up/down change by 10%; set needs level.",
            },
            "level": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Target volume in percent (action=set only).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    _set_volume_tool,
)

register_tool(
    "create_file",
    "Create a .txt or .md file inside %USERPROFILE%\\Documents\\VoiceRunaCore.",
    {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Bare file name with a .txt or .md suffix, no path.",
            },
            "content": {
                "type": "string",
                "description": "Optional text to put in the file.",
            },
        },
        "required": ["filename"],
        "additionalProperties": False,
    },
    _create_file_tool,
)

TOOL_NAMES = (*TOOL_NAMES, *REGISTRY.names())
