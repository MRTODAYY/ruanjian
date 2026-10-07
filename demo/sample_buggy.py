"""示例：一个包含多处典型缺陷的订单处理模块。

这个文件是**故意写坏的**，用于演示 Code Review Agent 的检出能力。
请勿在真实项目中使用。

已知植入的缺陷（供对照验证检出力）：
1. 除零风险          —— average_order_amount / success_rate
2. 可变默认参数      —— add_item(items=[])
3. 裸 except         —— load_config
4. 静默吞异常        —— save_order 中的 except: pass
5. 可变类属性        —— OrderRegistry.cache
6. 未使用变量/导入   —— process_order 中的 tmp、顶部 import
7. 资源未释放        —— read_lines 未使用 with
8. 圈复杂度偏高      —— process_order
9. eval 高危调用     —— parse_expression
10. 深层嵌套 + 重复代码 —— validate_order / is_order_valid
"""

import json
import os
import sys  # FIXME: 未使用的导入

MAX_ITEMS = 50


class OrderRegistry:
    """订单注册表。

    缺陷：cache 是可变类属性，所有实例共享，会导致订单串号。
    """

    cache = []
    stats = {}

    def __init__(self, name):
        self.name = name

    def add(self, order):
        self.cache.append(order)
        return len(self.cache)


def parse_expression(expr):
    """把字符串表达式转成数值。

    缺陷：使用 eval 执行任意代码，存在注入风险。
    """
    return eval(expr)


def average_order_amount(orders):
    """计算平均订单金额。

    缺陷：orders 为空时 len(orders) 为 0，触发 ZeroDivisionError。
    """
    total = 0
    for order in orders:
        total += order.get("amount", 0)
    return total / len(orders)


def success_rate(success, total):
    """计算成功率。

    缺陷：total 为 0 时除零。
    """
    return success / total * 100


def add_item(item, items=[]):
    """往列表里追加商品。

    缺陷：可变默认参数，多次调用之间会共享同一个列表。
    """
    items.append(item)
    return items


def load_config(path):
    """读取配置文件。

    缺陷：裸 except 会吞掉 KeyboardInterrupt；且失败时静默返回空字典。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except:
        return {}


def save_order(order, path):
    """保存订单到文件。

    缺陷：静默吞掉异常，写失败时调用方完全不知道。
    """
    try:
        handle = open(path, "w", encoding="utf-8")
        handle.write(json.dumps(order, ensure_ascii=False))
        handle.close()
    except:
        pass


def read_lines(path):
    """读取文件所有行。

    缺陷：异常路径下文件句柄不会关闭，存在资源泄漏。
    """
    handle = open(path, "r", encoding="utf-8")
    return handle.readlines()


def process_order(order, registry, discount=0.0, tax=0.0, shipping=0.0, coupon=None):
    """处理订单：校验 → 计算价格 → 落库。

    缺陷：圈复杂度过高、深层嵌套、存在未使用变量、金额计算未处理负数。
    """
    tmp = order.get("debug")
    result = {}

    if order is None:
        return None
    else:
        if "items" not in order:
            result["error"] = "no items"
        else:
            if len(order["items"]) == 0:
                result["error"] = "empty items"
            else:
                if order.get("status") == "cancelled":
                    result["error"] = "cancelled"
                else:
                    total = 0
                    for item in order["items"]:
                        if item.get("price") is None:
                            continue
                        else:
                            if item.get("qty") is None:
                                continue
                            else:
                                total = total + item["price"] * item["qty"]
                    total = total - total * discount
                    total = total + total * tax
                    total = total + shipping
                    if coupon:
                        total = total - 10
                    result["total"] = total
                    result["currency"] = "CNY"
                    result["name"] = registry.name
                    registry.add(order)
                    return result

    return result


def validate_order(order):
    """校验订单字段。

    缺陷：与 process_order 中的校验逻辑重复（复制粘贴）。
    """
    if order is None:
        return False
    else:
        if "items" not in order:
            return False
        else:
            if len(order["items"]) == 0:
                return False
            else:
                if order.get("status") == "cancelled":
                    return False
                else:
                    return True


def is_order_valid(order):
    """另一种「订单是否有效」的判断。

    缺陷：与 validate_order **逐行完全相同**，是教科书式的复制粘贴。
    两处规则一旦不同步就会产生行为漂移，属于典型的 DRY 违背。
    """
    if order is None:
        return False
    else:
        if "items" not in order:
            return False
        else:
            if len(order["items"]) == 0:
                return False
            else:
                if order.get("status") == "cancelled":
                    return False
                else:
                    return True


def format_report(rows):
    """生成文本报表。

    缺陷：字符串在循环中反复拼接，数据量大时性能会退化。
    """
    text = ""
    for row in rows:
        text = text + str(row["id"]) + "," + str(row["amount"]) + "\n"
    return text


def main():
    registry = OrderRegistry("default")
    orders = [
        {"status": "ok", "items": [{"price": 10, "qty": 2}]},
        {"status": "ok", "items": []},
    ]
    for order in orders:
        print(process_order(order, registry))
    print("平均金额:", average_order_amount([]))   # 这里会崩溃


if __name__ == "__main__":
    main()
