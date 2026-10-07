"""代码执行类工具（有副作用，属于高风险工具）。

安全设计
--------
1. **总开关**：``CODEAGENT_ENABLE_EXEC=false`` 时，这些工具根本不会暴露给 LLM。
2. **超时**：子进程强制超时，避免死循环卡死 Agent。
3. **输出截断**：防止模型上下文被海量日志撑爆。
4. **路径沙箱**：工作目录限制在 ``CODEAGENT_WORKSPACE`` 内。
5. **显式标记**：工具描述里写明「会真实执行代码」，让模型与用户都知情。

注意：这是**作业级**沙箱，不是生产级隔离。真实产品应放到容器/虚拟机中执行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .base import ToolRegistry, ToolResult
from .common import iter_files, read_text, resolve_path

MAX_OUTPUT_CHARS = 6000
RUNNABLE_SUFFIXES = {".py", ".js", ".ts", ".java", ".go", ".rs", ".rb", ".php", ".sh", ".c", ".cpp"}


def _find_python() -> str:
    """寻找可用的 Python 解释器（优先当前进程的解释器）。"""
    if sys.executable and Path(sys.executable).exists():
        return sys.executable
    for candidate in ("python3", "python", "py"):
        found = shutil.which(candidate)
        if found:
            return found
    return "python"


def _run_command(
    command: List[str],
    cwd: Path,
    timeout: int,
    *,
    env_extra: Optional[dict] = None,
) -> Tuple[int, str, str, float, bool]:
    """执行子进程，返回 (returncode, stdout, stderr, 耗时, 是否超时)。"""
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)

    start = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - 受控输入，作业场景可接受
            command,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - start
        return completed.returncode, completed.stdout, completed.stderr, elapsed, False
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return -1, stdout, stderr + f"\n[超时] 执行超过 {timeout} 秒已被强制终止", elapsed, True
    except FileNotFoundError as exc:
        elapsed = time.perf_counter() - start
        return -2, "", f"[环境错误] 找不到可执行文件：{exc}", elapsed, False
    except OSError as exc:
        elapsed = time.perf_counter() - start
        return -3, "", f"[环境错误] {type(exc).__name__}: {exc}", elapsed, False


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...（省略 {len(text) - limit} 字符）...\n{text[-half:]}"


def _format_result(name: str, code: int, out: str, err: str, elapsed: float, timed_out: bool) -> str:
    status = "⏱️ 超时" if timed_out else ("✅ 成功" if code == 0 else f"❌ 失败（退出码 {code}）")
    parts = [f"{name} {status}｜耗时 {elapsed:.2f}s"]
    if out.strip():
        parts.append(f"--- stdout ---\n{_truncate(out.strip())}")
    if err.strip():
        parts.append(f"--- stderr ---\n{_truncate(err.strip())}")
    if not out.strip() and not err.strip():
        parts.append("（无任何输出）")
    return "\n".join(parts)


def register_exec_tools(registry: ToolRegistry, workspace: Path, *, exec_timeout: int = 20) -> None:
    """注册执行类工具。"""

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="execute_code",
        description=(
            "在受限子进程中真实执行一段 Python 代码并返回输出（会真实运行，请谨慎）。"
            "常用于：验证某段逻辑是否真的报错、复现 Bug、检查修复方案是否可行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 源码"},
                "timeout": {"type": "integer", "description": f"超时秒数，默认 {exec_timeout}"},
                "stdin": {"type": "string", "description": "可选的标准输入内容"},
            },
            "required": ["code"],
        },
        requires_exec=True,
        dangerous=True,
        examples=["execute_code(code='print(sum(range(10)))')"],
    )
    def execute_code(code: str, timeout: int = 0, stdin: str = "") -> ToolResult:
        if not str(code).strip():
            return ToolResult.fail("code 不能为空。")
        limit = int(timeout) if int(timeout or 0) > 0 else exec_timeout
        limit = min(limit, 120)

        with tempfile.TemporaryDirectory(prefix="cra_exec_") as temp_dir:
            script = Path(temp_dir) / "snippet.py"
            script.write_text(code, encoding="utf-8")
            env_extra = {"PYTHONPATH": str(workspace)}
            command = [_find_python(), "-u", str(script)]
            if stdin:
                # 用管道喂输入
                process = subprocess.run(  # noqa: S603
                    command,
                    cwd=str(workspace),
                    input=stdin,
                    capture_output=True,
                    timeout=limit,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env={**os.environ, **env_extra, "PYTHONIOENCODING": "utf-8"},
                )
                text = _format_result(
                    "代码执行", process.returncode, process.stdout, process.stderr, 0.0, False
                )
                return ToolResult.success(text, returncode=process.returncode)

            code_rc, out, err, elapsed, timed_out = _run_command(command, workspace, limit, env_extra=env_extra)

        return ToolResult.success(
            _format_result("代码执行", code_rc, out, err, elapsed, timed_out),
            returncode=code_rc,
            timed_out=timed_out,
            elapsed=round(elapsed, 3),
        )

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="run_tests",
        description=(
            "运行项目中的 Python 测试：优先使用 pytest（若已安装），否则回退到标准库 unittest。"
            "返回通过/失败统计与失败详情，用于验证代码正确性或验证修复是否生效。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "测试文件或目录的相对路径，默认当前目录 '.'"},
                "timeout": {"type": "integer", "description": f"超时秒数，默认 {max(exec_timeout, 60)}"},
                "keyword": {"type": "string", "description": "可选：只运行匹配的测试（pytest -k）"},
            },
            "required": [],
        },
        requires_exec=True,
        examples=["run_tests(path='tests')"],
    )
    def run_tests(path: str = ".", timeout: int = 0, keyword: str = "") -> ToolResult:
        target = resolve_path(path, workspace, must_exist=True)
        limit = min(int(timeout) if int(timeout or 0) > 0 else max(exec_timeout, 60), 300)
        python = _find_python()

        if shutil.which("pytest") or _module_available(python, "pytest"):
            command = [python, "-m", "pytest", str(target), "-q", "--no-header", "-p", "no:cacheprovider"]
            if keyword:
                command += ["-k", keyword]
            runner = "pytest"
        else:
            if target.is_file():
                command = [python, "-m", "unittest", "-v", str(target)]
            else:
                command = [python, "-m", "unittest", "discover", "-s", str(target), "-v"]
            runner = "unittest"

        rc, out, err, elapsed, timed_out = _run_command(command, workspace, limit)
        text = _format_result(f"测试运行（{runner}）", rc, out, err, elapsed, timed_out)
        if rc != 0 and not timed_out:
            text += "\n\n提示：请结合失败堆栈定位根因；若为环境问题（缺少依赖）请单独说明。"
        return ToolResult.success(
            text, runner=runner, returncode=rc, elapsed=round(elapsed, 3), timed_out=timed_out
        )

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="analyze_with_runtime",
        description=(
            "对一个 Python 文件做“运行期体检”：用 ast 找出所有公开函数，"
            "再在子进程中以典型边界输入（空列表 / 0 / None / 空字符串）逐个调用，"
            "捕获真实运行期异常（如 ZeroDivisionError、TypeError）。"
            "这是静态分析的有力补充——能抓到只有跑起来才暴露的 Bug。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的 Python 文件路径"},
                "timeout": {"type": "integer", "description": f"超时秒数，默认 {exec_timeout}"},
            },
            "required": ["path"],
        },
        requires_exec=True,
        examples=["analyze_with_runtime(path='demo/sample_buggy.py')"],
    )
    def analyze_with_runtime(path: str, timeout: int = 0) -> ToolResult:
        target = resolve_path(path, workspace, must_exist=True)
        if target.is_dir():
            return ToolResult.fail("请提供具体文件路径。")
        limit = min(int(timeout) if int(timeout or 0) > 0 else exec_timeout, 60)

        probe = _build_probe_script(target, workspace)
        with tempfile.TemporaryDirectory(prefix="cra_probe_") as temp_dir:
            script = Path(temp_dir) / "probe.py"
            script.write_text(probe, encoding="utf-8")
            rc, out, err, elapsed, timed_out = _run_command(
                [_find_python(), "-u", str(script)],
                workspace,
                limit,
                env_extra={"PYTHONPATH": str(workspace)},
            )
        text = _format_result("运行期体检", rc, out, err, elapsed, timed_out)
        return ToolResult.success(text, returncode=rc, elapsed=round(elapsed, 3))


def _module_available(python: str, module: str) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603
            [python, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('{module}') else 1)"],
            capture_output=True,
            timeout=20,
        )
        return completed.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败就当作不可用
        return False


def _build_probe_script(target: Path, workspace: Path) -> str:
    """生成探测脚本：反射调用目标模块中的公开函数，喂边界输入，记录真实异常。"""
    return _PROBE_TEMPLATE.replace("__TARGET_PATH__", str(target).replace("\\", "\\\\"))


_PROBE_TEMPLATE = r'''
# -*- coding: utf-8 -*-
"""自动生成的运行期体检脚本（由 code-review-agent 注入，可安全删除）。"""
import contextlib
import importlib.util
import inspect
import io
import sys
import traceback

TARGET = "__TARGET_PATH__"

# 单参数场景的边界输入
EDGE_VALUES = [[], {}, "", 0, 0.0, None, [0], [1, -1], "abc", -1]
# 多参数场景的填充值（按位置依次取用）
FILLERS = [None, 0, "", [], {}, 1.5, [1, 2], "x"]

print("=" * 62)
print("运行期体检报告:", TARGET)
print("=" * 62)

try:
    spec = importlib.util.spec_from_file_location("probe_target", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception:
    print("[模块导入失败]")
    traceback.print_exc()
    sys.exit(0)


def public_functions(mod):
    found = []
    for name, obj in vars(mod).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and getattr(obj, "__module__", "") == mod.__name__:
            found.append((name, obj))
    return sorted(found)


functions = public_functions(module)
if not functions:
    print("未发现可探测的公开函数。")
    sys.exit(0)

print("发现 %d 个公开函数，开始边界输入探测\n" % len(functions))

issues = 0
executed = 0

for name, func in functions:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        print("--- %s(无法获取签名，跳过) ---" % name)
        continue

    params = [
        p for p in signature.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    required = [p for p in params if p.default is p.empty]
    print("--- %s%s --" % (name, signature))

    if not required:
        # 无必填参数：直接调用一次看是否报错
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                func()
            executed += 1
            print("  正常返回（无必填参数）")
        except Exception as exc:
            executed += 1
            issues += 1
            print("  [!] 调用 %s() -> %s: %s" % (name, type(exc).__name__, exc))
        continue

    trials = []
    if len(required) == 1:
        trials = [(v,) for v in EDGE_VALUES]
    else:
        # 多参数：固定填充值，逐个把第 1 个参数替换为边界值
        base = [FILLERS[i % len(FILLERS)] for i in range(len(required))]
        trials = [tuple([v] + base[1:]) for v in EDGE_VALUES]
        trials.append(tuple(base))

    seen = set()
    for args in trials:
        label = repr(args[0]) if len(args) == 1 else repr(args)
        if label in seen:
            continue
        seen.add(label)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                func(*args)
            executed += 1
        except Exception as exc:
            executed += 1
            issues += 1
            print("  [!] %s -> %s: %s" % (label, type(exc).__name__, exc))

print()
if executed == 0:
    print("未执行任何调用（函数签名过于复杂）。")
else:
    print("探测完成: 执行 %d 次调用, 发现 %d 处运行期异常。" % (executed, issues))
if issues:
    print("提示: 上述异常不一定都是 Bug，但值得逐个确认边界处理是否正确。")
'''.strip()
