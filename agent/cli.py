"""命令行界面（CLI）。

命令一览::

    python -m agent review demo/sample_buggy.py        # 代码审查（核心功能）
    python -m agent review src/ --report               # 审查整个目录并存报告
    python -m agent explain demo/sample_buggy.py       # 代码解释
    python -m agent test  demo/sample_buggy.py         # 生成单元测试
    python -m agent refactor src/                      # 重构建议
    python -m agent ask "这个项目用了什么设计模式？"    # 自由问答
    python -m agent repl                               # 交互式多轮对话
    python -m agent tools                              # 查看工具清单
    python -m agent selftest                           # 自检（无需 API Key）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .console import Console, setup_console
from .core import AgentRun, CodeReviewAgent
from .llm import LLMClient, MockLLMClient, default_mock_script
from .prompts import PromptLibrary
from .tools import build_registry

MODE_CHOICES = ["review", "explain", "test", "refactor", "ask"]


# --------------------------------------------------------------------------- #
# 事件渲染：把 Agent 的每一步实时展示出来（Agent 的「可解释性」体现）
# --------------------------------------------------------------------------- #
class TracePrinter:
    """订阅 Agent 事件流，实时打印执行轨迹。"""

    def __init__(self, console: Console, *, verbose: bool = False, max_steps: int = 14):
        self.console = console
        self.verbose = verbose
        self.max_steps = max_steps
        self.tool_count = 0
        self._in_step = False

    def __call__(self, event: str, payload: Dict[str, Any]) -> None:
        if event == "start":
            names = payload.get("tools") or []
            self.console.dim(f"模型：{payload.get('model')}｜可用工具 {len(names)} 个：{', '.join(names)}")
        elif event == "step":
            self.console.step(payload["step"], payload["max_steps"], "推理中…")
        elif event == "thought":
            text = (payload.get("text") or "").strip()
            if text:
                preview = text if len(text) <= 700 else text[:700] + " …"
                for line in preview.splitlines():
                    self.console.write(self.console.paint(f"     │ {line}", "dim"))
        elif event == "tool_start":
            self.tool_count += 1
            args = json.dumps(payload.get("args") or {}, ensure_ascii=False)
            if len(args) > 110:
                args = args[:110] + "…"
            self.console.write(
                self.console.paint("     ├─ 调用工具 ", "blue")
                + self.console.paint(f"{payload.get('name')}", "bold", "blue")
                + self.console.paint(f" {args}", "gray")
            )
        elif event == "tool_call":
            ok = payload.get("ok", True)
            mark = self.console.paint("✔", "green") if ok else self.console.paint("✖", "red")
            duration = payload.get("duration_ms", 0)
            self.console.write(f"     │  {mark} 返回（{duration} ms）")
            if self.verbose or not ok:
                preview = (payload.get("preview") or "").strip()
                for line in preview.splitlines()[:self._preview_lines(ok)]:
                    self.console.write(self.console.paint(f"     │    {line}", "gray"))
        elif event == "compact":
            self.console.dim(f"上下文已压缩（省略 {payload.get('dropped')} 条历史消息）")
        elif event == "error":
            self.console.warn(payload.get("message", ""))

    @staticmethod
    def _preview_lines(ok: bool) -> int:
        return 3 if ok else 12


# --------------------------------------------------------------------------- #
# 公共构建
# --------------------------------------------------------------------------- #
def _build_config(args: argparse.Namespace) -> Config:
    config = Config.load()
    if getattr(args, "model", None):
        config.model = args.model
    if getattr(args, "base_url", None):
        config.base_url = args.base_url.rstrip("/")
    if getattr(args, "api_key", None):
        config.api_key = args.api_key
    if getattr(args, "max_steps", None):
        config.max_steps = max(1, args.max_steps)
    if getattr(args, "no_exec", False):
        config.enable_exec = False
    if getattr(args, "workspace", None):
        config.workspace = Path(args.workspace).expanduser().resolve()
    return config


def _build_agent(config: Config, console: Console, args: argparse.Namespace):
    registry = build_registry(
        config.workspace,
        enable_exec=config.enable_exec,
        exec_timeout=config.exec_timeout,
    )
    if getattr(args, "mock", False):
        llm: LLMClient = MockLLMClient(config, default_mock_script(), verbose=args.verbose)
    else:
        llm = LLMClient(config, verbose=args.verbose)
    printer = TracePrinter(console, verbose=args.verbose, max_steps=config.max_steps)
    agent = CodeReviewAgent(config, llm, registry, on_event=printer)
    return agent, llm, registry


def _require_key(config: Config, console: Console, args: argparse.Namespace) -> bool:
    if config.has_api_key and not getattr(args, "mock", False):
        return True
    if getattr(args, "mock", False):
        console.dim("运行于 Mock 模式：使用内置假回复，不消耗 API 额度。")
        return True
    console.error("未找到可用的 API Key。")
    console.info("请任选一种方式配置：")
    console.info("  1) 复制 .env.example 为 .env，填写 CODEAGENT_API_KEY")
    console.info("  2) 设置环境变量 CODEAGENT_API_KEY=sk-xxxx")
    console.info("  3) 加参数 --api-key sk-xxxx")
    console.info("  4) 先体验流程：加参数 --mock（离线假回复）")
    return False


def _print_run_summary(console: Console, run: AgentRun, llm: LLMClient) -> None:
    console.write()
    console.write(console.paint("  ── 运行统计 " + "─" * 40, "gray"))
    console.info(
        f"步数 {run.steps}/{llm.config.max_steps}｜工具调用 {run.tool_calls} 次"
        f"（失败 {run.tool_failures}）｜耗时 {run.duration_ms / 1000:.1f}s"
    )
    console.info(
        f"Token：输入 {run.prompt_tokens}｜输出 {run.completion_tokens}"
        f"｜合计 {run.prompt_tokens + run.completion_tokens}"
    )
    if run.errors:
        for message in run.errors:
            console.warn(message)
    if run.stop_reason != "completed":
        console.warn(f"停止原因：{run.stop_reason}")


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def _run_task(args: argparse.Namespace, mode: str, query: str) -> int:
    console = Console(quiet=args.quiet, color=None if not args.no_color else False)
    config = _build_config(args)

    if not _require_key(config, console, args):
        return 2

    if args.verbose:
        console.dim(f"配置：{config.describe()}")

    target = Path(query).expanduser()
    if not target.is_absolute():
        target = config.workspace / target
    if mode in {"review", "explain", "test", "refactor"} and not target.exists():
        console.error(f"目标不存在：{query}")
        console.info(f"当前工作目录：{config.workspace}")
        return 2

    agent, llm, registry = _build_agent(config, console, args)

    if not args.quiet:
        console.banner(f"模式：{PromptLibrary.mode_label(mode)}｜模型：{config.model}")
        console.dim(f"工作目录：{config.workspace}")
        console.dim(f"待审查目标：{query}")

    started = time.perf_counter()
    run = agent.run(query, mode=mode, goals=args.goals or "")
    elapsed = time.perf_counter() - started

    # 输出最终报告
    if not args.quiet:
        console.title("审查报告")
    console.write(run.answer, force=True)
    console.write(force=True)

    if not args.quiet:
        _print_run_summary(console, run, llm)

    # 落盘
    if args.report:
        report_path = _save_report(config, run, agent, query=query, mode=mode)
        console.success(f"报告已保存：{report_path.relative_to(config.workspace).as_posix()}")
    if args.json:
        payload = run.to_dict()
        payload["mode"] = mode
        payload["target"] = query
        payload["model"] = config.model
        payload["elapsed_seconds"] = round(elapsed, 2)
        json_path = Path(args.json)
        if not json_path.is_absolute():
            json_path = config.workspace / json_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.success(f"JSON 结果已保存：{json_path.name}")

    return 0 if run.stop_reason in {"completed", "max_steps"} else 1


def _save_report(config: Config, run: AgentRun, agent: CodeReviewAgent,
                 *, query: str, mode: str) -> Path:
    import datetime as _dt

    reports_dir = config.workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_target = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in query)[:40]
    path = reports_dir / f"{mode}-{safe_target}-{stamp}.md"

    body = [
        f"# 代码审查报告 · {PromptLibrary.mode_label(mode)}",
        "",
        f"- **审查目标**：`{query}`",
        f"- **模型**：`{config.model}`",
        f"- **工作目录**：`{config.workspace}`",
        f"- **生成时间**：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Agent 步数**：{run.steps}｜**工具调用**：{run.tool_calls} 次（失败 {run.tool_failures}）",
        f"- **Token 消耗**：输入 {run.prompt_tokens} / 输出 {run.completion_tokens}",
        "",
        "---",
        "",
        run.answer,
        "",
        "---",
        "",
        agent.transcript_markdown(),
        "",
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
def cmd_review(args: argparse.Namespace) -> int:
    return _run_task(args, "review", args.target)


def cmd_explain(args: argparse.Namespace) -> int:
    return _run_task(args, "explain", args.target)


def cmd_test(args: argparse.Namespace) -> int:
    return _run_task(args, "test", args.target)


def cmd_refactor(args: argparse.Namespace) -> int:
    return _run_task(args, "refactor", args.target)


def cmd_ask(args: argparse.Namespace) -> int:
    return _run_task(args, "ask", args.question)


def cmd_tools(args: argparse.Namespace) -> int:
    console = Console(quiet=False, color=False if args.no_color else None)
    config = _build_config(args)
    registry = build_registry(config.workspace, enable_exec=config.enable_exec,
                              exec_timeout=config.exec_timeout)
    console.banner("工具清单")
    console.dim(f"执行类工具开关 CODEAGENT_ENABLE_EXEC = {config.enable_exec}")
    console.write()
    for tool in registry.available():
        console.write(console.paint(f"  ● {tool.name}", "bold", "cyan"))
        for line in tool.description.strip().splitlines():
            console.info(line)
        required = (tool.parameters or {}).get("required") or []
        props = list(((tool.parameters or {}).get("properties") or {}).keys())
        console.dim(f"    参数：{', '.join(props) or '无'}｜必填：{', '.join(required) or '无'}")
        if tool.examples:
            console.dim(f"    示例：{tool.examples[0]}")
        if tool.dangerous:
            console.warn("    该工具会真实执行代码，请谨慎使用")
        console.write()
    console.info(f"共 {len(registry.available())} 个工具可用。")
    return 0


def cmd_repl(args: argparse.Namespace) -> int:
    console = Console(quiet=False, color=False if args.no_color else None)
    config = _build_config(args)
    if not _require_key(config, console, args):
        return 2
    agent, llm, registry = _build_agent(config, console, args)
    console.banner("交互式对话模式（多轮记忆已启用）")
    console.dim("输入代码路径或问题；输入 :mode review|explain|test|refactor 切换模式；")
    console.dim("输入 :reset 清空记忆；:tools 查看工具；:quit 退出。")
    console.write()

    mode = args.mode
    while True:
        try:
            raw = input(console.paint(f"  你[{mode}] › ", "bold", "green")).strip()
        except (EOFError, KeyboardInterrupt):
            console.write()
            console.info("再见！")
            return 0
        if not raw:
            continue
        if raw in {":quit", ":q", "exit", "quit"}:
            console.info("再见！")
            return 0
        if raw == ":reset":
            agent.reset()
            console.success("对话记忆已清空。")
            continue
        if raw == ":tools":
            console.write(registry.catalog_text())
            continue
        if raw.startswith(":mode"):
            parts = raw.split()
            if len(parts) == 2 and parts[1] in MODE_CHOICES:
                mode = parts[1]
                console.success(f"已切换到 {PromptLibrary.mode_label(mode)} 模式。")
            else:
                console.warn(f"用法：:mode {'|'.join(MODE_CHOICES)}")
            continue

        console.write()
        run = agent.run(raw, mode=mode)
        console.title("回答")
        console.write(run.answer, force=True)
        console.write(force=True)
        console.dim(
            f"步数 {run.steps}｜工具 {run.tool_calls} 次｜"
            f"token {run.prompt_tokens + run.completion_tokens}｜{run.duration_ms / 1000:.1f}s"
        )
        console.write()


def cmd_selftest(args: argparse.Namespace) -> int:
    """不依赖网络与 API Key 的自检：验证工具、分析器、Agent 循环是否正常。"""
    console = Console(quiet=False, color=False if args.no_color else None)
    console.banner("自检（离线，不需要 API Key）")

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            console.success(f"{name}" + (f" — {detail}" if detail else ""))
        else:
            failed += 1
            console.error(f"{name}" + (f" — {detail}" if detail else ""))

    config = _build_config(args)
    console.write()

    # 1) 工具注册
    registry = build_registry(config.workspace, enable_exec=True, exec_timeout=15)
    names = set(registry.names())
    expected = {"list_files", "read_file", "static_analyze", "detect_code_smells",
                "write_report", "execute_code", "run_tests", "analyze_with_runtime"}
    check("工具注册完整", expected <= names, f"{len(names)} 个工具：{', '.join(sorted(names))}")

    # 2) 关闭执行开关时执行类工具被隐藏
    safe_registry = build_registry(config.workspace, enable_exec=False)
    check(
        "安全开关生效",
        not ({"execute_code", "run_tests"} & set(safe_registry.names())),
        "enable_exec=False 时执行类工具不暴露",
    )

    # 3) 静态分析能抓到已知坏味道
    sample = config.workspace / "demo" / "sample_buggy.py"
    if sample.is_file():
        from .analyzer import analyze_file

        metrics, findings = analyze_file(sample)
        rules = {f.rule for f in findings}
        check("静态分析可运行", metrics.parse_ok, f"发现 {len(findings)} 个问题")
        check("能检出可变默认参数", "MUTABLE_DEFAULT_ARG" in rules)
        check("能检出裸 except", "BARE_EXCEPT" in rules)
        check("能检出除零风险", "DIVISION_BY_ZERO_RISK" in rules)
    else:
        check("示例文件存在", False, f"缺少 {sample}")

    # 4) 工具沙箱：越界路径被拒绝
    result = registry.execute("read_file", {"path": "../../etc/passwd"})
    check("路径沙箱生效", not result.ok, result.error[:60])

    # 5) 未知工具被优雅处理
    result = registry.execute("no_such_tool", {})
    check("未知工具容错", not result.ok and "未知工具" in result.error)

    # 6) 参数校验
    result = registry.execute("read_file", {})
    check("参数校验生效", not result.ok and "缺少必填参数" in result.error)

    # 7) 代码执行工具可用
    result = registry.execute("execute_code", {"code": "print(1+1)"})
    check("代码执行工具可用", result.ok and "2" in result.content)

    # 8) Agent 循环（Mock LLM）
    from dataclasses import replace

    from .llm import LLMResponse, MockLLMClient, ToolCall

    mock_config = replace(config, max_steps=6)
    script = [
        LLMResponse(
            content="先看看目录。",
            tool_calls=[ToolCall(id="c1", name="list_files",
                                 arguments={"pattern": "*.py"}, raw_arguments='{"pattern":"*.py"}')],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="## 审查结论\n完成。", finish_reason="stop"),
    ]
    mock_llm = MockLLMClient(mock_config, script)
    agent = CodeReviewAgent(mock_config, mock_llm, registry)
    run = agent.run("demo", mode="review")
    check("Agent 循环可运行", run.steps == 2 and run.tool_calls == 1,
          f"步数 {run.steps}，工具 {run.tool_calls} 次")
    check("Agent 能收敛并给出答案", "审查结论" in run.answer)
    check("轨迹可审计", any(e.kind == "tool_call" for e in run.trace),
          f"{len(run.trace)} 条轨迹")

    # 9) LLM 客户端重试逻辑（注入网络错误）
    from .llm import LLMError

    class FlakyClient(LLMClient):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.calls = 0

        def chat(self, messages, tools=None, *, temperature=None):  # type: ignore[override]
            self.calls += 1
            raise LLMError(f"模拟网络失败 #{self.calls}", retryable=False)

    flaky_config = replace(config, api_key="sk-test", max_retries=0)
    flaky_agent = CodeReviewAgent(flaky_config, FlakyClient(flaky_config), registry)
    flaky_run = flaky_agent.run("demo", mode="review")
    check("LLM 故障不崩溃", flaky_run.stop_reason == "llm_error",
          f"停止原因 {flaky_run.stop_reason}")

    # 10) Prompt 模板齐全
    library = PromptLibrary()
    ok = all(
        library.user_task_prompt("x", mode=m).strip()
        for m in ["review", "explain", "test", "refactor", "ask"]
    )
    check("Prompt 模板齐全", ok, f"{len(PromptLibrary.modes())} 种模式")
    check(
        "Prompt 含防幻觉约束",
        "行号" in library.system_prompt(workspace=".", tools_catalog="-"),
    )

    console.write()
    console.write(console.paint(f"  自检结果：通过 {passed} 项，失败 {failed} 项", "bold"))
    console.write()
    return 0 if failed == 0 else 1


def cmd_version(args: argparse.Namespace) -> int:
    from . import __version__

    console = Console(color=False if args.no_color else None)
    config = _build_config(args)
    console.write(f"Code Review Agent v{__version__}")
    console.write(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    console.write(f"配置: {config.describe()}")
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-agent",
        description="Code Review Agent · 基于 LLM 的代码审查智能体（Homework 1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m agent review demo/sample_buggy.py --report\n"
            "  python -m agent refactor src/ --goals \"重点看异常处理\"\n"
            "  python -m agent selftest\n"
        ),
    )
    parser.add_argument("--model", help="覆盖模型名（默认读取 .env 的 CODEAGENT_MODEL）")
    parser.add_argument("--base-url", dest="base_url", help="覆盖 OpenAI 兼容接口地址")
    parser.add_argument("--api-key", dest="api_key", help="临时指定 API Key（不推荐写入命令历史）")
    parser.add_argument("--max-steps", dest="max_steps", type=int, help="Agent 最大步数")
    parser.add_argument("--workspace", help="工作目录（工具访问范围的根）")
    parser.add_argument("--mock", action="store_true", help="离线 Mock 模式，不调用真实 API")
    parser.add_argument("--no-exec", action="store_true", help="禁用代码执行类工具")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印详细日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终报告")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    def add_task_parser(name: str, target_help: str, default_mode: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=target_help)
        p.add_argument("target", help=target_help)
        p.add_argument("--goals", default="", help="额外的审查重点（自然语言）")
        p.add_argument("--report", action="store_true", help="把报告保存到 reports/ 目录")
        p.add_argument("--json", metavar="FILE", help="把结构化结果保存为 JSON")
        p.set_defaults(func=globals()[f"cmd_{name}"], mode=default_mode)
        return p

    add_task_parser("review", "待审查的文件或目录路径", "review")
    add_task_parser("explain", "待解释的文件或目录路径", "explain")
    add_task_parser("test", "待生成测试的文件路径", "test")
    add_task_parser("refactor", "待重构分析的文件或目录路径", "refactor")

    p_ask = sub.add_parser("ask", help="就代码库自由提问")
    p_ask.add_argument("question", help="你的问题")
    p_ask.add_argument("--goals", default="", help="补充说明")
    p_ask.add_argument("--report", action="store_true", help="把回答保存到 reports/ 目录")
    p_ask.add_argument("--json", metavar="FILE", help="把结果保存为 JSON")
    p_ask.set_defaults(func=cmd_ask, mode="ask")

    p_repl = sub.add_parser("repl", help="交互式多轮对话")
    p_repl.add_argument("--mode", default="review", choices=MODE_CHOICES, help="初始模式")
    p_repl.set_defaults(func=cmd_repl)

    p_tools = sub.add_parser("tools", help="列出所有可用工具")
    p_tools.set_defaults(func=cmd_tools)

    p_self = sub.add_parser("selftest", help="离线自检（无需 API Key）")
    p_self.set_defaults(func=cmd_selftest)

    p_ver = sub.add_parser("version", help="显示版本与配置")
    p_ver.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        # 未指定子命令时，若当前目录看起来是个代码仓库，就直接审查当前目录
        parser.print_help()
        return 0

    if getattr(args, "quiet", False) and getattr(args, "verbose", False):
        parser.error("--quiet 与 --verbose 不能同时使用")

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        Console().write()
        Console().warn("已被用户中断。")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        console = Console()
        console.error(f"运行失败：{type(exc).__name__}: {exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        else:
            console.dim("加 -v 参数可查看完整堆栈。")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
