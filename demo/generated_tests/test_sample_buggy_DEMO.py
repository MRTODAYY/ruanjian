
"""针对 demo/sample_buggy.py 的单元测试（标准库 unittest，零依赖）。

设计原则：
  * 正常路径 -> 断言当前真实行为（回归保护）。
  * 已确认的缺陷 -> 写成「失败测试」，用 assertRaises 断言「期望的正确行为」，
    因此在修复前会 FAIL，修复后自动 PASS（即 XFAIL 风格的可执行缺陷清单）。
  * 每个失败测试的 docstring 都标注了缺陷位置与证据。
"""

import json
import os
import sys
import tempfile
import unittest

# 让测试可以从项目根目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.sample_buggy import (  # noqa: E402
    MAX_ITEMS,
    OrderRegistry,
    add_item,
    average_order_amount,
    format_report,
    load_config,
    parse_expression,
    process_order,
    read_lines,
    save_order,
    success_rate,
    validate_order,
)


class TestAverageOrderAmount(unittest.TestCase):
    """average_order_amount —— 第 51-59 行。"""

    def test_normal_path(self):
        """正常路径：多笔订单取平均。"""
        self.assertEqual(average_order_amount([{"amount": 10}, {"amount": 20}]), 15.0)

    def test_missing_amount_key_defaults_to_zero(self):
        """缺 amount 键时按 0 计（第 58 行 order.get("amount", 0)）。"""
        self.assertEqual(average_order_amount([{"x": 1}, {"amount": 10}]), 5.0)

    def test_single_order(self):
        self.assertEqual(average_order_amount([{"amount": 7}]), 7.0)

    def test_empty_list_should_not_crash(self):
        """🔴 缺陷：第 59 行 total / len(orders)，空列表触发 ZeroDivisionError。

        期望行为：空输入返回 0.0（或抛 ValueError），而不是 ZeroDivisionError。
        """
        with self.assertRaises(ValueError):
            average_order_amount([])

    def test_none_input_should_raise_type_error(self):
        """🟠 缺陷：None 输入报 TypeError: 'NoneType' object is not iterable。"""
        with self.assertRaises(TypeError):
            average_order_amount(None)

    def test_non_dict_element_should_raise_type_error(self):
        """🟠 缺陷：元素非 dict 时报 AttributeError，应显式校验并抛 TypeError。"""
        with self.assertRaises(TypeError):
            average_order_amount([1, 2])


class TestSuccessRate(unittest.TestCase):
    """success_rate —— 第 62-67 行。"""

    def test_normal_path(self):
        self.assertAlmostEqual(success_rate(1, 3), 33.33333333333333)

    def test_all_success(self):
        self.assertEqual(success_rate(5, 5), 100.0)

    def test_zero_total_should_not_crash(self):
        """🔴 缺陷：第 67 行 success / total，total=0 触发 ZeroDivisionError。"""
        with self.assertRaises(ValueError):
            success_rate(0, 0)

    def test_negative_total_should_be_rejected(self):
        """🟠 缺陷：total 为负时返回 -50.0，语义上不可能，应拒绝。"""
        with self.assertRaises(ValueError):
            success_rate(1, -2)

    def test_type_error_on_non_numeric(self):
        """🟠 缺陷：非数值入参报 TypeError，应显式校验。"""
        with self.assertRaises(TypeError):
            success_rate("abc", 10)


class TestAddItem(unittest.TestCase):
    """add_item —— 第 70-76 行。"""

    def test_explicit_list_is_returned(self):
        """正常路径：显式传入列表。"""
        target = []
        self.assertEqual(add_item("a", target), ["a"])
        self.assertIs(add_item("b", target), target)

    def test_default_arg_should_not_leak_between_calls(self):
        """🔴 缺陷：第 70 行 items=[] 是可变默认参数，跨调用共享同一列表。

        证据：add_item("a") -> ['a']；add_item("b") -> ['a', 'b']（幽灵状态）。
        期望行为：每次调用得到独立的新列表。
        """
        first = add_item("a")
        second = add_item("b")
        self.assertEqual(first, ["a"])
        self.assertEqual(second, ["b"], "默认参数在多次调用间被共享（幽灵状态）")

    def test_default_arg_object_identity(self):
        """🔴 缺陷：默认参数对象在函数定义期只创建一次。"""
        add_item("x")
        leaked = add_item.__defaults__[0]
        self.assertEqual(leaked, [], "默认列表被污染，说明默认值被跨调用复用")


class TestOrderRegistry(unittest.TestCase):
    """OrderRegistry —— 第 26-40 行。"""

    def test_add_returns_length(self):
        reg = OrderRegistry("r")
        self.assertEqual(reg.add({"id": 1}), 1)
        self.assertEqual(reg.add({"id": 2}), 2)

    def test_name_is_stored(self):
        self.assertEqual(OrderRegistry("shop").name, "shop")

    def test_cache_should_not_be_shared_between_instances(self):
        """🔴 缺陷：第 32 行 cache = [] 是可变类属性，所有实例共享。

        证据：r1.add(...) 后 r2.cache 也出现该订单（订单串号）。
        """
        r1 = OrderRegistry("r1")
        r2 = OrderRegistry("r2")
        r1.add({"id": 1})
        self.assertEqual(r2.cache, [], "cache 被跨实例共享，导致订单串号")

    def test_stats_should_not_be_shared_between_instances(self):
        """🟠 缺陷：第 33 行 stats = {} 同样是可变类属性。"""
        r1 = OrderRegistry("r1")
        r2 = OrderRegistry("r2")
        r1.stats["hits"] = 1
        self.assertEqual(r2.stats, {}, "stats 被跨实例共享")


class TestParseExpression(unittest.TestCase):
    """parse_expression —— 第 43-48 行。"""

    def test_normal_arithmetic(self):
        self.assertEqual(parse_expression("1+2*3"), 7)

    def test_eval_should_not_execute_arbitrary_code(self):
        """🔴 缺陷：第 48 行 eval(expr) 可执行任意代码（代码注入）。

        证据：parse_expression("__import__('os').getcwd()") 返回了真实工作目录。
        期望行为：只允许字面量，非法表达式抛 ValueError。
        """
        with self.assertRaises(ValueError):
            parse_expression("__import__('os').getcwd()")

    def test_eval_should_not_perform_side_effects(self):
        """🔴 缺陷：eval 可产生副作用（写文件）。"""
        marker = os.path.join(tempfile.gettempdir(), "eval_pwned.txt")
        if os.path.exists(marker):
            os.remove(marker)
        with self.assertRaises(ValueError):
            parse_expression("open(%r, 'w').close() or 42" % marker)
        self.assertFalse(os.path.exists(marker), "eval 执行了文件写入副作用")

    def test_empty_string_should_raise_value_error(self):
        """🟠 缺陷：空字符串抛 SyntaxError，应统一为 ValueError。"""
        with self.assertRaises(ValueError):
            parse_expression("")

    def test_non_string_should_raise_type_error(self):
        """🟠 缺陷：非字符串入参抛 TypeError（eval 内部报错，信息不友好）。"""
        with self.assertRaises(TypeError):
            parse_expression(None)


class TestLoadConfig(unittest.TestCase):
    """load_config —— 第 79-88 行。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_valid_json(self):
        path = self._write("ok.json", '{"a": 1}')
        self.assertEqual(load_config(path), {"a": 1})

    def test_missing_file_returns_empty_dict(self):
        """当前行为：文件不存在时静默返回 {}。"""
        self.assertEqual(load_config(os.path.join(self.tmpdir, "nope.json")), {})

    def test_invalid_json_should_not_be_silently_swallowed(self):
        """🟠 缺陷：第 87 行裸 except 把 JSON 解析错误也吞成 {}，调用方无法区分
        「文件不存在」与「文件损坏」。期望：非法 JSON 抛 ValueError。
        """
        path = self._write("bad.json", "{not json")
        with self.assertRaises(ValueError):
            load_config(path)

    def test_bare_except_should_not_swallow_keyboard_interrupt(self):
        """🟠 缺陷：第 87 行裸 except 会吞掉 KeyboardInterrupt，程序无法中断。"""
        with self.assertRaises(KeyboardInterrupt):
            load_config(_BoomPath())


class _BoomPath:
    """伪路径对象：被 open() 使用时抛出 KeyboardInterrupt。"""

    def __fspath__(self):
        raise KeyboardInterrupt("user pressed Ctrl-C")


class TestSaveOrder(unittest.TestCase):
    """save_order —— 第 91-101 行。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_normal_path_writes_json(self):
        path = os.path.join(self.tmpdir, "order.json")
        save_order({"id": 1, "name": "张三"}, path)
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"id": 1, "name": "张三"})

    def test_write_failure_should_not_be_silently_swallowed(self):
        """🔴 缺陷：第 100-101 行 except: pass 静默吞掉写失败。

        证据：save_order(order, "no_such_dir_xyz/f.json") 返回 None 且不抛异常，
        调用方以为保存成功，实际数据丢失。
        期望行为：向上抛出 OSError。
        """
        bad_path = os.path.join(self.tmpdir, "no_such_dir_xyz", "f.json")
        with self.assertRaises(OSError):
            save_order({"id": 1}, bad_path)

    def test_non_serializable_order_should_raise(self):
        """🟠 缺陷：不可序列化对象同样被静默吞掉。"""
        path = os.path.join(self.tmpdir, "x.json")
        with self.assertRaises(TypeError):
            save_order({"bad": object()}, path)


class TestReadLines(unittest.TestCase):
    """read_lines —— 第 104-110 行。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_normal_path(self):
        path = os.path.join(self.tmpdir, "a.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("a\nb\n")
        self.assertEqual(read_lines(path), ["a\n", "b\n"])

    def test_empty_file(self):
        path = os.path.join(self.tmpdir, "empty.txt")
        open(path, "w", encoding="utf-8").close()
        self.assertEqual(read_lines(path), [])

    def test_missing_file_raises_file_not_found(self):
        """当前行为：文件不存在抛 FileNotFoundError（但句柄未关闭）。"""
        with self.assertRaises(FileNotFoundError):
            read_lines(os.path.join(self.tmpdir, "nope.txt"))

    def test_handle_should_be_closed_after_read(self):
        """🟠 缺陷：第 109-110 行未使用 with，句柄在异常路径下泄漏。

        这里用「读取后文件可被删除/重命名」间接验证句柄已释放（Windows 下
        未关闭的句柄会阻止删除）。
        """
        path = os.path.join(self.tmpdir, "lock.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        read_lines(path)
        try:
            os.remove(path)
        except OSError as exc:  # pragma: no cover - 平台相关
            self.fail("文件句柄未关闭，无法删除文件: %s" % exc)


class TestProcessOrder(unittest.TestCase):
    """process_order —— 第 113-153 行。"""

    def setUp(self):
        self.registry = OrderRegistry("shop")

    def test_normal_path(self):
        order = {"status": "ok", "items": [{"price": 10, "qty": 2}]}
        result = process_order(order, self.registry)
        self.assertEqual(result["total"], 20.0)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["name"], "shop")

    def test_discount_tax_shipping(self):
        order = {"status": "ok", "items": [{"price": 100, "qty": 1}]}
        result = process_order(order, self.registry, discount=0.1, tax=0.05, shipping=5.0)
        self.assertAlmostEqual(result["total"], 100 * 0.9 * 1.05 + 5.0)

    def test_missing_items_returns_error(self):
        self.assertEqual(process_order({"status": "ok"}, self.registry), {"error": "no items"})

    def test_empty_items_returns_error(self):
        self.assertEqual(
            process_order({"status": "ok", "items": []}, self.registry), {"error": "empty items"}
        )

    def test_cancelled_returns_error(self):
        order = {"status": "cancelled", "items": [{"price": 1, "qty": 1}]}
        self.assertEqual(process_order(order, self.registry), {"error": "cancelled"})

    def test_none_order_should_return_none(self):
        """🔴 缺陷：第 118 行 tmp = order.get("debug") 在 None 检查（第 121 行）之前执行，
        导致 process_order(None, reg) 抛 AttributeError，而第 121-122 行的
        `if order is None: return None` 成了永远不可达的死代码。
        """
        self.assertIsNone(process_order(None, self.registry))

    def test_negative_amount_should_be_rejected(self):
        """🟠 缺陷：第 141 行未校验负数，负金额订单被正常受理。

        证据：process_order({"items":[{"price":-100,"qty":1}]}) -> total = -100.0
        """
        order = {"status": "ok", "items": [{"price": -100, "qty": 1}]}
        result = process_order(order, self.registry)
        self.assertNotIn("total", result, "负金额订单不应被受理")

    def test_coupon_should_not_produce_negative_total(self):
        """🟠 缺陷：第 145-146 行 coupon 固定减 10，小额订单总价变负。

        证据：1 元订单 + coupon -> total = -9.0
        """
        order = {"status": "ok", "items": [{"price": 1, "qty": 1}]}
        result = process_order(order, self.registry, coupon="SAVE10")
        self.assertGreaterEqual(result["total"], 0, "优惠后总价不应为负")

    def test_items_missing_price_are_silently_skipped(self):
        """🟠 缺陷：第 135-139 行缺 price/qty 的商品被 continue 静默丢弃，
        总价少算且调用方无任何提示。期望：返回错误或计入告警。
        """
        order = {"status": "ok", "items": [{"price": 10, "qty": 2}, {"price": 5}]}
        result = process_order(order, self.registry)
        self.assertIn("error", result, "存在字段缺失的商品时应报错，而不是静默少算")

    def test_successful_order_is_registered(self):
        order = {"status": "ok", "items": [{"price": 1, "qty": 1}]}
        process_order(order, self.registry)
        self.assertIn(order, self.registry.cache)

    def test_type_error_on_non_dict_order(self):
        """🟠 缺陷：非 dict 入参抛 AttributeError，应显式校验抛 TypeError。"""
        with self.assertRaises(TypeError):
            process_order("not-a-dict", self.registry)


class TestValidateOrder(unittest.TestCase):
    """validate_order —— 第 156-173 行。"""

    def test_valid_order(self):
        self.assertTrue(validate_order({"status": "ok", "items": [{"price": 1, "qty": 1}]}))

    def test_none_is_invalid(self):
        self.assertFalse(validate_order(None))

    def test_missing_items_is_invalid(self):
        self.assertFalse(validate_order({"status": "ok"}))

    def test_empty_items_is_invalid(self):
        self.assertFalse(validate_order({"status": "ok", "items": []}))

    def test_cancelled_is_invalid(self):
        self.assertFalse(validate_order({"status": "cancelled", "items": [{"price": 1, "qty": 1}]}))

    def test_non_dict_should_raise_type_error(self):
        """🟠 缺陷：第 164 行 "items" not in order 对 int/float 抛 TypeError，
        而 None 却返回 False —— 类型校验不一致。
        """
        with self.assertRaises(TypeError):
            validate_order(0)

    def test_consistency_with_process_order(self):
        """🟡 缺陷：第 156-173 行与 process_order 第 121-131 行校验逻辑重复，
        两处容易漂移。这里断言二者判定一致。
        """
        cases = [
            {"status": "ok", "items": [{"price": 1, "qty": 1}]},
            {"status": "ok"},
            {"status": "ok", "items": []},
            {"status": "cancelled", "items": [{"price": 1, "qty": 1}]},
        ]
        for order in cases:
            with self.subTest(order=order):
                accepted = "error" not in process_order(order, OrderRegistry("s"))
                self.assertEqual(validate_order(order), accepted)


class TestFormatReport(unittest.TestCase):
    """format_report —— 第 176-184 行。"""

    def test_normal_path(self):
        rows = [{"id": 1, "amount": 2.5}, {"id": 2, "amount": 3}]
        self.assertEqual(format_report(rows), "1,2.5\n2,3\n")

    def test_empty_rows(self):
        self.assertEqual(format_report([]), "")

    def test_missing_amount_raises_key_error(self):
        """🟠 缺陷：第 183 行 row["amount"] 直接下标访问，缺键抛 KeyError。"""
        with self.assertRaises(KeyError):
            format_report([{"id": 1}])

    def test_none_rows_should_raise_type_error(self):
        with self.assertRaises(TypeError):
            format_report(None)


class TestModuleConstants(unittest.TestCase):
    """模块级常量。"""

    def test_max_items_value(self):
        self.assertEqual(MAX_ITEMS, 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
