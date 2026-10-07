# -*- coding: utf-8 -*-
"""LLM 客户端测试：响应解析、重试策略、错误分类、Mock 行为。

全部通过替换 ``urllib.request.urlopen`` 实现，**不产生真实网络请求**。
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

from agent.config import Config
from agent.llm import (
    LLMClient,
    LLMError,
    LLMResponse,
    MockLLMClient,
    ToolCall,
    default_mock_script,
)


def make_config(**overrides) -> Config:
    base = Config(
        base_url="https://api.example.com",
        api_key="sk-test-key-1234567890",
        model="test-model",
        max_retries=2,
        timeout=5,
        temperature=0.0,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class FakeHTTPResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def completion(content="ok", tool_calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
        "model": "test-model",
    }


class TestConfig(unittest.TestCase):
    def test_endpoint_normalisation(self):
        self.assertEqual(make_config(base_url="https://api.deepseek.com").chat_endpoint,
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(make_config(base_url="https://api.openai.com/v1").chat_endpoint,
                         "https://api.openai.com/v1/chat/completions")
        # 已经是完整端点时不重复拼接
        self.assertEqual(
            make_config(base_url="https://x.com/v1/chat/completions").chat_endpoint,
            "https://x.com/v1/chat/completions",
        )

    def test_has_api_key_detects_placeholder(self):
        self.assertFalse(make_config(api_key="").has_api_key)
        self.assertFalse(make_config(api_key="sk-your-key-here").has_api_key)
        self.assertTrue(make_config(api_key="sk-real-key").has_api_key)

    def test_mask_key_hides_secret(self):
        masked = make_config(api_key="sk-abcdefghijklmnop").mask_key()
        self.assertNotIn("efghijkl", masked)
        self.assertIn("*", masked)

    def test_describe_does_not_leak_key(self):
        describe = make_config(api_key="sk-supersecretvalue123").describe()
        self.assertNotIn("supersecretvalue", describe)


class TestResponseParsing(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient(make_config())

    def test_plain_content(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(completion("hello"))):
            response = self.client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "hello")
        self.assertFalse(response.wants_tools)
        self.assertEqual(response.prompt_tokens, 10)
        self.assertEqual(response.total_tokens, 15)

    def test_tool_call_parsing(self):
        payload = completion(tool_calls=[{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        }])
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = self.client.chat([{"role": "user", "content": "go"}])
        self.assertTrue(response.wants_tools)
        call = response.tool_calls[0]
        self.assertEqual(call.id, "call_abc")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "a.py"})

    def test_invalid_json_arguments_do_not_crash(self):
        payload = completion(tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "read_file", "arguments": "{not json"},
        }])
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = self.client.chat([{"role": "user", "content": "go"}])
        self.assertEqual(response.tool_calls[0].arguments, {})

    def test_to_message_round_trip(self):
        response = LLMResponse(
            content="thinking",
            tool_calls=[ToolCall(id="c1", name="read_file",
                                 arguments={"path": "a.py"}, raw_arguments='{"path": "a.py"}')],
        )
        message = response.to_message()
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "read_file")
        # 必须能被 JSON 序列化（回填历史时要用）
        json.dumps(message)

    def test_usage_accumulates(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(completion())):
            self.client.chat([{"role": "user", "content": "1"}])
            self.client.chat([{"role": "user", "content": "2"}])
        self.assertEqual(self.client.usage["calls"], 2)
        self.assertEqual(self.client.usage["prompt_tokens"], 20)

    def test_server_side_error_field(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeHTTPResponse({"error": {"message": "bad model"}})):
            with self.assertRaises(LLMError) as ctx:
                self.client.chat([{"role": "user", "content": "x"}])
        self.assertIn("bad model", str(ctx.exception))

    def test_empty_choices_raises(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse({"choices": []})):
            with self.assertRaises(LLMError):
                self.client.chat([{"role": "user", "content": "x"}])


class TestRetryPolicy(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError(
            url="https://api.example.com", code=code, msg="err",
            hdrs=None, fp=io.BytesIO(b'{"error": "boom"}'),
        )

    def test_retries_on_429_then_succeeds(self):
        client = LLMClient(make_config(max_retries=3))
        side_effects = [self._http_error(429), FakeHTTPResponse(completion("recovered"))]
        with mock.patch("urllib.request.urlopen", side_effect=side_effects) as patched:
            with mock.patch("time.sleep"):  # 不真的等待
                response = client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(response.content, "recovered")
        self.assertEqual(patched.call_count, 2)

    def test_retries_on_500(self):
        client = LLMClient(make_config(max_retries=2))
        side_effects = [self._http_error(500), FakeHTTPResponse(completion("ok"))]
        with mock.patch("urllib.request.urlopen", side_effect=side_effects) as patched:
            with mock.patch("time.sleep"):
                client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(patched.call_count, 2)

    def test_401_is_not_retried(self):
        client = LLMClient(make_config(max_retries=3))
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(401)) as patched:
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(patched.call_count, 1, "鉴权失败不应浪费重试次数")
        self.assertIn("API Key", str(ctx.exception))

    def test_404_hints_at_wrong_model(self):
        client = LLMClient(make_config(max_retries=0))
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(404)):
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
        self.assertIn("CODEAGENT_MODEL", str(ctx.exception))

    def test_retries_exhausted_raises(self):
        client = LLMClient(make_config(max_retries=2))
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(503)) as patched:
            with mock.patch("time.sleep"):
                with self.assertRaises(LLMError):
                    client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(patched.call_count, 3)  # 1 次 + 2 次重试

    def test_network_error_is_retried(self):
        client = LLMClient(make_config(max_retries=1))
        side_effects = [
            urllib.error.URLError("connection refused"),
            FakeHTTPResponse(completion("back online")),
        ]
        with mock.patch("urllib.request.urlopen", side_effect=side_effects):
            with mock.patch("time.sleep"):
                response = client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(response.content, "back online")

    def test_timeout_is_retried(self):
        client = LLMClient(make_config(max_retries=1))
        side_effects = [TimeoutError("slow"), FakeHTTPResponse(completion("ok"))]
        with mock.patch("urllib.request.urlopen", side_effect=side_effects):
            with mock.patch("time.sleep"):
                self.assertEqual(client.chat([{"role": "user", "content": "x"}]).content, "ok")

    def test_missing_key_fails_fast_without_request(self):
        client = LLMClient(make_config(api_key=""))
        with mock.patch("urllib.request.urlopen") as patched:
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "x"}])
        patched.assert_not_called()
        self.assertIn("API Key", str(ctx.exception))


class TestRequestBuilding(unittest.TestCase):
    def test_tools_are_attached_to_payload(self):
        client = LLMClient(make_config())
        captured = {}

        def capture(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.headers)
            return FakeHTTPResponse(completion("ok"))

        tools = [{"type": "function", "function": {"name": "t", "description": "d",
                                                   "parameters": {"type": "object"}}}]
        with mock.patch("urllib.request.urlopen", side_effect=capture):
            client.chat([{"role": "user", "content": "x"}], tools=tools)

        self.assertEqual(captured["body"]["model"], "test-model")
        self.assertEqual(len(captured["body"]["tools"]), 1)
        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertIn("Authorization", captured["headers"])
        self.assertTrue(captured["headers"]["Authorization"].startswith("Bearer "))

    def test_no_tools_key_when_none_passed(self):
        client = LLMClient(make_config())
        captured = {}

        def capture(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(completion("ok"))

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            client.chat([{"role": "user", "content": "x"}])
        self.assertNotIn("tools", captured["body"])

    def test_chinese_content_survives_round_trip(self):
        client = LLMClient(make_config())
        captured = {}

        def capture(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(completion("中文回复"))

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            response = client.chat([{"role": "user", "content": "审查 demo/中文.py"}])
        self.assertIn("中文", captured["body"]["messages"][0]["content"])
        self.assertEqual(response.content, "中文回复")


class TestMockClient(unittest.TestCase):
    def test_script_is_consumed_then_converges(self):
        client = MockLLMClient(make_config(), default_mock_script())
        first = client.chat([{"role": "user", "content": "x"}])
        self.assertTrue(first.wants_tools)
        for _ in range(len(default_mock_script()) + 3):
            last = client.chat([{"role": "user", "content": "x"}])
        self.assertFalse(last.wants_tools, "脚本耗尽后必须收敛，避免死循环")

    def test_records_prompts_for_assertions(self):
        client = MockLLMClient(make_config(), [])
        client.chat([{"role": "user", "content": "审查 A"}])
        self.assertEqual(client.recorded_messages[0][0]["content"], "审查 A")

    def test_default_script_has_review_structure(self):
        script = default_mock_script()
        self.assertTrue(any(r.wants_tools for r in script))
        self.assertTrue(any("审查结论" in r.content for r in script))


if __name__ == "__main__":
    unittest.main(verbosity=2)
