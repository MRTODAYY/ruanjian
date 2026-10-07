"""LLM 客户端 —— OpenAI 兼容的 Chat Completions 接口。

设计要点
--------
* **零依赖**：仅使用标准库 ``urllib.request``，不需要 ``openai`` / ``requests``。
* **服务商无关**：任何实现了 OpenAI 兼容协议的服务都能直接使用
  （DeepSeek、OpenAI、通义千问、Kimi、智谱、本地 Ollama / vLLM ...）。
* **错误处理与重试**：区分「可重试错误」（网络抖动、429 限流、5xx）与
  「不可重试错误」（401 鉴权失败、400 参数错误），采用指数退避 + 随机抖动。
* **原生 Function Calling**：把工具定义成 JSON Schema，让模型自己决定调用哪个工具。
* **可测试**：``MockLLMClient`` 提供确定性的离线回复，让单测不依赖网络。
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .config import Config

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: Dict[str, Any]
    raw_arguments: str = ""

    @classmethod
    def from_api(cls, payload: Dict[str, Any]) -> "ToolCall":
        function = payload.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"_value": parsed}
        return cls(
            id=payload.get("id") or f"call_{abs(hash(raw)) % 10**8}",
            name=function.get("name") or "",
            arguments=parsed,
            raw_arguments=raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
        )


@dataclass
class LLMResponse:
    """一次 LLM 调用的结果。"""

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_message(self) -> Dict[str, Any]:
        """转回 OpenAI messages 格式，用于追加到对话历史。"""
        message: Dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class LLMError(RuntimeError):
    """LLM 调用失败。``retryable`` 标记是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


# --------------------------------------------------------------------------- #
# 真实客户端
# --------------------------------------------------------------------------- #


class LLMClient:
    """OpenAI 兼容协议的 Chat Completions 客户端。"""

    def __init__(self, config: Config, *, verbose: bool = False):
        self.config = config
        self.verbose = verbose
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def _build_request(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        temperature: Optional[float],
        stream: bool = False,
    ) -> urllib.request.Request:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "code-review-agent/1.0",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return urllib.request.Request(
            self.config.chat_endpoint, data=body, headers=headers, method="POST"
        )

    @staticmethod
    def _extract_http_error(exc: urllib.error.HTTPError) -> LLMError:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 读取失败不应掩盖原始错误
            detail = ""
        hint = ""
        if exc.code in (401, 403):
            hint = "（API Key 无效或没有权限，请检查 .env 中的 CODEAGENT_API_KEY）"
        elif exc.code == 404:
            hint = "（接口路径或模型名不存在，请检查 CODEAGENT_BASE_URL / CODEAGENT_MODEL）"
        elif exc.code == 429:
            hint = "（触发限流，稍后自动重试）"
        message = f"HTTP {exc.code} {exc.reason}{hint}: {detail[:500]}"
        retryable = exc.code == 429 or 500 <= exc.code < 600
        return LLMError(message, retryable=retryable, status=exc.code)

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """发起一次对话补全，内部自动重试。"""
        if not self.config.has_api_key:
            raise LLMError(
                "未配置 API Key：请在 .env 中设置 CODEAGENT_API_KEY，"
                "或设置环境变量 CODEAGENT_API_KEY；如需离线演示可运行 `--mock`。"
            )

        last_error: Optional[LLMError] = None
        attempts = self.config.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                request = self._build_request(messages, tools, temperature)
                self._log(
                    f"  → LLM 请求 #{attempt}/{attempts}  model={self.config.model}  "
                    f"messages={len(messages)}  tools={len(tools or [])}"
                )
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    data = json.loads(response.read().decode("utf-8", errors="replace"))
                result = self._parse_response(data)
                self.usage["prompt_tokens"] += result.prompt_tokens
                self.usage["completion_tokens"] += result.completion_tokens
                self.usage["calls"] += 1
                self._log(
                    f"  ← LLM 返回 finish={result.finish_reason}  "
                    f"tool_calls={len(result.tool_calls)}  tokens={result.total_tokens}"
                )
                return result

            except urllib.error.HTTPError as exc:
                error = self._extract_http_error(exc)
            except urllib.error.URLError as exc:
                error = LLMError(f"网络连接失败: {exc.reason}（检查网络或 BASE_URL）", retryable=True)
            except TimeoutError:
                error = LLMError("请求超时", retryable=True)
            except json.JSONDecodeError as exc:
                error = LLMError(f"响应不是合法 JSON: {exc}", retryable=True)
            except LLMError as exc:
                error = exc

            last_error = error
            if not error.retryable or attempt == attempts:
                break
            delay = min(2 ** (attempt - 1) + random.uniform(0, 0.5), 20.0)
            self._log(f"  ! 第 {attempt} 次失败：{error}，{delay:.1f}s 后重试")
            time.sleep(delay)

        raise last_error or LLMError("未知的 LLM 调用错误")

    @staticmethod
    def _parse_response(data: Dict[str, Any]) -> LLMResponse:
        if "error" in data and isinstance(data["error"], dict):
            err = data["error"]
            raise LLMError(f"服务端返回错误: {err.get('message') or err}", retryable=False)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应中没有 choices 字段: {str(data)[:300]}", retryable=True)

        choice = choices[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        tool_calls = [ToolCall.from_api(tc) for tc in (message.get("tool_calls") or [])]
        return LLMResponse(
            content=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "stop",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=data.get("model") or "",
            raw=data,
        )

    # ------------------------------------------------------------------ #
    def simple_chat(self, messages: List[Dict[str, Any]]) -> str:
        """不带工具的单轮调用，用于摘要等辅助场景。"""
        return self.chat(messages, tools=None).content


# --------------------------------------------------------------------------- #
# 离线 Mock 客户端（供单元测试 / 无网络演示使用）
# --------------------------------------------------------------------------- #


class MockLLMClient(LLMClient):
    """确定性的假 LLM：按脚本依次返回预设回复。

    这让 Agent 循环、工具调度、错误处理都能在没有网络和密钥的情况下被测试，
    也让老师/助教可以一条命令跑通全流程。
    """

    def __init__(self, config: Config, script: Optional[Iterable[LLMResponse]] = None, *, verbose: bool = False):
        super().__init__(config, verbose=verbose)
        self.script: List[LLMResponse] = list(script or [])
        self.cursor = 0
        self.recorded_messages: List[List[Dict[str, Any]]] = []

    def chat(self, messages, tools=None, *, temperature=None) -> LLMResponse:  # type: ignore[override]
        self.recorded_messages.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        self.usage["calls"] += 1
        if self.cursor < len(self.script):
            response = self.script[self.cursor]
            self.cursor += 1
            return response
        # 脚本用尽后收敛，避免死循环
        return LLMResponse(content="## 审查结论\n\n（Mock 模式：脚本已执行完毕）", finish_reason="stop")


def default_mock_script() -> List[LLMResponse]:
    """一段贴近真实流程的 Mock 脚本：先探索、再分析、最后出报告。"""
    return [
        LLMResponse(
            content="我先探索项目结构，再逐个文件审查。",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="list_files",
                    arguments={"path": ".", "pattern": "*.py"},
                    raw_arguments=json.dumps({"path": ".", "pattern": "*.py"}),
                )
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="读取目标文件内容，并做静态分析。",
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name="read_file",
                    arguments={"path": "demo/sample_buggy.py"},
                    raw_arguments=json.dumps({"path": "demo/sample_buggy.py"}),
                ),
                ToolCall(
                    id="call_3",
                    name="static_analyze",
                    arguments={"path": "demo/sample_buggy.py"},
                    raw_arguments=json.dumps({"path": "demo/sample_buggy.py"}),
                ),
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content=(
                "## 审查结论\n\n"
                "### 🔴 严重\n"
                "- **demo/sample_buggy.py:24** 除零风险：`average()` 未处理空列表，`len(nums)` 为 0 时抛 "
                "`ZeroDivisionError`。建议先判空并返回 `0.0` 或抛出带说明的异常。\n\n"
                "### 🟡 警告\n"
                "- **demo/sample_buggy.py:38** 使用可变默认参数 `items=[]`，会在多次调用间共享状态。"
                "建议改为 `items=None` 再在函数体内初始化。\n\n"
                "### 🟢 建议\n"
                "- 异常捕获过宽（`except:`），会吞掉 `KeyboardInterrupt`；请改为捕获具体异常类型。\n"
            ),
            finish_reason="stop",
        ),
    ]
