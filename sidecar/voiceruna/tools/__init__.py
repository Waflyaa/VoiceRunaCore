"""Five tools (TZ §8) and the executor that validates their arguments."""

from __future__ import annotations

from .base import ToolResult
from .executor import TOOL_NAMES, ToolExecutor
from .weather import WeatherService

__all__ = ["ToolResult", "ToolExecutor", "WeatherService", "TOOL_NAMES"]
