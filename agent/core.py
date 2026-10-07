"""Agent 主循环 —— 本项目的核心。

所谓 Agent，与「一次问答」的区别就在于这个循环：

    用户输入 → [ LLM 推理 → 决定调用工具 → 工具真实执行 → 结果回填 ] × N → 最终输出

关键设计
--------
* **Thought / Action / Observation**：每一步都记录推理文本、工具调用、工具结果，
  形成可审计的轨迹（transcript），出问题能定位是「模型想错了」还是「工具做错了」。
* **收敛保证**：``max_steps`` 上限 + 无工具调用即结束，避免无限循环烧钱。
* **容错**：工具异常、模型返回非法 JSON、接口报错都不会中断整个流程，
  而是把错误作为 Observation 回填给模型，让它自我纠正。
* **上下文记忆**：多轮对话历史 + 长期记忆（项目摘要），并在超长时自动压缩。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .llm import LLMClient, LLMError, LLMResponse, ToolCall
from .prompts import PromptLibrary
from .tools import ToolRegistry, ToolResult

# 上下文管理的保守阈值：超过则压缩最早的工具结果
MAX_HISTORY_CHARS = 60_000
KEEP_RECENT_MESSAGES = 12


@dataclass
class TraceEntry:
    """Agent 单步轨迹。"""

    step: int
    kind: str  # thought | tool_call | tool_result | final | error
    content: str = ""
    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)
    tool_ok: bool = True
    duration_ms: int = 0
    tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "content": self.content,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "tool_ok": self.tool_ok,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
        }


@dataclass
class AgentRun:
    """一次完整运行的产出。"""

    query: str
    answer: str
    steps: int
    trace: List[TraceEntry] = field(default_factory=list)
    tool_calls: int = 0
    tool_failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    stop_reason: str = "completed"
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": self.duration_ms,
            "stop_reason": self.stop_reason,
            "errors": self.errors,
            "trace": [entry.to_dict() for entry in self.trace],
        }


EventCallback = Callable[[str, Dict[str, Any]], None]


class CodeReviewAgent:
    """代码审查 Agent。"""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        prompts: Optional[PromptLibrary] = None,
        on_event: Optional[EventCallback] = None,
    ):
        self.config = config
        self.llm = llm
        self.registry = registry
        self.prompts = prompts or PromptLibrary()
        self.on_event = on_event
        self.history: List[Dict[str, Any]] = []  # 多轮对话记忆（含工具消息）
        self.memory: Dict[str, Any] = {}         # 长期记忆：项目摘要等
        self.trace: List[TraceEntry] = []

    # ------------------------------------------------------------------ #
    # 事件通知
    # ------------------------------------------------------------------ #
    def _emit(self, event: str, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event(event, payload)
            except Exception:  # noqa: BLE001 - 回调异常不能影响主流程
                pass

    # ------------------------------------------------------------------ #
    # 上下文管理
    # ------------------------------------------------------------------ #
    def _history_chars(self) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in self.history)

    @staticmethod
    def _sanitize_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """保证工具消息与 assistant.tool_calls 严格配对。

        这是 OpenAI 兼容接口的**硬性协议要求**：每条 ``role="tool"`` 消息必须是
        某条 ``assistant`` 消息中某个 ``tool_call_id`` 的响应，否则整个请求被拒绝：

            HTTP 400: Messages with role 'tool' must be a response to a
                      preceding message with 'tool_calls'

        上下文压缩、消息裁剪、异常中断都可能破坏这种配对，因此在每次请求前
        统一做一次「孤儿清理」，把找不到父调用的工具消息丢掉。
        丢掉一条消息不影响正确性（模型可以重新读取文件），但协议错误会让整个
        Agent 直接失败，代价高得多。
        """
        available: set = set()
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = call.get("id")
                    if call_id:
                        available.add(call_id)

        cleaned: List[Dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "tool":
                if message.get("tool_call_id") not in available:
                    continue  # 父调用已被裁掉，这条 Observation 成了孤儿
            cleaned.append(message)

        # 反向检查：assistant 声明了 tool_calls 但对应结果缺失时，
        # 部分服务端同样会报错，因此为缺失的调用补一条说明性结果。
        answered = {
            m.get("tool_call_id") for m in cleaned if m.get("role") == "tool"
        }
        repaired: List[Dict[str, Any]] = []
        for message in cleaned:
            repaired.append(message)
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = call.get("id")
                    if call_id and call_id not in answered:
                        repaired.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "name": (call.get("function") or {}).get("name", ""),
                                "content": json.dumps(
                                    {"ok": False, "error": "该工具调用结果已因上下文压缩被省略。"},
                                    ensure_ascii=False,
                                ),
                            }
                        )
        return repaired

    def _compact_history(self) -> None:
        """压缩历史：保留系统提示 + 首轮用户提问 + 最近的若干消息。

        被裁掉的历史摘要成一条 system 消息，既省 token 又不丢关键信息。
        压缩后必须重新校验工具消息配对，否则请求会被服务端拒绝。
        """
        if self._history_chars() <= MAX_HISTORY_CHARS:
            return
        head = self.history[:2]
        tail = self.history[-KEEP_RECENT_MESSAGES:]
        dropped = self.history[2 : len(self.history) - KEEP_RECENT_MESSAGES]
        if not dropped:
            return
        tool_names = sorted({m.get("name", "") for m in dropped if m.get("role") == "tool"})
        summary = (
            "[上下文压缩] 之前的对话已省略 "
            f"{len(dropped)} 条消息（历史长度 {self._history_chars()} 字符）。"
            + (f" 已调用过的工具：{', '.join(n for n in tool_names if n)}。" if tool_names else "")
            + " 如需重新查看某个文件，请再次调用 read_file。"
        )
        self.history = self._sanitize_history(
            head + [{"role": "system", "content": summary}] + tail
        )
        self._emit("compact", dropped=len(dropped))

    def reset(self) -> None:
        """清空对话记忆（保留长期记忆）。"""
        self.history = []
        self.trace = []

    def remember(self, key: str, value: Any) -> None:
        """写入长期记忆（跨轮次保留）。"""
        self.memory[key] = value

    # ------------------------------------------------------------------ #
    # 系统提示
    # ------------------------------------------------------------------ #
    def _system_message(self) -> Dict[str, str]:
        tools_desc = self.registry.catalog_text()
        memory_text = ""
        if self.memory:
            memory_text = "\n\n【已知上下文记忆】\n" + "\n".join(
                f"- {k}: {str(v)[:500]}" for k, v in self.memory.items()
            )
        return {
            "role": "system",
            "content": self.prompts.system_prompt(
                workspace=str(self.config.workspace),
                tools_catalog=tools_desc,
                extra=memory_text,
            ),
        }

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self, query: str, *, mode: str = "review", goals: str = "") -> AgentRun:
        """执行一次完整的 Agent 循环。

        :param query: 用户请求（例如「审查 demo/sample_buggy.py」）
        :param mode:  任务模式，决定使用哪个 Prompt 模板
        :param goals: 额外的审查重点（可选）
        """
        run = AgentRun(query=query, answer="", steps=0)
        started = time.perf_counter()

        # 每次运行都重置本轮的轨迹（保留对话历史，实现多轮记忆）
        self.trace = []
        run.trace = self.trace  # 共享同一列表，运行中实时可读

        # 1) 组装初始上下文
        if not self.history or self.history[0].get("role") != "system":
            self.history.insert(0, self._system_message())
        else:
            self.history[0] = self._system_message()

        user_content = self.prompts.user_task_prompt(query, mode=mode, goals=goals)
        self.history.append({"role": "user", "content": user_content})
        self._emit("user", text=user_content)

        schemas = self.registry.schemas()
        self._emit("start", model=self.config.model, tools=self.registry.names(),
                   max_steps=self.config.max_steps)

        # 2) Agent 循环
        for step in range(1, self.config.max_steps + 1):
            run.steps = step
            self._compact_history()
            self._emit("step", step=step, max_steps=self.config.max_steps)

            # --- 推理：让 LLM 决定下一步 ---
            try:
                response = self.llm.chat(self._sanitize_history(self.history), tools=schemas)
            except LLMError as exc:
                message = f"LLM 调用失败：{exc}"
                run.errors.append(message)
                run.stop_reason = "llm_error"
                self.trace.append(TraceEntry(step=step, kind="error", content=message))
                self._emit("error", message=message)
                break
            except Exception as exc:  # noqa: BLE001 - 兜底，保证 CLI 不崩
                message = f"未预期的错误：{type(exc).__name__}: {exc}"
                run.errors.append(message)
                run.stop_reason = "unexpected_error"
                self.trace.append(TraceEntry(step=step, kind="error", content=message))
                self._emit("error", message=message)
                break

            run.prompt_tokens += response.prompt_tokens
            run.completion_tokens += response.completion_tokens

            # 记录模型的思考文本
            if response.content:
                entry = TraceEntry(step=step, kind="thought", content=response.content,
                                   tokens=response.total_tokens)
                self.trace.append(entry)
                self._emit("thought", step=step, text=response.content)

            # --- 分支 A：模型认为可以收尾 ---
            if not response.wants_tools:
                run.answer = response.content.strip()
                run.stop_reason = "completed"
                self.trace.append(TraceEntry(step=step, kind="final", content=run.answer))
                self.history.append(response.to_message())
                self._emit("final", step=step, text=run.answer)
                break

            # --- 分支 B：执行工具调用 ---
            self.history.append(response.to_message())
            for call in response.tool_calls:
                result, duration = self._execute_tool(call)
                run.tool_calls += 1
                if not result.ok:
                    run.tool_failures += 1

                self.trace.append(
                    TraceEntry(
                        step=step, kind="tool_call", tool_name=call.name,
                        tool_args=call.arguments, content=result.content or result.error,
                        tool_ok=result.ok, duration_ms=duration,
                    )
                )
                self._emit(
                    "tool_call", step=step, name=call.name, args=call.arguments,
                    ok=result.ok, duration_ms=duration,
                    preview=(result.content or result.error)[:600],
                )

                # Observation 回填：无论成功失败都告诉模型真实结果
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result.to_payload(),
                    }
                )
        else:
            # for-else：循环跑满仍未收敛
            run.stop_reason = "max_steps"
            message = (
                f"已达到最大步数 {self.config.max_steps}，Agent 停止继续调用工具。"
                "请基于已有分析给出结论，或提高 CODEAGENT_MAX_STEPS。"
            )
            self._emit("error", message=message)
            run.errors.append(message)
            if not run.answer:
                run.answer = self._forced_summary(run)

        # 3) 收尾
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        if not run.answer:
            run.answer = self._forced_summary(run)
        self._emit("done", run=run.to_dict())
        return run

    # ------------------------------------------------------------------ #
    def _execute_tool(self, call: ToolCall) -> tuple[ToolResult, int]:
        """执行一次工具调用并计时；任何异常都收敛为失败结果。"""
        self._emit("tool_start", name=call.name, args=call.arguments)
        start = time.perf_counter()
        try:
            if not call.name:
                result = ToolResult.fail(
                    "模型返回的工具调用缺少函数名（可能被截断），请重新给出完整调用。"
                )
            else:
                result = self.registry.execute(call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 - 双重保险
            result = ToolResult.fail(f"工具调度异常：{type(exc).__name__}: {exc}")
        duration = int((time.perf_counter() - start) * 1000)
        result.duration_ms = duration
        return result, duration

    # ------------------------------------------------------------------ #
    def _forced_summary(self, run: AgentRun) -> str:
        """未获得最终答案时的兜底输出：把已有 Observation 整理成可读结果。"""
        parts = [f"（Agent 未在 {self.config.max_steps} 步内给出最终结论，以下为已收集到的分析素材）"]
        tool_outputs = [e for e in self.trace if e.kind == "tool_call" and e.tool_ok]
        if tool_outputs:
            for entry in tool_outputs[-3:]:
                parts.append(f"\n### 工具 `{entry.tool_name}` 的输出\n```\n{entry.content[:2000]}\n```")
        if run.errors:
            parts.append("\n### 过程中的错误\n" + "\n".join(f"- {e}" for e in run.errors))
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    def transcript_markdown(self) -> str:
        """把轨迹渲染为 Markdown，便于写进报告或做演示。"""
        lines = ["## Agent 执行轨迹", ""]
        for entry in self.trace:
            if entry.kind == "thought":
                lines.append(f"**Step {entry.step} · 推理**\n\n> {entry.content}\n")
            elif entry.kind == "tool_call":
                status = "✅" if entry.tool_ok else "❌"
                args = json.dumps(entry.tool_args, ensure_ascii=False)
                lines.append(
                    f"**Step {entry.step} · 工具调用** {status} `{entry.tool_name}({args})`"
                    f"（{entry.duration_ms} ms）\n"
                )
                preview = entry.content[:800].replace("\n", "\n> ")
                lines.append(f"> {preview}\n")
            elif entry.kind == "error":
                lines.append(f"**Step {entry.step} · 错误** ⚠️ {entry.content}\n")
        return "\n".join(lines)
