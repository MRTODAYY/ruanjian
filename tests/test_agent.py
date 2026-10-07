# -*- coding: utf-8 -*-
"""Agent 循环测试：推理→工具调用→结果回填 的完整闭环。

使用 MockLLMClient，因此**不消耗 API 额度**，可随时运行。
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent.config import Config
from agent.core import CodeReviewAgent
from agent.llm import LLMError, LLMResponse, MockLLMClient, ToolCall
from agent.prompts import PromptLibrary
from agent.tools import build_registry


def tool_response(name, arguments, call_id="call_1", content="我来调用工具。"):
    return LLMResponse(
        content=content,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments,
                             raw_arguments=json.dumps(arguments, ensure_ascii=False))],
        finish_reason="tool_calls",
    )


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        (self.workspace / "app.py").write_text(
            "def divide(a, b):\n    return a / b\n", encoding="utf-8"
        )
        self.config = Config(
            base_url="https://api.example.com",
            api_key="sk-test",
            model="test-model",
            max_steps=6,
            workspace=self.workspace,
        )
        self.registry = build_registry(self.workspace, enable_exec=True, exec_timeout=15)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, script, on_event=None):
        llm = MockLLMClient(self.config, script)
        return CodeReviewAgent(self.config, llm, self.registry, on_event=on_event), llm


class TestAgentLoop(AgentTestCase):
    def test_single_step_answer(self):
        agent, _ = self.build([LLMResponse(content="## 审查结论\n没问题。")])
        run = agent.run("app.py", mode="review")
        self.assertEqual(run.steps, 1)
        self.assertEqual(run.tool_calls, 0)
        self.assertIn("审查结论", run.answer)
        self.assertEqual(run.stop_reason, "completed")

    def test_full_loop_reason_act_observe(self):
        script = [
            tool_response("read_file", {"path": "app.py"}),
            tool_response("static_analyze", {"path": "app.py"}, call_id="call_2"),
            LLMResponse(content="## 审查结论\n发现除零风险。"),
        ]
        agent, llm = self.build(script)
        run = agent.run("app.py", mode="review")

        self.assertEqual(run.steps, 3)
        self.assertEqual(run.tool_calls, 2)
        self.assertEqual(run.tool_failures, 0)
        self.assertIn("除零", run.answer)

        # 关键：工具结果必须回填到对话历史，模型才可能自我纠正
        roles = [m["role"] for m in agent.history]
        self.assertIn("tool", roles)
        tool_messages = [m for m in agent.history if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertIn("app.py", tool_messages[0]["content"])
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")

    def test_multiple_tool_calls_in_one_step(self):
        script = [
            LLMResponse(
                content="并行调用。",
                tool_calls=[
                    ToolCall(id="a", name="read_file", arguments={"path": "app.py"},
                             raw_arguments='{"path": "app.py"}'),
                    ToolCall(id="b", name="list_files", arguments={"pattern": "*.py"},
                             raw_arguments='{"pattern": "*.py"}'),
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done"),
        ]
        agent, _ = self.build(script)
        run = agent.run("app.py")
        self.assertEqual(run.steps, 2)
        self.assertEqual(run.tool_calls, 2)

    def test_trace_records_every_phase(self):
        script = [
            tool_response("static_analyze", {"path": "app.py"}),
            LLMResponse(content="结论"),
        ]
        agent, _ = self.build(script)
        run = agent.run("app.py")
        kinds = [entry.kind for entry in run.trace]
        self.assertIn("thought", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("final", kinds)
        self.assertTrue(run.trace, "run.trace 必须被填充（CLI 报告依赖它）")

    def test_max_steps_stops_runaway_loop(self):
        """模型每步都要求调用工具时，必须在 max_steps 处收敛。"""
        config = replace(self.config, max_steps=3)
        script = [tool_response("list_files", {"pattern": "*"}) for _ in range(10)]
        agent = CodeReviewAgent(config, MockLLMClient(config, script), self.registry)
        run = agent.run("x")
        self.assertEqual(run.steps, 3)
        self.assertEqual(run.stop_reason, "max_steps")
        self.assertTrue(run.answer, "超步数时也必须给出兜底输出")
        self.assertTrue(any("最大步数" in e for e in run.errors))

    def test_tool_failure_is_fed_back_not_fatal(self):
        script = [
            tool_response("read_file", {"path": "missing.py"}),
            LLMResponse(content="文件不存在，我改用 list_files。"),
        ]
        agent, _ = self.build(script)
        run = agent.run("missing.py")
        self.assertEqual(run.tool_failures, 1)
        self.assertEqual(run.stop_reason, "completed")
        tool_message = [m for m in agent.history if m["role"] == "tool"][0]
        self.assertIn('"ok": false', tool_message["content"])

    def test_unknown_tool_is_handled_gracefully(self):
        script = [
            tool_response("nonexistent_tool", {"x": 1}),
            LLMResponse(content="我换个工具。"),
        ]
        agent, _ = self.build(script)
        run = agent.run("x")
        self.assertEqual(run.tool_failures, 1)
        self.assertEqual(run.stop_reason, "completed")

    def test_llm_error_stops_cleanly(self):
        class BrokenLLM(MockLLMClient):
            def chat(self, messages, tools=None, *, temperature=None):
                raise LLMError("模拟接口故障")

        agent = CodeReviewAgent(self.config, BrokenLLM(self.config, []), self.registry)
        run = agent.run("x")
        self.assertEqual(run.stop_reason, "llm_error")
        self.assertTrue(run.errors)
        self.assertTrue(run.answer, "出错时仍要有可读输出")

    def test_unexpected_exception_is_contained(self):
        class ExplodingLLM(MockLLMClient):
            def chat(self, messages, tools=None, *, temperature=None):
                raise RuntimeError("内存炸了")

        agent = CodeReviewAgent(self.config, ExplodingLLM(self.config, []), self.registry)
        run = agent.run("x")
        self.assertEqual(run.stop_reason, "unexpected_error")
        self.assertIn("内存炸了", " ".join(run.errors))

    def test_usage_is_aggregated(self):
        script = [
            LLMResponse(content="a", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id="1", name="list_files", arguments={},
                                             raw_arguments="{}")],
                        prompt_tokens=100, completion_tokens=20),
            LLMResponse(content="b", prompt_tokens=200, completion_tokens=40),
        ]
        agent, _ = self.build(script)
        run = agent.run("x")
        self.assertEqual(run.prompt_tokens, 300)
        self.assertEqual(run.completion_tokens, 60)


class TestContextMemory(AgentTestCase):
    def test_multi_turn_history_is_kept(self):
        script = [
            LLMResponse(content="第一次回答"),
            LLMResponse(content="第二次回答"),
        ]
        agent, llm = self.build(script)
        agent.run("第一个问题")
        agent.run("第二个问题")

        # 第二轮应能看到第一轮的对话
        second_call_messages = llm.recorded_messages[1]
        joined = json.dumps(second_call_messages, ensure_ascii=False)
        self.assertIn("第一个问题", joined)
        self.assertIn("第一次回答", joined)
        self.assertIn("第二个问题", joined)

    def test_system_prompt_is_refreshed_not_duplicated(self):
        agent, llm = self.build([LLMResponse(content="a"), LLMResponse(content="b")])
        agent.run("q1")
        agent.run("q2")
        systems = [m for m in agent.history if m["role"] == "system"]
        self.assertEqual(len(systems), 1, "系统提示只能有一条")

    def test_long_history_is_compacted(self):
        script = [
            tool_response("read_file", {"path": "app.py"}, call_id=f"c{i}")
            for i in range(40)
        ] + [LLMResponse(content="结束")]
        agent, _ = self.build(script)

        # 造一个超长工具输出，触发压缩
        big = self.workspace / "big.py"
        big.write_text("x = 1\n" * 4000, encoding="utf-8")
        script_agent, _ = self.build(
            [tool_response("read_file", {"path": "big.py"}, call_id=f"b{i}") for i in range(30)]
            + [LLMResponse(content="结束")]
        )
        run = script_agent.run("big.py")
        self.assertTrue(run.answer)
        self.assertLess(len(script_agent.history), 60,
                        "历史必须被压缩，否则会撑爆上下文窗口")

    def test_reset_clears_conversation(self):
        agent, _ = self.build([LLMResponse(content="a"), LLMResponse(content="b")])
        agent.run("q1")
        self.assertTrue(agent.history)
        agent.reset()
        self.assertEqual(agent.history, [])
        self.assertEqual(agent.trace, [])

    def test_long_term_memory_appears_in_system_prompt(self):
        agent, llm = self.build([LLMResponse(content="a"), LLMResponse(content="b")])
        agent.run("q1")
        agent.remember("项目类型", "Django 后端")
        agent.run("q2")
        joined = json.dumps(llm.recorded_messages[-1], ensure_ascii=False)
        self.assertIn("项目类型", joined)
        self.assertIn("Django", joined)


class TestHistoryProtocol(AgentTestCase):
    """OpenAI 兼容接口的协议约束。

    真实 API 曾拒绝过请求：
    ``Messages with role 'tool' must be a response to a preceding message with
    'tool_calls'`` —— 上下文压缩把 assistant 的 tool_calls 裁掉后，
    工具消息就成了「孤儿」。这类错误会让整个 Agent 直接失败，必须有测试兜住。
    """

    def test_orphan_tool_message_is_dropped(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "tool", "tool_call_id": "gone", "name": "read_file", "content": "{}"},
        ]
        cleaned = CodeReviewAgent._sanitize_history(messages)
        self.assertEqual([m["role"] for m in cleaned], ["system", "user"])

    def test_paired_tool_message_is_kept(self):
        messages = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "{}"},
        ]
        cleaned = CodeReviewAgent._sanitize_history(messages)
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[-1]["role"], "tool")

    def test_missing_tool_result_is_padded(self):
        """assistant 声明了调用但没有结果时，补一条说明性结果。"""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
        ]
        cleaned = CodeReviewAgent._sanitize_history(messages)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[1]["role"], "tool")
        self.assertEqual(cleaned[1]["tool_call_id"], "c1")
        self.assertIn("上下文压缩", cleaned[1]["content"])

    def test_compact_history_never_orphans_tools(self):
        """压缩 40 轮「assistant+tool」历史后，历史必须仍然合法。"""
        agent, _ = self.build([LLMResponse(content="done")])
        for index in range(40):
            call_id = f"c{index}"
            agent.history.append({
                "role": "assistant", "content": f"第{index}步",
                "tool_calls": [{"id": call_id, "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            })
            # 每条工具结果都塞大内容，确保触发压缩
            agent.history.append({
                "role": "tool", "tool_call_id": call_id, "name": "read_file",
                "content": "x" * 2000,
            })
        agent.history.insert(0, {"role": "system", "content": "系统提示"})

        agent._compact_history()
        agent.history = agent._sanitize_history(agent.history)

        declared = {
            call["id"]
            for message in agent.history if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
        }
        answered = {
            message["tool_call_id"]
            for message in agent.history if message.get("role") == "tool"
        }
        self.assertTrue(answered, "压缩后应仍保留部分工具结果")
        self.assertTrue(
            answered <= declared,
            f"存在孤儿工具消息：{sorted(answered - declared)}",
        )
        self.assertTrue(declared <= answered, "存在没有结果的工具调用")

    def test_sanitize_is_idempotent(self):
        messages = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "{}"},
        ]
        once = CodeReviewAgent._sanitize_history(messages)
        twice = CodeReviewAgent._sanitize_history(once)
        self.assertEqual(once, twice)

    def test_every_request_sent_is_protocol_valid(self):
        """端到端：每一轮真正发给 LLM 的消息都必须通过协议校验。"""
        script = [
            tool_response("read_file", {"path": "app.py"}, call_id=f"c{i}")
            for i in range(12)
        ] + [LLMResponse(content="done")]
        agent, llm = self.build(script)
        (self.workspace / "big.py").write_text("y = 1\n" * 3000, encoding="utf-8")
        agent.run("big.py")

        for index, messages in enumerate(llm.recorded_messages):
            declared = {
                call["id"] for m in messages if m.get("role") == "assistant"
                for call in m.get("tool_calls") or []
            }
            for m in messages:
                if m.get("role") == "tool":
                    self.assertIn(
                        m["tool_call_id"], declared,
                        f"第 {index} 次请求出现孤儿工具消息 {m['tool_call_id']}",
                    )


class TestEvents(AgentTestCase):
    def test_emits_expected_event_sequence(self):
        events = []
        script = [
            tool_response("static_analyze", {"path": "app.py"}),
            LLMResponse(content="结论"),
        ]
        agent, _ = self.build(script, on_event=lambda name, payload: events.append(name))
        agent.run("app.py")
        for expected in ["start", "step", "thought", "tool_start", "tool_call", "final", "done"]:
            self.assertIn(expected, events)

    def test_broken_callback_does_not_break_run(self):
        def bad_callback(event, payload):
            raise RuntimeError("回调炸了")

        agent, _ = self.build([LLMResponse(content="ok")], on_event=bad_callback)
        run = agent.run("x")
        self.assertEqual(run.stop_reason, "completed")
        self.assertEqual(run.answer, "ok")

    def test_tool_call_event_carries_diagnostics(self):
        captured = {}
        script = [
            tool_response("read_file", {"path": "app.py"}),
            LLMResponse(content="done"),
        ]
        agent, _ = self.build(
            script,
            on_event=lambda name, payload: captured.update({name: payload})
            if name == "tool_call" else None,
        )
        agent.run("app.py")
        self.assertTrue(captured["tool_call"]["ok"])
        self.assertIn("duration_ms", captured["tool_call"])
        self.assertIn("app.py", captured["tool_call"]["preview"])


class TestTranscript(AgentTestCase):
    def test_transcript_markdown_contains_steps(self):
        script = [
            tool_response("static_analyze", {"path": "app.py"}, content="先做静态分析。"),
            LLMResponse(content="结论如下。"),
        ]
        agent, _ = self.build(script)
        agent.run("app.py")
        transcript = agent.transcript_markdown()
        self.assertIn("Agent 执行轨迹", transcript)
        self.assertIn("static_analyze", transcript)
        self.assertIn("先做静态分析。", transcript)

    def test_run_to_dict_is_json_serialisable(self):
        script = [tool_response("list_files", {}), LLMResponse(content="done")]
        agent, _ = self.build(script)
        run = agent.run("x")
        payload = json.dumps(run.to_dict(), ensure_ascii=False)
        self.assertIn("tool_call", payload)


class TestPromptLibrary(unittest.TestCase):
    def test_all_modes_render(self):
        library = PromptLibrary()
        for mode in ["review", "explain", "test", "refactor", "ask"]:
            with self.subTest(mode=mode):
                text = library.user_task_prompt("demo/x.py", mode=mode, goals="关注并发")
                self.assertIn("demo/x.py", text)
                self.assertIn("关注并发", text)

    def test_unknown_mode_falls_back(self):
        text = PromptLibrary().user_task_prompt("q", mode="not_a_mode")
        self.assertIn("q", text)

    def test_system_prompt_contains_guardrails(self):
        prompt = PromptLibrary().system_prompt(
            workspace="/tmp", tools_catalog="- `read_file`：读文件"
        )
        self.assertIn("/tmp", prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("行号", prompt)          # 强制引用行号，抑制幻觉
        self.assertIn("严重", prompt)          # 严重级别口径
        self.assertIn("不要当作问题上报", prompt.replace("**", ""))  # 误报抑制

    def test_mode_labels_are_chinese(self):
        labels = PromptLibrary.modes()
        self.assertEqual(labels["review"], "代码审查")
        self.assertEqual(PromptLibrary.mode_label("review"), "代码审查")


class TestSafety(AgentTestCase):
    def test_exec_disabled_blocks_model_request(self):
        """模型请求执行代码但功能被关闭时，必须被明确拒绝且给出替代方案。"""
        config = replace(self.config, enable_exec=False)
        registry = build_registry(self.workspace, enable_exec=False)
        self.assertNotIn("execute_code", registry.names())
        script = [
            tool_response("execute_code", {"code": "print(1)"}),
            LLMResponse(content="好的，我改为静态分析。"),
        ]
        agent = CodeReviewAgent(config, MockLLMClient(config, script), registry)
        run = agent.run("x")
        self.assertEqual(run.tool_failures, 1)
        tool_message = [m for m in agent.history if m["role"] == "tool"][0]
        self.assertIn("未知工具", tool_message["content"])
        # 模型必须知道下一步能做什么，否则会陷入无效重试
        self.assertIn("static_analyze", tool_message["content"])

    def test_path_escape_attempt_is_blocked(self):
        script = [
            tool_response("read_file", {"path": "../../../../etc/passwd"}),
            LLMResponse(content="被拒绝了，我换个思路。"),
        ]
        agent, _ = self.build(script)
        run = agent.run("x")
        self.assertEqual(run.tool_failures, 1)
        tool_message = [m for m in agent.history if m["role"] == "tool"][0]
        self.assertIn("拒绝访问", tool_message["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
