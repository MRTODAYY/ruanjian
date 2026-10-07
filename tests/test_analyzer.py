# -*- coding: utf-8 -*-
"""静态分析引擎测试。

重点验证「能不能稳定抓到真实缺陷」，因为这是 Agent 结论可信度的地基。
"""

import unittest
from pathlib import Path

from agent.analyzer import (
    PythonAnalyzer,
    analyze_file,
    analyze_source,
    cyclomatic_complexity,
    summarize_findings,
)

import ast

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"
BUGGY = DEMO_DIR / "sample_buggy.py"
CLEAN = DEMO_DIR / "sample_clean.py"


def rules_of(source: str) -> set:
    _, findings = analyze_source(source)
    return {f.rule for f in findings}


class TestCyclomaticComplexity(unittest.TestCase):
    def test_simple_function_is_one(self):
        tree = ast.parse("def f():\n    return 1\n")
        func = tree.body[0]
        self.assertEqual(cyclomatic_complexity(func), 1)

    def test_if_adds_one(self):
        tree = ast.parse("def f(x):\n    if x:\n        return 1\n    return 2\n")
        self.assertEqual(cyclomatic_complexity(tree.body[0]), 2)

    def test_bool_operator_counts_operands(self):
        tree = ast.parse("def f(a, b, c):\n    if a and b and c:\n        return 1\n")
        # 1 (基础) + 1 (if) + 2 (and 的两个额外操作数)
        self.assertEqual(cyclomatic_complexity(tree.body[0]), 4)

    def test_loop_and_except(self):
        tree = ast.parse(
            "def f(xs):\n"
            "    for x in xs:\n"
            "        try:\n"
            "            pass\n"
            "        except ValueError:\n"
            "            pass\n"
        )
        self.assertEqual(cyclomatic_complexity(tree.body[0]), 3)


class TestDetection(unittest.TestCase):
    """逐条验证缺陷检出能力（每条对应一种真实世界的 Bug 模式）。"""

    def test_mutable_default_argument(self):
        self.assertIn("MUTABLE_DEFAULT_ARG", rules_of("def f(items=[]):\n    return items\n"))

    def test_mutable_default_via_constructor_call(self):
        self.assertIn("MUTABLE_DEFAULT_ARG", rules_of("def f(items=list()):\n    return items\n"))

    def test_bare_except(self):
        self.assertIn("BARE_EXCEPT", rules_of("def f():\n    try:\n        pass\n    except:\n        pass\n"))

    def test_empty_except_body(self):
        source = "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n"
        rules = rules_of(source)
        self.assertIn("EMPTY_EXCEPT_BODY", rules)

    def test_swallowed_exception(self):
        self.assertIn("SWALLOWED_EXCEPTION", rules_of(
            "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
        ))

    def test_eval_is_dangerous(self):
        self.assertIn("DANGEROUS_CALL", rules_of("def f(x):\n    return eval(x)\n"))

    def test_pickle_loads_is_dangerous(self):
        source = "import pickle\ndef f(b):\n    return pickle.loads(b)\n"
        self.assertIn("DANGEROUS_CALL", rules_of(source))

    def test_shell_true_is_dangerous(self):
        source = "import subprocess\ndef f(c):\n    return subprocess.run(c, shell=True)\n"
        self.assertIn("SHELL_INJECTION", rules_of(source))

    def test_division_by_zero_risk(self):
        self.assertIn("DIVISION_BY_ZERO_RISK", rules_of(
            "def f(items):\n    return sum(items) / len(items)\n"
        ))

    def test_division_guarded_is_not_reported(self):
        """有判空守卫时**不应**报警——这验证了误报抑制能力。"""
        source = (
            "def f(items):\n"
            "    if not items:\n"
            "        return 0.0\n"
            "    return sum(items) / len(items)\n"
        )
        self.assertNotIn("DIVISION_BY_ZERO_RISK", rules_of(source))

    def test_mutable_class_attribute(self):
        source = "class A:\n    cache = []\n    def __init__(self):\n        pass\n"
        self.assertIn("MUTABLE_CLASS_ATTRIBUTE", rules_of(source))

    def test_unreachable_code(self):
        source = "def f():\n    return 1\n    print('dead')\n"
        self.assertIn("UNREACHABLE_CODE", rules_of(source))

    def test_unused_import(self):
        self.assertIn("UNUSED_IMPORT", rules_of("import os\nimport json\nprint(json.dumps({}))\n"))

    def test_used_import_not_reported(self):
        self.assertNotIn("UNUSED_IMPORT", rules_of("import os\nprint(os.getcwd())\n"))

    def test_unused_variable(self):
        source = "def f():\n    unused = 1\n    return 2\n"
        self.assertIn("UNUSED_VARIABLE", rules_of(source))

    def test_is_comparison_with_none(self):
        source = "def f(x):\n    if x == None:\n        return 1\n    return 2\n"
        self.assertIn("IS_COMPARISON", rules_of(source))

    def test_deep_nesting(self):
        source = (
            "def f(a, b, c, d, e, g):\n"
            "    if a:\n"
            "        if b:\n"
            "            if c:\n"
            "                if d:\n"
            "                    if e:\n"
            "                        if g:\n"
            "                            return 1\n"
        )
        self.assertIn("DEEP_NESTING", rules_of(source))

    def test_too_many_params(self):
        source = "def f(a, b, c, d, e, g):\n    return a\n"
        self.assertIn("TOO_MANY_PARAMS", rules_of(source))

    def test_placeholder_implementation(self):
        self.assertIn("PLACEHOLDER_IMPLEMENTATION", rules_of("def f():\n    pass\n"))

    def test_missing_self_in_method(self):
        source = "class A:\n    def run():\n        return 1\n"
        self.assertIn("MISSING_SELF", rules_of(source))

    def test_wildcard_import(self):
        self.assertIn("WILDCARD_IMPORT", rules_of("from os import *\n"))

    def test_syntax_error_is_reported_not_crashed(self):
        metrics, findings = analyze_source("def f(:\n    pass\n")
        self.assertFalse(metrics.parse_ok)
        self.assertIn("SYNTAX_ERROR", {f.rule for f in findings})
        self.assertTrue(metrics.parse_error)

    def test_duplicate_code_detection(self):
        block = "    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    f = 6\n"
        source = "def f():\n" + block + "    return a\n\n\ndef g():\n" + block + "    return a\n"
        self.assertIn("DUPLICATE_CODE", rules_of(source))


class TestMetrics(unittest.TestCase):
    def test_counts_are_consistent(self):
        source = "import os\n\n# 注释\n\ndef f():\n    return 1\n"
        metrics, _ = analyze_source(source)
        self.assertEqual(metrics.total_lines, 6)
        self.assertEqual(metrics.comment_lines, 1)
        self.assertEqual(metrics.blank_lines, 2)
        self.assertEqual(metrics.functions, 1)
        self.assertEqual(metrics.imports, 1)

    def test_type_hint_coverage(self):
        hinted = "def f(a: int) -> int:\n    return a\n"
        not_hinted = "def f(a):\n    return a\n"
        self.assertEqual(analyze_source(hinted)[0].type_hinted_functions, 1)
        self.assertEqual(analyze_source(not_hinted)[0].type_hinted_functions, 0)

    def test_hotspots_sorted_by_complexity(self):
        source = (
            "def simple():\n    return 1\n\n"
            "def complex_(a, b, c):\n"
            "    if a:\n        return 1\n"
            "    if b:\n        return 2\n"
            "    if c:\n        return 3\n"
            "    return 4\n"
        )
        metrics, _ = analyze_source(source)
        self.assertEqual(metrics.complexity_hotspots[0]["name"], "complex_")
        self.assertEqual(metrics.max_complexity, 4)


class TestRealFiles(unittest.TestCase):
    """对示例文件做端到端断言：这是「工具真的有用」的证明。"""

    def test_buggy_sample_detects_all_planted_defects(self):
        self.assertTrue(BUGGY.is_file(), f"缺少示例文件 {BUGGY}")
        metrics, findings = analyze_file(BUGGY)
        self.assertTrue(metrics.parse_ok)
        rules = {f.rule for f in findings}

        expected = {
            "DANGEROUS_CALL",             # eval
            "DIVISION_BY_ZERO_RISK",      # average_order_amount / success_rate
            "MUTABLE_DEFAULT_ARG",        # add_item(items=[])
            "BARE_EXCEPT",                # load_config
            "EMPTY_EXCEPT_BODY",          # save_order
            "MUTABLE_CLASS_ATTRIBUTE",    # OrderRegistry.cache
            "UNUSED_IMPORT",              # import os / sys
            "UNUSED_VARIABLE",            # tmp
            "DEEP_NESTING",               # process_order
            "TOO_MANY_PARAMS",            # process_order
            "DUPLICATE_CODE",             # validate_order / is_order_valid 逐行相同
        }
        missing = expected - rules
        self.assertFalse(missing, f"漏检规则：{sorted(missing)}")

    def test_findings_have_actionable_fields(self):
        _, findings = analyze_file(BUGGY)
        for finding in findings:
            self.assertGreaterEqual(finding.line, 1)
            self.assertTrue(finding.message)
            self.assertIn(finding.severity, {"critical", "major", "minor", "info"})

    def test_critical_finds_eval_first(self):
        _, findings = analyze_file(BUGGY)
        self.assertEqual(findings[0].severity, "critical")
        self.assertIn("eval", findings[0].message)

    def test_clean_sample_produces_low_noise(self):
        """优质代码不应产生严重/重要级别的问题（误报抑制）。"""
        _, findings = analyze_file(CLEAN)
        serious = [f for f in findings if f.severity in {"critical", "major"}]
        self.assertEqual(
            serious, [],
            f"优质代码被误报：{[(f.rule, f.line) for f in serious]}",
        )

    def test_summary_counts_match(self):
        _, findings = analyze_file(BUGGY)
        summary = summarize_findings(findings)
        self.assertEqual(sum(summary.values()), len(findings))

    def test_analyzer_never_raises_on_weird_inputs(self):
        for source in ["", "\n\n\n", "class A:\n    pass\n", "x = 1", "\ufeffx = 1\n"]:
            with self.subTest(source=source[:20]):
                metrics, findings = analyze_source(source)
                self.assertIsInstance(metrics.total_lines, int)
                self.assertIsInstance(findings, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
