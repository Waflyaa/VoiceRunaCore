"""basic_info: time and weather."""

from __future__ import annotations

from datetime import datetime

from voiceruna.logging_setup import get_logger
from voiceruna.plugins.base import BasePlugin
from voiceruna.tools.base import ToolResult

__all__ = ["BasicInfoPlugin"]

log = get_logger("plugins.basic")


class BasicInfoPlugin(BasePlugin):
    name = "basic_info"
    title = "Время и погода"
    description = "Текущее время и прогноз погоды."

    def __init__(self, app=None) -> None:
        super().__init__(app)
        self._weather_module = None
        self._service = None

    def _weather(self):
        if self._service is None:
            if self._weather_module is None:
                self._weather_module = self.load_helper("weather.py", "vox_plugin_basic_weather")
            config = getattr(self.app, "config", None)
            if config is None:
                config = getattr(self.app, "_config", None)
            if config is None:
                from voiceruna.config import ConfigManager

                config = ConfigManager()
                config.load()
            self._service = self._weather_module.WeatherService(config)
        return self._service

    def get_tools(self):
        return [
            {
                "name": "get_time",
                "description": "Current local time.",
                "parameters": {"type": "object", "properties": {}, "required": [],
                               "additionalProperties": False},
                "handler": self._get_time,
            },
            {
                "name": "get_weather",
                "description": "Current weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name; default when omitted.",
                                 "maxLength": 100}
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "handler": self._get_weather,
            },
        ]

    async def _get_time(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, set())
        now = datetime.now()
        return ToolResult(ok=True, speech=f"Сейчас {now:%H:%M}", payload={"time": f"{now:%H:%M}"})

    async def _get_weather(self, executor, args: dict) -> ToolResult:
        executor.reject_unknown_args(args, {"city"})
        city = args.get("city")
        if city is not None and not isinstance(city, str):
            raise ValueError("city должен быть строкой")
        if isinstance(city, str) and len(city.strip()) > 100:
            raise ValueError("слишком длинное название города")
        service = self._weather()
        if executor.config is not None and service._config is not executor.config:  # type: ignore[attr-defined]
            service._config = executor.config  # type: ignore[attr-defined]
        return await service.forecast(city)
