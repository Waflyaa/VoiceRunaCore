"""Tool transport (executor) and the plugin ToolRegistry."""

from __future__ import annotations

from .base import ToolResult
from .executor import ToolExecutor, tool_names
from .registry import REGISTRY, ToolRegistry, register_tool

__all__ = ["ToolResult", "ToolExecutor", "tool_names", "REGISTRY", "ToolRegistry", "register_tool"]
