"""工具（Tool）基础设施。

Agent 的能力边界由工具决定：LLM 负责「思考调用哪个工具」，工具负责「真实执行」。
本模块提供：

* ``ToolResult`` —— 工具的统一返回结构（成功/失败、文本、结构化数据）
* ``Tool`` —— 工具描述（名称、说明、JSON Schema、执行函数、安全标记）
* ``ToolRegistry`` —— 注册、查找、导出 OpenAI 工具的 JSON Schema、统一调度

JSON Schema 既用于喂给 LLM 做 Function Calling，也用于工具侧参数校验，
避免模型给出缺参/错参导致崩溃。
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool
    content: str
    data: Optional[Dict[str, Any]] = None
    error: str = ""
    duration_ms: int = 0

    def to_payload(self) -> str:
        """序列化为回填给 LLM 的字符串（限制长度，防止撑爆上下文）。"""
        body = {"ok": self.ok, "result": self.content}
        if self.error:
            body["error"] = self.error
        if self.data:
            body["data"] = self.data
        text = json.dumps(body, ensure_ascii=False, indent=None)
        if len(text) > 12000:
            text = text[:12000] + " ...(内容过长已截断)"
        return text

    @classmethod
    def fail(cls, message: str, **data: Any) -> "ToolResult":
        return cls(ok=False, content="", error=message, data=data or None)

    @classmethod
    def success(cls, content: str, **data: Any) -> "ToolResult":
        return cls(ok=True, content=content, data=data or None)


@dataclass
class Tool:
    """一个可被 Agent 调用的工具。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[..., ToolResult]
    requires_exec: bool = False
    dangerous: bool = False
    examples: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, arguments: Dict[str, Any]) -> Optional[str]:
        """轻量级 JSON Schema 校验，返回错误信息（None 表示通过）。"""
        schema = self.parameters or {}
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        for key in required:
            if key not in arguments or arguments[key] in (None, ""):
                return f"缺少必填参数 `{key}`"
        for key, value in arguments.items():
            spec = properties.get(key)
            if spec is None:
                continue
            expected = spec.get("type")
            if expected == "integer" and not isinstance(value, int):
                if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    continue
                return f"参数 `{key}` 应为整数，收到 {type(value).__name__}"
            if expected == "string" and not isinstance(value, str):
                return f"参数 `{key}` 应为字符串，收到 {type(value).__name__}"
            if expected == "boolean" and not isinstance(value, bool):
                return f"参数 `{key}` 应为布尔值，收到 {type(value).__name__}"
        return None


class ToolRegistry:
    """工具注册表。"""

    def __init__(self, *, enable_exec: bool = True):
        self._tools: Dict[str, Tool] = {}
        self.enable_exec = enable_exec

    # ------------------------------------------------------------------ #
    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        *,
        requires_exec: bool = False,
        dangerous: bool = False,
        examples: Optional[List[str]] = None,
    ) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
        """装饰器写法注册工具。"""

        def decorator(func: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    func=func,
                    requires_exec=requires_exec,
                    dangerous=dangerous,
                    examples=examples or [],
                )
            )
            return func

        return decorator

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        *,
        requires_exec: bool = False,
        dangerous: bool = False,
        examples: Optional[List[str]] = None,
    ) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
        """``tool`` 的显式别名，语义更直观：注册并返回装饰器。"""
        return self.tool(
            name,
            description,
            parameters,
            requires_exec=requires_exec,
            dangerous=dangerous,
            examples=examples,
        )

    # ------------------------------------------------------------------ #
    def available(self) -> List[Tool]:
        """当前配置下真正可用的工具（例如禁用执行时自动隐藏执行类工具）。"""
        return [
            tool
            for tool in self._tools.values()
            if not (tool.requires_exec and not self.enable_exec)
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self.available()]

    def names(self) -> List[str]:
        return [tool.name for tool in self.available()]

    # ------------------------------------------------------------------ #
    def execute(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        """执行工具，任何异常都被收敛为 ``ToolResult``，绝不中断 Agent 循环。"""
        tool = self._tools.get(name)  # 用全量表查找，才能区分「不存在」与「被禁用」
        if tool is None:
            hint = f"可用工具：{', '.join(self.names())}"
            return ToolResult.fail(f"未知工具 `{name}`。{hint}")
        if tool.requires_exec and not self.enable_exec:
            return ToolResult.fail(
                f"工具 `{name}` 已被禁用：当前 CODEAGENT_ENABLE_EXEC=false，出于安全考虑不允许执行代码。"
                "请改用静态分析类工具（static_analyze / detect_code_smells）来获取信息。"
            )

        invalid = tool.validate(arguments or {})
        if invalid:
            return ToolResult.fail(f"参数校验失败：{invalid}")

        try:
            result = tool.func(**(arguments or {}))
            if not isinstance(result, ToolResult):  # 容错：工具误返回字符串
                result = ToolResult.success(str(result))
            return result
        except TypeError as exc:
            return ToolResult.fail(f"工具 `{name}` 调用参数不匹配：{exc}")
        except Exception as exc:  # noqa: BLE001 - Agent 必须容错
            return ToolResult.fail(
                f"工具 `{name}` 执行异常：{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc()[-1500:],
            )

    def catalog_text(self) -> str:
        """人类可读的工具清单（用于 README / CLI 展示）。"""
        lines = []
        for tool in self.available():
            flag = " ⚠️危险" if tool.dangerous else ""
            lines.append(f"- `{tool.name}`{flag}：{tool.description.splitlines()[0]}")
        return "\n".join(lines)
