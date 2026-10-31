"""Tool executor: validates arguments and dispatches to ToolRegistry handlers.

The microkernel keeps this file as the transport layer only. Every tool lives
in a plugin (plugins/<name>/plugin.py) and registers itself in ToolRegistry at
startup via PluginManager. Async handlers get this executor as their context so
they can read config/apps, resolve folders and start processes.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging_setup import get_logger
from ..security import ValidationError
from .base import ToolResult
from .registry import REGISTRY

if TYPE_CHECKING:  # pragma: no cover
    from ..apps import AppsRegistry
    from ..config import ConfigManager

__all__ = ["ToolExecutor", "ToolResult", "tool_names"]

logger = get_logger("tools")

DEFAULT_FOLDER_IDS = ["desktop", "downloads", "documents"]
_FALLBACK_PHRASE = "не могу это сделать"


def tool_names() -> tuple[str, ...]:
    """Names registered by the plugins (call after PluginManager.load())."""
    return REGISTRY.names()


class ToolExecutor:
    """Argument validation and dispatch for all plugin-registered tools."""

    def __init__(self, config: "ConfigManager", apps: "AppsRegistry") -> None:
        self._config = config
        self._apps = apps

    @property
    def config(self) -> "ConfigManager":
        return self._config

    @property
    def apps(self) -> "AppsRegistry":
        return self._apps

    def app_ids(self) -> list[str]:
        return self._apps.ids()

    def specs(self) -> list[dict]:
        """OpenAI tools schema from whatever the enabled plugins registered.

        The launch_application enum is filled from the live apps registry so a
        freshly added program shows up in the LLM schema immediately.
        """
        specs = REGISTRY.specs()
        ids = self.app_ids()
        for spec in specs:
            function = spec.get("function") or {}
            if function.get("name") == "launch_application":
                properties = function.setdefault("parameters", {}).setdefault("properties", {})
                properties.setdefault("application_id", {})["enum"] = ids
        return specs

    async def execute(self, name: str, args: dict) -> ToolResult:
        """Validate and run one tool call; never raises."""
        try:
            if not isinstance(name, str):
                return self.refuse("имя инструмента должно быть строкой")
            if not REGISTRY.has(name):
                return self.refuse(f"неизвестный инструмент: {name!r}")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return self.refuse("аргументы должны быть объектом")
            return await REGISTRY.run(name, self, args)
        except ValidationError as exc:
            return self.refuse(str(exc))
        except Exception as exc:  # noqa: BLE001 - an error must never escape a tool
            logger.exception("инструмент %s завершился ошибкой", name)
            return self.refuse(type(exc).__name__)

    # ------------------------------------------------------------------ helpers
    def folder_ids(self) -> list[str]:
        allowed = self._config.get("folders.allowed", DEFAULT_FOLDER_IDS)
        if not isinstance(allowed, list) or not allowed:
            return list(DEFAULT_FOLDER_IDS)
        return [str(item) for item in allowed]

    @staticmethod
    def reject_unknown_args(args: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValidationError(f"лишний аргумент: {unknown[0]}")

    @staticmethod
    def launchable(executable: str) -> bool:
        target = executable.strip()
        if not target:
            return False
        absolute = os.path.isabs(target) or bool(Path(target).drive)
        if absolute or "\\" in target or "/" in target:
            return Path(target).exists()
        return True

    @staticmethod
    async def startfile(target: str) -> None:
        starter = getattr(os, "startfile", None)
        if starter is None:  # pragma: no cover - non-Windows
            raise OSError("os.startfile is not available")
        await asyncio.to_thread(starter, target)

    @staticmethod
    async def popen(command: list[str]) -> None:
        await asyncio.to_thread(subprocess.Popen, command, shell=False)

    def refuse(self, reason: str) -> ToolResult:
        return ToolResult(ok=False, speech=self.phrase(), error=reason)

    def failure(self, error: str, exc: BaseException) -> ToolResult:
        logger.warning("ошибка инструмента: %s: %s", error, type(exc).__name__)
        return ToolResult(ok=False, speech=self.phrase(), error=f"{error}: {type(exc).__name__}")

    def phrase(self) -> str:
        phrase = self._config.get("errors.tool_refused_phrase", _FALLBACK_PHRASE)
        return str(phrase) if isinstance(phrase, str) and phrase.strip() else _FALLBACK_PHRASE
