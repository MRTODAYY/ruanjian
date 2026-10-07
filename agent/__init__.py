"""Code Review Agent —— 一个可解释、可扩展的代码审查 Agent。

模块结构::

    agent/
      config.py      配置加载（.env + 环境变量）
      llm.py         LLM 客户端（OpenAI 兼容 + 重试 + Mock）
      prompts.py     Prompt 模板集中管理
      analyzer.py    AST 静态分析引擎
      core.py        Agent 主循环
      tools/         工具集（文件、分析、执行、报告）
      cli.py         命令行入口
      web.py         零依赖 Web 界面
"""

from .config import Config
from .core import AgentRun, CodeReviewAgent, TraceEntry
from .llm import LLMClient, LLMError, LLMResponse, MockLLMClient, ToolCall
from .prompts import PromptLibrary
from .tools import Tool, ToolRegistry, ToolResult, build_registry

__version__ = "1.0.0"

__all__ = [
    "Config",
    "CodeReviewAgent",
    "AgentRun",
    "TraceEntry",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "MockLLMClient",
    "ToolCall",
    "PromptLibrary",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "__version__",
]
