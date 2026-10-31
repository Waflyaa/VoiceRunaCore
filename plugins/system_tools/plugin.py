"""system_tools: Windows volume control and sandboxed file creation."""

from __future__ import annotations

import asyncio

from voiceruna.logging_setup import get_logger
from voiceruna.plugins.base import BasePlugin
from voiceruna.security import ValidationError
from voiceruna.tools.base import ToolResult

__all__ = ["SystemToolsPlugin"]

log = get_logger("plugins.system")

VOLUME_ACTIONS = {"up", "down", "mute", "unmute", "set"}


class SystemToolsPlugin(BasePlugin):
    name = "system_tools"
    title = "Громкость и файлы"
    description = "Управление громкостью Windows и создание файлов в песочнице."

    def __init__(self, app=None) -> None:
        super().__init__(app)
        self._volume = None
        self._files = None

    def _volume_module(self):
        if self._volume is None:
            self._volume = self.load_helper("volume.py", "vox_plugin_system_volume")
        return self._volume

    def _files_module(self):
        if self._files is None:
            self._files = self.load_helper("files.py", "vox_plugin_system_files")
        return self._files

    def get_tools(self):
        return [
            {
                "name": "set_volume",
                "description": "Change the Windows output volume: up, down, mute, unmute or set a level.",
                "parameters": {
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
                "handler": self._set_volume,
            },
            {
                "name": "create_file",
                "description": "Create a .txt or .md file inside %USERPROFILE%\\Documents\\VoiceRunaCore.",
                "parameters": {
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
                "handler": self._create_file,
            },
        ]

    async def _set_volume(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, {"action", "level"})
        action = args.get("action")
        if not isinstance(action, str) or action.strip().lower() not in VOLUME_ACTIONS:
            raise ValidationError("action: up, down, mute, unmute или set")
        action = action.strip().lower()
        level = args.get("level")
        if action == "set":
            if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
                raise ValidationError("level: целое число от 0 до 100")
        elif level is not None:
            raise ValidationError("level допустим только для action=set")

        try:
            percent, muted = await asyncio.to_thread(self._volume_module().apply, action, level)
        except Exception as exc:  # noqa: BLE001 - audio driver may vanish
            log.warning("громкость недоступна: %s", type(exc).__name__)
            return ToolResult(ok=False, speech="Не могу изменить громкость",
                              error=f"volume_unavailable: {type(exc).__name__}")
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

    async def _create_file(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, {"filename", "content"})
        filename = args.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise ValidationError("не задано имя файла")
        content = args.get("content")
        if content is not None and not isinstance(content, str):
            raise ValidationError("content должен быть строкой")
        try:
            path = await asyncio.to_thread(self._files_module().create_text_file, filename, content)
        except ValueError as exc:
            return ToolResult(ok=False, speech=str(exc), error="refused")
        except OSError:
            log.warning("не удалось создать файл", exc_info=True)
            return ToolResult(ok=False, speech="Не удалось создать файл", error="os_error")
        return ToolResult(ok=True, speech=f"Создал файл {path.name}",
                          payload={"filename": path.name, "path": str(path)})
