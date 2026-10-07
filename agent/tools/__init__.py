"""工具层入口：统一构建 Agent 可用的工具集。"""

from __future__ import annotations

from pathlib import Path

from .base import Tool, ToolRegistry, ToolResult
from .exec_tools import register_exec_tools
from .local_tools import register_local_tools
from .report_tool import register_report_tool

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "register_local_tools",
    "register_exec_tools",
    "register_report_tool",
]


def build_registry(
    workspace: Path,
    *,
    enable_exec: bool = True,
    exec_timeout: int = 20,
) -> ToolRegistry:
    """构建完整的工具注册表。

    ``enable_exec=False`` 时不会注册执行类工具，模型即使想调用也无从调用，
    这是比「注册后再拒绝」更彻底的安全策略。
    """
    registry = ToolRegistry(enable_exec=enable_exec)
    register_local_tools(registry, workspace)
    register_report_tool(registry, workspace)
    if enable_exec:
        register_exec_tools(registry, workspace, exec_timeout=exec_timeout)
    return registry
