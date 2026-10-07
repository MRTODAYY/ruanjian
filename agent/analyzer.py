"""AST 静态分析引擎（Python）。

这是 Agent 的「确定性能力」：LLM 负责判断，但**度量与事实**必须由程序算出来，
否则模型很容易编造行号、漏掉问题。本模块不依赖任何第三方库，只用标准库 ``ast``。

输出分两部分：

* ``metrics``  —— 客观度量（行数、函数数、圈复杂度、嵌套深度、文档覆盖率……）
* ``findings`` —— 按严重级别分类的问题清单，每条都带精确行号与修改建议
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}
SEVERITY_LABEL = {"critical": "严重", "major": "重要", "minor": "次要", "info": "提示"}
SEVERITY_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵"}

BUILTIN_NAMES = set(dir(builtins))

# 危险函数：调用它们属于安全风险
DANGEROUS_CALLS = {
    "eval": "执行任意字符串代码，存在代码注入风险",
    "exec": "执行任意字符串代码，存在代码注入风险",
    "os.system": "直接调用系统 shell，存在命令注入风险",
    "os.popen": "直接调用系统 shell，存在命令注入风险",
    "pickle.loads": "反序列化不可信数据可导致任意代码执行",
    "marshal.loads": "反序列化不可信数据可导致任意代码执行",
    "yaml.load": "默认 Loader 可构造任意对象，请使用 safe_load",
    "input": "Python 2 语义下会执行输入内容（若为兼容代码需注意）",
}

FIXTURE_MARKERS = ("self", "cls")

# 结构归一化时保留的关键字（其余标识符一律折叠为 V）
KEYWORDS = {
    "if", "elif", "else", "for", "while", "return", "def", "class", "try",
    "except", "finally", "with", "as", "import", "from", "raise", "pass",
    "break", "continue", "and", "or", "not", "in", "is", "lambda", "yield",
    "global", "nonlocal", "assert", "del", "async", "await", "None", "True",
    "False",
}


@dataclass
class Finding:
    """一条静态检查发现。"""

    rule: str
    severity: str
    line: int
    message: str
    suggestion: str = ""
    symbol: str = ""
    evidence: str = ""
    source: str = "static"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FileMetrics:
    """单文件客观度量。"""

    path: str = ""
    language: str = "python"
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    docstring_lines: int = 0
    functions: int = 0
    classes: int = 0
    imports: int = 0
    max_complexity: int = 0
    avg_complexity: float = 0.0
    max_nesting: int = 0
    documented_functions: int = 0
    type_hinted_functions: int = 0
    parse_ok: bool = True
    parse_error: str = ""
    complexity_hotspots: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 度量工具
# --------------------------------------------------------------------------- #


def count_physical_lines(text: str) -> Dict[str, int]:
    lines = text.splitlines()
    blank = sum(1 for line in lines if not line.strip())
    comment = sum(1 for line in lines if line.strip().startswith("#"))
    return {
        "total": len(lines),
        "blank": blank,
        "comment": comment,
        "code": len(lines) - blank - comment,
    }


def cyclomatic_complexity(node: ast.AST) -> int:
    """圈复杂度：判定点数量 + 1（McCabe 简化版）。"""
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            complexity += 1
        elif isinstance(child, ast.ExceptHandler):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
        elif isinstance(child, ast.IfExp):
            complexity += 1
        elif isinstance(child, ast.comprehension):
            complexity += 1 + len(child.ifs)
        elif isinstance(child, ast.Assert):
            complexity += 1
        elif hasattr(ast, "Match") and isinstance(child, getattr(ast, "Match")):
            complexity += len(getattr(child, "cases", []))
        elif isinstance(child, ast.With) and getattr(child, "items", None):
            complexity += 0
    return complexity


def max_nesting(node: ast.AST, depth: int = 0) -> int:
    """最大嵌套深度（if/for/while/with/try 记一层）。"""
    nesting_types = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
    best = depth
    for child in ast.iter_child_nodes(node):
        # 函数定义重新开始计数
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            best = max(best, max_nesting(child, 0))
            continue
        if isinstance(child, nesting_types):
            best = max(best, max_nesting(child, depth + 1))
        else:
            best = max(best, max_nesting(child, depth))
    return best


def function_length(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)
    start = getattr(node, "lineno", 0)
    return max(0, end - start + 1)


def has_return_annotation(node) -> bool:
    return getattr(node, "returns", None) is not None


def arg_is_annotated(arg: ast.arg) -> bool:
    return arg.annotation is not None


# --------------------------------------------------------------------------- #
# 主分析器
# --------------------------------------------------------------------------- #


class PythonAnalyzer:
    """对单份 Python 源码做静态分析。"""

    def __init__(self, source: str, path: str = "<string>"):
        self.source = source
        self.path = path
        self.lines = source.splitlines()
        self.findings: List[Finding] = []
        self.metrics = FileMetrics(path=path)
        self.tree: Optional[ast.AST] = None
        self._used_names: Set[str] = set()
        self._assigned_names: Set[str] = set()

    # ------------------------------------------------------------------ #
    def _add(self, rule: str, severity: str, line: int, message: str,
             suggestion: str = "", symbol: str = "", evidence: str = "") -> None:
        self.findings.append(
            Finding(
                rule=rule,
                severity=severity,
                line=max(1, int(line or 1)),
                message=message,
                suggestion=suggestion,
                symbol=symbol,
                evidence=evidence.strip()[:200],
            )
        )

    def _evidence(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()
        return ""

    # ------------------------------------------------------------------ #
    def analyze(self) -> Tuple[FileMetrics, List[Finding]]:
        counts = count_physical_lines(self.source)
        self.metrics.total_lines = counts["total"]
        self.metrics.code_lines = counts["code"]
        self.metrics.comment_lines = counts["comment"]
        self.metrics.blank_lines = counts["blank"]

        try:
            self.tree = ast.parse(self.source, filename=self.path)
        except SyntaxError as exc:
            self.metrics.parse_ok = False
            self.metrics.parse_error = f"第 {exc.lineno} 行: {exc.msg}"
            self._add(
                "SYNTAX_ERROR", "critical", exc.lineno or 1,
                f"语法错误：{exc.msg}",
                "修复语法错误后重新分析；语法错误的代码无法运行。",
                evidence=(exc.text or "").strip(),
            )
            return self.metrics, self.findings

        assert self.tree is not None
        self._collect_names()
        self._analyze_module_level()
        self._analyze_definitions()
        self._analyze_imports()
        self._analyze_calls()
        self._analyze_code_smells()
        self._finalize_metrics()

        self.findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.line))
        return self.metrics, self.findings

    # ------------------------------------------------------------------ #
    def _collect_names(self) -> None:
        """收集被引用的名字与被赋值的名字，用于未使用导入判断。"""
        assert self.tree is not None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    self._used_names.add(node.id)
                else:
                    self._assigned_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                # 记录根名字：a.b.c -> a
                root = node
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    self._used_names.add(root.id)
            elif isinstance(node, ast.arg):
                self._used_names.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self._used_names.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._used_names.add(node.name)

    # ------------------------------------------------------------------ #
    def _analyze_module_level(self) -> None:
        assert self.tree is not None
        body = getattr(self.tree, "body", [])
        docstring = ast.get_docstring(self.tree)  # type: ignore[arg-type]
        if docstring:
            self.metrics.docstring_lines += len(docstring.splitlines())
        elif len(body) > 20:
            self._add(
                "MISSING_MODULE_DOCSTRING", "info", 1,
                "模块缺少 docstring，阅读者无法快速了解文件职责。",
                "在文件顶部添加一行模块说明。",
            )

        return

    # ------------------------------------------------------------------ #
    def _analyze_definitions(self) -> None:
        assert self.tree is not None
        complexities: List[int] = []
        function_nodes: List[ast.AST] = []

        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                self.metrics.classes += 1
                if ast.get_docstring(node):
                    self.metrics.docstring_lines += len(ast.get_docstring(node).splitlines())
                self._check_class(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_nodes.append(node)

        for node in function_nodes:
            self.metrics.functions += 1
            complex_ = cyclomatic_complexity(node)
            complexities.append(complex_)
            name = node.name
            length = function_length(node)
            nesting = max_nesting(node)
            args = list(node.args.args) + list(node.args.kwonlyargs)
            positional = list(node.args.args)
            if node.args.vararg:
                args.append(node.args.vararg)
            if node.args.kwarg:
                args.append(node.args.kwarg)

            self.metrics.complexity_hotspots.append(
                {"name": name, "line": node.lineno, "complexity": complex_,
                 "length": length, "nesting": nesting}
            )

            if ast.get_docstring(node):
                self.metrics.docstring_lines += len(ast.get_docstring(node).splitlines())
                self.metrics.documented_functions += 1
            else:
                # 只对“复杂到需要解释”的函数提示缺失 docstring，避免噪声淹没真问题
                if not name.startswith("_") and (complex_ >= 5 or length >= 25):
                    self._add(
                        "MISSING_DOCSTRING", "info", node.lineno,
                        f"函数 `{name}` 缺少 docstring（{length} 行 / 复杂度 {complex_}）。",
                        "补充 docstring，说明参数、返回值与可能的异常。",
                        symbol=name, evidence=self._evidence(node.lineno),
                    )

            if all(arg_is_annotated(a) for a in args) and has_return_annotation(node):
                self.metrics.type_hinted_functions += 1
            elif name.startswith("__") and name.endswith("__"):
                pass  # 魔术方法不强制
            elif not name.startswith("_") and args:
                self._add(
                    "MISSING_TYPE_HINT", "info", node.lineno,
                    f"函数 `{name}` 缺少完整的类型注解。",
                    "为参数与返回值添加类型注解，便于静态检查与 IDE 提示。",
                    symbol=name, evidence=self._evidence(node.lineno),
                )

            # 圈复杂度过高
            if complex_ > 10:
                self._add(
                    "HIGH_COMPLEXITY", "major" if complex_ > 15 else "minor", node.lineno,
                    f"函数 `{name}` 圈复杂度为 {complex_}（建议 ≤ 10），分支过多难以测试与维护。",
                    "用「卫语句提前返回」「提取子函数」「用策略表替代长 if-elif 链」来拆分。",
                    symbol=name, evidence=self._evidence(node.lineno),
                )

            # 函数过长
            if length > 60:
                self._add(
                    "LONG_FUNCTION", "major" if length > 100 else "minor", node.lineno,
                    f"函数 `{name}` 共 {length} 行（建议 ≤ 50），职责可能不单一。",
                    "按「单一职责」拆分为若干小函数，每个函数只做一件事。",
                    symbol=name, evidence=self._evidence(node.lineno),
                )

            # 嵌套过深
            if nesting > 4:
                self._add(
                    "DEEP_NESTING", "major" if nesting > 5 else "minor", node.lineno,
                    f"函数 `{name}` 最大嵌套深度为 {nesting} 层（建议 ≤ 3），可读性下降。",
                    "使用提前返回（early return）、continue 或提取子函数来降低嵌套。",
                    symbol=name, evidence=self._evidence(node.lineno),
                )

            # 参数过多
            if len(positional) > 5:
                self._add(
                    "TOO_MANY_PARAMS", "minor", node.lineno,
                    f"函数 `{name}` 有 {len(positional)} 个位置参数（建议 ≤ 5）。",
                    "把相关参数聚合为 dataclass / 配置对象，或使用关键字参数。",
                    symbol=name, evidence=self._evidence(node.lineno),
                )

            # 可变默认参数
            for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    self._add(
                        "MUTABLE_DEFAULT_ARG", "major", getattr(default, "lineno", node.lineno),
                        f"函数 `{name}` 使用可变对象作为默认参数，默认值在多次调用间共享，"
                        "会导致“幽灵状态”Bug。",
                        "改为 `None` 作为默认值，在函数体内 `if arg is None: arg = []`。",
                        symbol=name, evidence=self._evidence(getattr(default, "lineno", node.lineno)),
                    )
                elif isinstance(default, ast.Call) and isinstance(default.func, ast.Name):
                    if default.func.id in {"list", "dict", "set", "bytearray"}:
                        self._add(
                            "MUTABLE_DEFAULT_ARG", "minor", getattr(default, "lineno", node.lineno),
                            f"函数 `{name}` 的默认参数 `{default.func.id}()` 等价于可变默认值。",
                            "改为 `None` 作为默认值并在函数体内初始化。",
                            symbol=name,
                        )

            self._check_function_body(node)

        self.metrics.max_complexity = max(complexities) if complexities else 0
        self.metrics.avg_complexity = round(sum(complexities) / len(complexities), 2) if complexities else 0.0
        self.metrics.max_nesting = max_nesting(self.tree) if self.tree else 0
        self.metrics.complexity_hotspots = sorted(
            self.metrics.complexity_hotspots, key=lambda item: -item["complexity"]
        )[:5]

    # ------------------------------------------------------------------ #
    def _check_class(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            # 可变类属性（所有实例共享）
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = stmt.value
                if isinstance(value, (ast.List, ast.Dict, ast.Set)):
                    self._add(
                        "MUTABLE_CLASS_ATTRIBUTE", "major", getattr(stmt, "lineno", node.lineno),
                        f"类 `{node.name}` 的可变类属性会被所有实例共享，容易造成跨实例污染。",
                        "改为在 `__init__` 中赋值，或使用 `dataclasses.field(default_factory=...)`。",
                        symbol=node.name, evidence=self._evidence(getattr(stmt, "lineno", node.lineno)),
                    )
            # __init__ 缺少 self
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = stmt.args.args
                if not args:
                    self._add(
                        "MISSING_SELF", "critical", stmt.lineno,
                        f"方法 `{node.name}.{stmt.name}` 没有接收 `self` 参数，实例化调用会报 TypeError。",
                        "在方法定义中加入 `self` 作为第一个参数，或加上 `@staticmethod` 装饰器。",
                        symbol=f"{node.name}.{stmt.name}", evidence=self._evidence(stmt.lineno),
                    )
                elif args[0].arg not in FIXTURE_MARKERS and not self._is_staticmethod(stmt):
                    self._add(
                        "SUSPICIOUS_FIRST_ARG", "minor", stmt.lineno,
                        f"方法 `{node.name}.{stmt.name}` 的第一个参数名为 `{args[0].arg}` 而非 `self`，"
                        "容易让调用者困惑。",
                        "按 PEP 8 约定使用 `self`（类方法用 `cls`）。",
                        symbol=f"{node.name}.{stmt.name}",
                    )

    @staticmethod
    def _is_staticmethod(node: ast.AST) -> bool:
        for decorator in getattr(node, "decorator_list", []):
            name = decorator.id if isinstance(decorator, ast.Name) else (
                decorator.attr if isinstance(decorator, ast.Attribute) else ""
            )
            if name == "staticmethod":
                return True
        return False

    # ------------------------------------------------------------------ #
    def _check_function_body(self, node) -> None:
        """函数体级检查：裸 except、静默失败、危险调用、死代码。"""
        for child in ast.walk(node):
            # 裸 except / except Exception 过宽
            if isinstance(child, ast.ExceptHandler):
                if child.type is None:
                    self._add(
                        "BARE_EXCEPT", "major", child.lineno,
                        "使用裸 `except:` 会吞掉 KeyboardInterrupt / SystemExit 等所有异常，"
                        "导致程序无法中断且问题被隐藏。",
                        "捕获具体异常类型（如 `except ValueError:`），或至少使用 `except Exception:`。",
                        evidence=self._evidence(child.lineno),
                    )
                elif isinstance(child.type, ast.Name) and child.type.id == "Exception":
                    if not child.body or self._is_pass_only(child.body):
                        self._add(
                            "SWALLOWED_EXCEPTION", "major", child.lineno,
                            "`except Exception: pass` 静默吞掉异常，出错时无法定位问题。",
                            "至少记录日志 `logger.exception(...)`，或重新抛出更明确的异常。",
                            evidence=self._evidence(child.lineno),
                        )
                # 空 except 体（只有 pass）
                if self._is_pass_only(child.body):
                    self._add(
                        "EMPTY_EXCEPT_BODY", "major", child.lineno,
                        "异常处理体为空（只有 `pass`），错误被忽略。",
                        "补充日志、兜底逻辑或重新抛出异常。",
                        evidence=self._evidence(child.lineno),
                    )

            if isinstance(child, ast.Try):
                for final in child.finalbody:
                    if isinstance(final, (ast.Return, ast.Break, ast.Continue)):
                        self._add(
                            "RETURN_IN_FINALLY", "major", getattr(final, "lineno", child.lineno),
                            "在 `finally` 中 return/break/continue 会吞掉正在传播的异常。",
                            "把清理逻辑与返回值分离，避免在 finally 中改变控制流。",
                            evidence=self._evidence(getattr(final, "lineno", child.lineno)),
                        )

        self._check_dead_code(node)

    @staticmethod
    def _is_pass_only(body: List[ast.stmt]) -> bool:
        meaningful = [stmt for stmt in body if not isinstance(stmt, ast.Pass)]
        if not meaningful:
            return True
        return all(
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
            for stmt in meaningful
        )

    def _check_dead_code(self, node) -> None:
        """检测 return/raise/break/continue 之后不可达的语句。"""
        for block in self._iter_blocks(node):
            terminated = False
            for stmt in block:
                if terminated:
                    self._add(
                        "UNREACHABLE_CODE", "major", getattr(stmt, "lineno", 0),
                        "该语句位于 return/raise/break/continue 之后，永远不会被执行。",
                        "删除不可达代码，或检查控制流是否写错（例如漏了分支）。",
                        evidence=self._evidence(getattr(stmt, "lineno", 0)),
                    )
                    break
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                    terminated = True

    @staticmethod
    def _iter_blocks(node: ast.AST) -> Iterable[List[ast.stmt]]:
        for child in ast.walk(node):
            for attr in ("body", "orelse", "finalbody"):
                block = getattr(child, attr, None)
                if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                    yield block

    # ------------------------------------------------------------------ #
    def _analyze_imports(self) -> None:
        assert self.tree is not None
        imported: List[Tuple[str, str, int]] = []  # (绑定名, 描述, 行号)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    imported.append((bound, alias.name, node.lineno))
                    if alias.name == "*":
                        self._add(
                            "WILDCARD_IMPORT", "minor", node.lineno,
                            "`import *` 会污染命名空间并覆盖同名变量，难以追踪名称来源。",
                            "改为显式导入所需名称。",
                            evidence=self._evidence(node.lineno),
                        )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        self._add(
                            "WILDCARD_IMPORT", "minor", node.lineno,
                            "`from ... import *` 会污染命名空间，难以追踪名称来源。",
                            "改为显式导入所需名称。",
                            evidence=self._evidence(node.lineno),
                        )
                        continue
                    bound = alias.asname or alias.name
                    imported.append((bound, f"{node.module}.{alias.name}", node.lineno))

        self.metrics.imports = len(imported)
        has_all = any(
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "__all__" for t in node.targets)
            for node in getattr(self.tree, "body", [])
        )
        for bound, full, line in imported:
            used = bound in self._used_names or bound in self._assigned_names
            if not used:
                text = self._evidence(line)
                if "# noqa" in text or "noqa" in text:
                    continue
                self._add(
                    "UNUSED_IMPORT", "minor", line,
                    f"导入的 `{bound}`（来自 `{full}`）在文件中未被使用。",
                    "删除未使用的导入，保持依赖清单清晰。" + ("（若为 re-export，请定义 __all__）" if has_all else ""),
                    symbol=bound, evidence=text,
                )

    # ------------------------------------------------------------------ #
    def _analyze_calls(self) -> None:
        assert self.tree is not None
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = self._dotted_name(node.func)
            if dotted in DANGEROUS_CALLS:
                self._add(
                    "DANGEROUS_CALL", "critical", node.lineno,
                    f"调用了高危函数 `{dotted}`：{DANGEROUS_CALLS[dotted]}。",
                    "改用安全替代方案（如 `ast.literal_eval`、`yaml.safe_load`、`subprocess` 参数列表形式）。",
                    symbol=dotted, evidence=self._evidence(node.lineno),
                )
            # subprocess(..., shell=True)
            if dotted in {"subprocess.run", "subprocess.call", "subprocess.Popen",
                          "subprocess.check_output", "subprocess.check_call"}:
                for keyword in node.keywords:
                    if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                        self._add(
                            "SHELL_INJECTION", "critical", node.lineno,
                            f"`{dotted}(..., shell=True)` 在拼接外部输入时存在命令注入风险。",
                            "传入参数列表并保持 `shell=False`；必要时用 `shlex.quote` 转义。",
                            evidence=self._evidence(node.lineno),
                        )

            # == None / == True / == False 的比较在下方统一处理

        for node in ast.walk(self.tree):
            if isinstance(node, ast.Compare):
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq)):
                        if isinstance(comparator, ast.Constant) and comparator.value is None:
                            self._add(
                                "IS_COMPARISON", "minor", node.lineno,
                                "使用 `== None` 比较单例，应使用 `is None`（PEP 8）。",
                                "改为 `is None` / `is not None`。",
                                evidence=self._evidence(node.lineno),
                            )
                        elif isinstance(comparator, ast.Constant) and isinstance(comparator.value, bool):
                            self._add(
                                "BOOL_COMPARISON", "info", node.lineno,
                                "与 `True`/`False` 显式比较是冗余的。",
                                "直接使用布尔表达式，例如 `if flag:`。",
                                evidence=self._evidence(node.lineno),
                            )

        # 除零风险：除法/取模的两侧是否出现 len(...) / 变量，且上游无判空
        self._check_division_by_zero()

    @staticmethod
    def _dotted_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = PythonAnalyzer._dotted_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    def _check_division_by_zero(self) -> None:
        """启发式检测除零风险：对 `x / len(y)`、`x / y` 这类表达式，
        若函数体内没有任何针对分母的判空/判零逻辑，则给出提示。"""
        assert self.tree is not None
        for func in [n for n in ast.walk(self.tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            guarded: Set[str] = set()
            for node in ast.walk(func):
                # 只有「提前返回 / 抛出 / 跳过」的检查才算真正守住了后续计算，
                # 否则 `if x: print(x)` 这种也会被误判为已守卫。
                if isinstance(node, ast.If) and self._exits_early(node.body):
                    guarded |= self._guarded_names(node.test)
                if isinstance(node, ast.Assert):
                    guarded |= self._guarded_names(node.test)
            for node in ast.walk(func):
                if not isinstance(node, ast.BinOp):
                    continue
                if not isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
                    continue
                divisor = node.right
                target = self._divisor_name(divisor)
                if target and target not in guarded and not isinstance(divisor, ast.Constant):
                    self._add(
                        "DIVISION_BY_ZERO_RISK", "major", node.lineno,
                        f"`{target}` 作为分母，但函数 `{func.name}` 内未见判零/判空逻辑，"
                        "空集合或 0 会触发 `ZeroDivisionError`。",
                        f"在计算前校验：`if not {target}: return 0.0`（或抛出带说明的异常）。",
                        symbol=func.name, evidence=self._evidence(node.lineno),
                    )

    @staticmethod
    def _exits_early(body: List[ast.stmt]) -> bool:
        """判断分支体是否会中断当前流程（提前返回 / 抛出 / 跳过本次循环）。

        这是区分「真守卫」与「普通条件语句」的关键：
        ``if not items: return 0.0`` 守住了后续的除法，
        而 ``if items: print(items)`` 并没有。
        """
        for stmt in body:
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                return True
            # 嵌套分支里也可能提前退出
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, attr, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    if PythonAnalyzer._exits_early(inner):
                        return True
        return False

    @staticmethod
    def _guarded_names(test: ast.AST) -> Set[str]:
        """从条件表达式中提取“被守卫”的变量名。"""
        found: Set[str] = set()
        for node in ast.walk(test):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Call):
                # len(x) -> x
                func = node.func
                if isinstance(func, ast.Name) and func.id == "len" and node.args:
                    inner = node.args[0]
                    if isinstance(inner, ast.Name):
                        found.add(inner.id)
                    elif isinstance(inner, ast.Attribute):
                        found.add(inner.attr)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
        return found

    @staticmethod
    def _divisor_name(divisor: ast.AST) -> str:
        """提取分母的名称，并**归一化**成与 ``_guarded_names`` 一致的裸名字。

        归一化是必需的：``x / len(items)`` 的分母是 ``len(items)``，
        而守卫 ``if not items`` 记录的是 ``items``。两者不做归一化就永远匹配不上，
        会导致「明明有守卫却报除零风险」的误报。
        """
        if isinstance(divisor, ast.Call):
            func = divisor.func
            if isinstance(func, ast.Name) and func.id == "len" and divisor.args:
                return PythonAnalyzer._divisor_name(divisor.args[0])
            return ""
        if isinstance(divisor, ast.Name):
            return divisor.id
        if isinstance(divisor, ast.Attribute):
            # self.total -> total，与守卫侧的记录方式保持一致
            return divisor.attr
        return ""

    # ------------------------------------------------------------------ #
    def _analyze_code_smells(self) -> None:
        assert self.tree is not None

        # 过长的行
        long_lines = [(i, len(line)) for i, line in enumerate(self.lines, 1) if len(line) > 120]
        for line, length in long_lines[:5]:
            self._add(
                "LONG_LINE", "info", line,
                f"该行长度 {length} 字符（PEP 8 建议 ≤ 79，团队常见上限 120）。",
                "拆分为多行，或提取局部变量。",
                evidence=self._evidence(line),
            )

        # 未完成的占位实现
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and self._is_pass_only(node.body):
                if not node.name.startswith("__") and not self._is_abstract(node):
                    self._add(
                        "PLACEHOLDER_IMPLEMENTATION", "minor", node.lineno,
                        f"函数 `{node.name}` 的函数体为空（仅 pass/文档字符串），功能可能尚未实现。",
                        "补充实现，或抛出 `NotImplementedError` 明确标记。",
                        symbol=node.name, evidence=self._evidence(node.lineno),
                    )

        # f-string 无占位符
        for node in ast.walk(self.tree):
            if isinstance(node, ast.JoinedStr):
                if not any(isinstance(v, ast.FormattedValue) for v in node.values):
                    self._add(
                        "REDUNDANT_FSTRING", "info", node.lineno,
                        "f-string 中没有占位符，`f` 前缀是多余的。",
                        "去掉 `f` 前缀，或补上需要的插值。",
                        evidence=self._evidence(node.lineno),
                    )

        # 与内建函数同名（遮蔽）
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in BUILTIN_NAMES and node.name not in {"id", "type", "filter", "map"}:
                    self._add(
                        "SHADOW_BUILTIN", "minor", node.lineno,
                        f"名称 `{node.name}` 与 Python 内建函数同名，会遮蔽内建实现。",
                        f"改名为 `{node.name}_` 或更具业务含义的名称（如 `build_{node.name}`）。",
                        symbol=node.name, evidence=self._evidence(node.lineno),
                    )
            if isinstance(node, ast.arg) and node.arg in BUILTIN_NAMES and node.arg not in {"id", "type", "input"}:
                self._add(
                    "SHADOW_BUILTIN", "info", node.lineno,
                    f"参数名 `{node.arg}` 与内建函数同名，函数内将无法使用该内建。",
                    "重命名参数，例如加后缀下划线。",
                    symbol=node.arg,
                )

        # TODO / FIXME 遗留
        todo_pattern = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.IGNORECASE)
        for index, line in enumerate(self.lines, 1):
            match = todo_pattern.search(line)
            if match:
                self._add(
                    "TODO_COMMENT", "info", index,
                    f"遗留标记 {match.group(1).upper()}：{match.group(2).strip()[:80] or '(无说明)'}",
                    "在提交前处理，或转为 Issue 跟踪。",
                    evidence=line.strip(),
                )

        # 重复代码块（按 6 行滑窗做哈希）
        self._detect_duplicate_blocks(window=6)

        # 未使用的局部变量（函数内赋值但从未读取）
        self._detect_unused_locals()

    @staticmethod
    def _is_abstract(node) -> bool:
        for decorator in getattr(node, "decorator_list", []):
            name = decorator.id if isinstance(decorator, ast.Name) else (
                decorator.attr if isinstance(decorator, ast.Attribute) else ""
            )
            if name in {"abstractmethod", "abstractproperty"}:
                return True
        return False

    def _detect_duplicate_blocks(self, window: int = 6) -> None:
        """检测复制粘贴（DRY 违背）。

        只有「逐字相同」的哈希是不够的：真实的复制粘贴经常被改掉几个字
        （换个返回值、改条报错信息），逻辑结构却一模一样。
        因此这里对每一行做**结构归一化**后再比对：字符串→``S``、数字→``N``、
        标识符→``V``、关键字/运算符保留。这样
        ``result["error"] = "no items"`` 与 ``return False`` 都被归一到同一种形状，
        结构完全相同的代码就有机会被匹配上。
        """
        normalized = [self._normalize_line(line) for line in self.lines]
        effective = [n for n in normalized if n]
        if len(effective) < 8:
            return

        reported_lines: Set[int] = set()
        for size in (8, 6, 5):
            if len(effective) < size:
                continue
            seen: Dict[str, int] = {}
            for start in range(0, len(effective) - size + 1):
                chunk = effective[start : start + size]
                # 至少 3 种不同的行，避免 `pass` 之类的重复堆叠被误报
                if len(set(chunk)) < 3:
                    continue
                digest = hashlib.md5("\n".join(chunk).encode("utf-8")).hexdigest()
                first = seen.get(digest)
                if first is None:
                    seen[digest] = start + 1
                    continue
                if any(abs(start + 1 - line) < size for line in reported_lines):
                    continue
                reported_lines.add(start + 1)
                source_line = self._source_line_of(normalized, start)
                first_line = self._source_line_of(normalized, first - 1)
                self._add(
                    "DUPLICATE_CODE", "minor", source_line,
                    f"该处 {size} 行代码与第 {first_line} 行处结构完全相同"
                    "（仅字面量/命名不同），属于复制粘贴。",
                    "提取为公共函数或使用模板方法，让规则只有一处定义（DRY 原则）。",
                    evidence=self._evidence(source_line),
                )

    @staticmethod
    def _normalize_line(line: str) -> str:
        """把一行代码归一化成「结构形状」，用于识别换了名字的复制粘贴。"""
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if not code.strip():
            return ""
        tokens = re.findall(
            r'""".*?"""|\'\'\'.*?\'\'\'|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''
            r"|\d+\.\d+|\d+|[A-Za-z_]\w*|\S",
            code,
        )
        shape = []
        for token in tokens:
            if token[:1] in "\"'" or token[:3] in ('"""', "'''"):
                shape.append("S")
            elif token[0].isdigit():
                shape.append("N")
            elif token[0].isalpha() or token[0] == "_":
                shape.append(token if token in KEYWORDS else "V")
            else:
                shape.append(token)
        return " ".join(shape)

    @staticmethod
    def _source_line_of(normalized: List[str], effective_index: int) -> int:
        """把「有效行序号」映射回真实行号。"""
        count = -1
        for index, value in enumerate(normalized):
            if value:
                count += 1
                if count == effective_index:
                    return index + 1
        return effective_index + 1

    def _detect_unused_locals(self) -> None:
        assert self.tree is not None
        for func in [n for n in ast.walk(self.tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            assigned: Dict[str, int] = {}
            loaded: Set[str] = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Name):
                    if isinstance(node.ctx, ast.Store):
                        assigned.setdefault(node.id, node.lineno)
                    else:
                        loaded.add(node.id)
                elif isinstance(node, ast.Attribute):
                    root = node
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name):
                        loaded.add(root.id)
            for name, line in assigned.items():
                if name in loaded or name.startswith("_"):
                    continue
                if name in {"self", "cls"}:
                    continue
                self._add(
                    "UNUSED_VARIABLE", "minor", line,
                    f"局部变量 `{name}` 被赋值但从未使用。",
                    "删除该赋值，或确认是否漏用了它（常见于重构后的残留）。",
                    symbol=name, evidence=self._evidence(line),
                )

    # ------------------------------------------------------------------ #
    def _finalize_metrics(self) -> None:
        # docstring 行数从总行数里扣掉，避免 code_lines 重复统计
        self.metrics.code_lines = max(
            0, self.metrics.code_lines - self.metrics.docstring_lines
        )


# --------------------------------------------------------------------------- #
# 便捷入口
# --------------------------------------------------------------------------- #


def analyze_source(source: str, path: str = "<string>") -> Tuple[FileMetrics, List[Finding]]:
    return PythonAnalyzer(source, path).analyze()


def analyze_file(path: Path) -> Tuple[FileMetrics, List[Finding]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return analyze_source(text, str(path))


def summarize_findings(findings: List[Finding]) -> Dict[str, int]:
    summary = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for finding in findings:
        summary[finding.severity] = summary.get(finding.severity, 0) + 1
    return summary


def render_findings(findings: List[Finding]) -> str:
    """把发现渲染成给 LLM / 人类看的紧凑文本。"""
    if not findings:
        return "（静态分析未发现问题）"
    lines = []
    for finding in findings:
        icon = SEVERITY_ICON.get(finding.severity, "•")
        lines.append(
            f"{icon} [{finding.rule}] 第 {finding.line} 行"
            + (f" ({finding.symbol})" if finding.symbol else "")
            + f" {finding.message}"
            + (f"\n    证据: {finding.evidence}" if finding.evidence else "")
            + (f"\n    建议: {finding.suggestion}" if finding.suggestion else "")
        )
    return "\n".join(lines)
