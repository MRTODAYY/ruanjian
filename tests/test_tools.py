# -*- coding: utf-8 -*-
"""工具层测试：注册、校验、沙箱、执行、报告落盘。"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.tools import build_registry
from agent.tools.common import resolve_path, PathSecurityError


class ToolTestCase(unittest.TestCase):
    """公共夹具：在临时目录里建一个小项目。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        (self.workspace / "README.md").write_text("# 项目\n", encoding="utf-8")
        self.registry = build_registry(self.workspace, enable_exec=True, exec_timeout=15)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_expected_tools_registered(self):
        expected = {
            "list_files", "read_file", "static_analyze", "detect_code_smells",
            "write_report", "execute_code", "run_tests", "analyze_with_runtime",
        }
        self.assertTrue(expected.issubset(set(self.registry.names())))

    def test_schemas_are_valid_openai_tools(self):
        schemas = self.registry.schemas()
        self.assertTrue(schemas)
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            self.assertIn("name", schema["function"])
            self.assertIn("description", schema["function"])
            self.assertEqual(schema["function"]["parameters"]["type"], "object")
            self.assertTrue(schema["function"]["parameters"].get("properties") is not None)

    def test_exec_tools_hidden_when_disabled(self):
        safe = build_registry(self.workspace, enable_exec=False)
        self.assertNotIn("execute_code", safe.names())
        self.assertNotIn("run_tests", safe.names())
        self.assertIn("read_file", safe.names())

    def test_disabled_tool_reports_clear_error(self):
        """禁用执行时，执行类工具**根本不注册**。

        这比「注册后再拒绝」更彻底：模型看不到工具定义，也就无从发起调用，
        连一次被利用的机会都没有。此时若模型仍硬要调用，必须收到明确错误。
        """
        safe = build_registry(self.workspace, enable_exec=False)
        self.assertNotIn("execute_code", safe.names())
        result = safe.execute("execute_code", {"code": "print(1)"})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error)
        # 错误信息必须告诉模型「现在能用什么」，否则它会反复重试死路
        self.assertIn("static_analyze", result.error)


class TestListFiles(ToolTestCase):
    def test_lists_matching_files(self):
        result = self.registry.execute("list_files", {"pattern": "*.py"})
        self.assertTrue(result.ok)
        self.assertIn("src/app.py", result.content)
        self.assertNotIn("README.md", result.content)

    def test_no_match_is_not_an_error(self):
        result = self.registry.execute("list_files", {"pattern": "*.zzz"})
        self.assertTrue(result.ok)
        self.assertIn("没有匹配", result.content)

    def test_directory_traversal_blocked(self):
        result = self.registry.execute("list_files", {"path": "../.."})
        self.assertFalse(result.ok)
        self.assertIn("拒绝访问", result.error)


class TestReadFile(ToolTestCase):
    def test_returns_numbered_content(self):
        result = self.registry.execute("read_file", {"path": "src/app.py"})
        self.assertTrue(result.ok)
        self.assertIn("def add(a, b):", result.content)
        self.assertIn("1 |", result.content)  # 带行号
        self.assertEqual(result.data["total_lines"], 2)

    def test_line_range(self):
        (self.workspace / "src" / "long.py").write_text(
            "\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8"
        )
        result = self.registry.execute(
            "read_file", {"path": "src/long.py", "start_line": 5, "end_line": 7}
        )
        self.assertTrue(result.ok)
        self.assertIn("line5", result.content)
        self.assertIn("line7", result.content)
        self.assertNotIn("line8", result.content)

    def test_missing_file(self):
        result = self.registry.execute("read_file", {"path": "nope.py"})
        self.assertFalse(result.ok)

    def test_directory_rejected(self):
        result = self.registry.execute("read_file", {"path": "src"})
        self.assertFalse(result.ok)
        self.assertIn("目录", result.error)

    def test_binary_file_rejected(self):
        (self.workspace / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        result = self.registry.execute("read_file", {"path": "blob.bin"})
        self.assertFalse(result.ok)

    def test_missing_required_argument(self):
        result = self.registry.execute("read_file", {})
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.error)

    def test_unknown_tool(self):
        result = self.registry.execute("does_not_exist", {})
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error)


class TestStaticAnalyze(ToolTestCase):
    def test_reports_metrics_and_findings(self):
        result = self.registry.execute("static_analyze", {"path": "src/app.py"})
        self.assertTrue(result.ok)
        self.assertIn("度量", result.content)
        self.assertEqual(result.data["metrics"]["functions"], 1)

    def test_finds_planted_bug(self):
        (self.workspace / "src" / "bad.py").write_text(
            "def f(items=[]):\n    return items\n", encoding="utf-8"
        )
        result = self.registry.execute("static_analyze", {"path": "src/bad.py"})
        self.assertTrue(result.ok)
        rules = {f["rule"] for f in result.data["findings"]}
        self.assertIn("MUTABLE_DEFAULT_ARG", rules)

    def test_directory_rejected(self):
        result = self.registry.execute("static_analyze", {"path": "src"})
        self.assertFalse(result.ok)


class TestExecuteCode(ToolTestCase):
    def test_runs_code_and_captures_stdout(self):
        result = self.registry.execute("execute_code", {"code": "print(6*7)"})
        self.assertTrue(result.ok)
        self.assertIn("42", result.content)
        self.assertIn("成功", result.content)

    def test_captures_exception_traceback(self):
        result = self.registry.execute("execute_code", {"code": "1/0"})
        self.assertTrue(result.ok)  # 工具本身成功，代码失败
        self.assertIn("ZeroDivisionError", result.content)
        self.assertIn("失败", result.content)

    def test_timeout_is_enforced(self):
        result = self.registry.execute(
            "execute_code", {"code": "import time\ntime.sleep(30)", "timeout": 2}
        )
        self.assertTrue(result.ok)
        self.assertIn("超时", result.content)
        self.assertTrue(result.data.get("timed_out"))

    def test_empty_code_rejected(self):
        result = self.registry.execute("execute_code", {"code": "   "})
        self.assertFalse(result.ok)


class TestRunTests(ToolTestCase):
    def test_passing_tests(self):
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_ok.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertEqual(1, 1)\n",
            encoding="utf-8",
        )
        result = self.registry.execute("run_tests", {"path": "tests"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["returncode"], 0)

    def test_failing_tests_are_reported(self):
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_bad.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_fail(self):\n        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        result = self.registry.execute("run_tests", {"path": "tests"})
        self.assertTrue(result.ok)
        self.assertNotEqual(result.data["returncode"], 0)
        self.assertIn("失败", result.content)


class TestRuntimeProbe(ToolTestCase):
    def test_planted_crash_is_detected(self):
        (self.workspace / "src" / "crashy.py").write_text(
            "def average(nums):\n    return sum(nums) / len(nums)\n", encoding="utf-8"
        )
        result = self.registry.execute("analyze_with_runtime", {"path": "src/crashy.py"})
        self.assertTrue(result.ok)
        self.assertIn("ZeroDivisionError", result.content)
        self.assertIn("运行期体检", result.content)

    def test_safe_function_reports_no_issue(self):
        result = self.registry.execute("analyze_with_runtime", {"path": "src/app.py"})
        self.assertTrue(result.ok)
        self.assertIn("运行期体检", result.content)


class TestWriteReport(ToolTestCase):
    def test_writes_markdown_into_reports_dir(self):
        result = self.registry.execute(
            "write_report", {"content": "## 结论\n\n很好\n", "filename": "r.md"}
        )
        self.assertTrue(result.ok)
        path = self.workspace / "reports" / "r.md"
        self.assertTrue(path.is_file())
        self.assertIn("## 结论", path.read_text(encoding="utf-8"))

    def test_auto_filename_when_missing(self):
        result = self.registry.execute("write_report", {"content": "x"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["path"].split("/")[0], "reports")

    def test_directory_escape_blocked(self):
        """文件名里的目录成分必须被剥离，不能写到 reports/ 之外。"""
        self.registry.execute("write_report", {"content": "x", "filename": "../../evil.md"})
        self.assertFalse((self.workspace.parent / "evil.md").exists())
        self.assertFalse((self.workspace / "evil.md").exists())

    def test_empty_content_rejected(self):
        self.assertFalse(self.registry.execute("write_report", {"content": "  "}).ok)


class TestPathSandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_relative_path(self):
        (self.workspace / "a.txt").write_text("x", encoding="utf-8")
        resolved = resolve_path("a.txt", self.workspace)
        self.assertEqual(resolved, (self.workspace / "a.txt").resolve())

    def test_absolute_path_inside_workspace_allowed(self):
        (self.workspace / "a.txt").write_text("x", encoding="utf-8")
        resolved = resolve_path(str(self.workspace / "a.txt"), self.workspace)
        self.assertTrue(str(resolved).startswith(str(self.workspace.resolve())))

    def test_parent_traversal_rejected(self):
        with self.assertRaises(PathSecurityError):
            resolve_path("../outside.txt", self.workspace)

    def test_sibling_prefix_attack_rejected(self):
        """`/tmp/ws-evil` 不能因为前缀相同而被当成 `/tmp/ws` 的子路径。"""
        sibling = self.workspace.parent / (self.workspace.name + "-evil")
        sibling.mkdir(exist_ok=True)
        try:
            with self.assertRaises(PathSecurityError):
                resolve_path(str(sibling / "f.txt"), self.workspace)
        finally:
            sibling.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
