"""Сборка реестра инструментов."""
from __future__ import annotations

from .apps import TOOLS as APP_TOOLS
from .base import Param, Tool, ToolError, ToolRegistry
from .files import TOOLS as FILE_TOOLS
from .input import TOOLS as INPUT_TOOLS
from .shell import TOOLS as SHELL_TOOLS
from .system import TOOLS as SYSTEM_TOOLS
from .vision_tools import TOOLS as VISION_TOOLS
from .web import TOOLS as WEB_TOOLS
from .windows import TOOLS as WINDOW_TOOLS

__all__ = ["Tool", "Param", "ToolError", "ToolRegistry", "build_registry", "get_registry"]

_ALL = [*APP_TOOLS, *INPUT_TOOLS, *WINDOW_TOOLS, *VISION_TOOLS, *SYSTEM_TOOLS,
        *FILE_TOOLS, *SHELL_TOOLS, *WEB_TOOLS]

_registry: ToolRegistry | None = None


def build_registry() -> ToolRegistry:
    global _registry
    registry = ToolRegistry(_ALL)
    _registry = registry
    return registry


def get_registry() -> ToolRegistry:
    return _registry or build_registry()
