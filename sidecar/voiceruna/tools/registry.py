"""Small tool registry (v2): a single place where every tool is declared.

The five original tools keep their hand-written specs in executor.py; tools
added from v2 on (set_volume, create_file) are registered here so a new tool is
one register_tool() call plus its handler.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .base import ToolResult

__all__ = ["REGISTRY", "ToolRegistry", "register_tool"]

ToolHandler = Callable[[Any, dict[str, Any]], Awaitable[ToolResult]]


class ToolRegistry:
    """Name -> OpenAI tool spec + async handler."""

    def __init__(self) -> None:
        self._specs: dict[str, dict] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: dict, handler: ToolHandler) -> None:
        name = str(spec["function"]["name"])
        self._specs[name] = spec
        self._handlers[name] = handler

    def has(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def specs(self) -> list[dict]:
        return [self._specs[name] for name in self._specs]

    async def run(self, name: str, executor: Any, args: dict[str, Any]) -> ToolResult:
        return await self._handlers[name](executor, args)


REGISTRY = ToolRegistry()


def register_tool(name: str, description: str, parameters: dict, handler: ToolHandler) -> None:
    """Register one tool in OpenAI function-calling format."""
    REGISTRY.register(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        },
        handler,
    )
