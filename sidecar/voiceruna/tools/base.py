"""Shared tool result type (TZ §8).

Every tool returns a ToolResult: the runtime speaks 'speech', emits the
payload into the UI event and never lets an exception escape a tool call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ToolResult"]


@dataclass
class ToolResult:
    """Outcome of one tool call."""

    ok: bool
    speech: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
