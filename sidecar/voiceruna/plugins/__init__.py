"""Microkernel plugin system: base class and manager."""

from __future__ import annotations

from .base import BasePlugin
from .manager import PluginManager

__all__ = ["BasePlugin", "PluginManager"]
