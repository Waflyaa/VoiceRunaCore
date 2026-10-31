"""apps_launcher: launch registered applications and open known folders."""

from __future__ import annotations

from voiceruna import security
from voiceruna.logging_setup import get_logger
from voiceruna.plugins.base import BasePlugin
from voiceruna.security import ValidationError
from voiceruna.tools.base import ToolResult

__all__ = ["AppsLauncherPlugin"]

log = get_logger("plugins.apps")

FOLDER_LABELS = {
    "desktop": "Рабочий стол",
    "downloads": "Загрузки",
    "documents": "Документы",
}


class AppsLauncherPlugin(BasePlugin):
    name = "apps_launcher"

    def get_tools(self):
        return [
            {
                "name": "launch_application",
                "description": "Launch a registered application by its id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "application_id": {
                            "type": "string",
                            "description": "Application id from apps.json.",
                        }
                    },
                    "required": ["application_id"],
                    "additionalProperties": False,
                },
                "handler": self._launch_application,
            },
            {
                "name": "open_file",
                "description": "Open a file that lives in one of the known folders.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder_id": {"type": "string", "description": "Known folder id."},
                        "filename": {"type": "string",
                                     "description": "Plain file name, without a path."},
                    },
                    "required": ["folder_id", "filename"],
                    "additionalProperties": False,
                },
                "handler": self._open_file,
            },
            {
                "name": "open_folder",
                "description": "Open one of the user's known folders.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder_id": {
                            "type": "string",
                            "description": "Known folder id: desktop, downloads, documents.",
                        }
                    },
                    "required": ["folder_id"],
                    "additionalProperties": False,
                },
                "handler": self._open_folder,
            },
        ]

    async def _launch_application(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, {"application_id"})
        application_id = args.get("application_id")
        if not isinstance(application_id, str) or not application_id.strip():
            raise ValidationError("не задан application_id")
        application_id = application_id.strip()

        entry = executor.apps.get(application_id)
        if entry is None:
            raise ValidationError(f"программа не найдена: {application_id}")
        display_name = str(entry.get("display_name") or application_id)

        uri = entry.get("uri")
        if uri is not None:
            allowed = executor.config.get("apps.allowed_uri_schemes", ["steam:"])
            target = security.validate_uri(str(uri), allowed)
            try:
                await executor.startfile(target)
            except OSError as exc:
                return executor.failure("os_error", exc)
            return ToolResult(
                ok=True,
                speech=f"Открываю {display_name}",
                payload={"application_id": application_id, "uri": target},
            )

        executable, arguments = security.expand_app_command(entry)
        if not executor.launchable(executable):
            return ToolResult(ok=True, speech=f"Не нашёл {display_name}",
                              payload={"application_id": application_id, "found": False})
        try:
            await executor.popen([executable, *arguments])
        except FileNotFoundError:
            return ToolResult(ok=True, speech=f"Не нашёл {display_name}",
                              payload={"application_id": application_id, "found": False})
        except OSError as exc:
            return executor.failure("os_error", exc)
        return ToolResult(
            ok=True,
            speech=f"Открываю {display_name}",
            payload={"application_id": application_id, "command": [executable, *arguments]},
        )



    async def _open_file(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, {"folder_id", "filename"})
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise ValidationError("не задан filename")
        path = security.resolve_file(
            args.get("folder_id", ""),
            filename,
            allowed_folders=executor.folder_ids(),
            allowed_extensions=executor.config.get("files.allowed_extensions", []),
            lnk_allowed_folders=executor.config.get("files.lnk_allowed_folders", ["desktop"]),
        )
        if not path.exists():
            return ToolResult(ok=False, speech=f"Не нашёл {path.name}", error="not_found")
        try:
            await executor.startfile(str(path))
        except OSError as exc:
            return executor.failure("os_error", exc)
        return ToolResult(ok=True, speech=f"Открываю {path.name}",
                          payload={"folder_id": str(args.get("folder_id", "")),
                                   "path": str(path)})

    async def _open_folder(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, {"folder_id"})
        folder_id = security.validate_folder_id(args.get("folder_id", ""), executor.folder_ids())
        path = security.known_folder(folder_id)
        label = FOLDER_LABELS.get(folder_id, folder_id)
        try:
            await executor.startfile(str(path))
        except OSError as exc:
            return executor.failure("os_error", exc)
        return ToolResult(ok=True, speech=f"Открываю {label}",
                          payload={"folder_id": folder_id, "path": str(path)})
