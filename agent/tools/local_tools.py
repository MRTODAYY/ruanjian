"""本地文件与静态分析类工具。

这些工具的定位是给 LLM 提供**可信事实**：
LLM 不擅长精确数行数、算复杂度、找未使用变量，但它擅长解释与给出改进方案。
把「度量」交给工具、把「判断」交给模型，是构建可靠 Agent 的关键分工。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..analyzer import (
    SEVERITY_ICON,
    SEVERITY_LABEL,
    analyze_source,
    render_findings,
    summarize_findings,
)
from .base import ToolRegistry, ToolResult
from .common import (
    DEFAULT_IGNORED_DIRS,
    is_probably_text,
    iter_files,
    language_of,
    numbered,
    read_text,
    resolve_path,
)


def register_local_tools(registry: ToolRegistry, workspace: Path) -> None:
    """把只读的文件/分析工具注册进注册表。"""

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="list_files",
        description=(
            "列出工作目录下的文件，支持通配符（如 *.py、**/*.java 用 *.java 表示）。"
            "用于在审查前先了解项目结构，决定要读哪些文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的路径，默认为当前目录 '.'"},
                "pattern": {"type": "string", "description": "文件名通配符，多个用逗号分隔，默认 '*'"},
                "max_files": {"type": "integer", "description": "最多返回多少条，默认 200"},
            },
            "required": [],
        },
        examples=["list_files(path='src', pattern='*.py')"],
    )
    def list_files(path: str = ".", pattern: str = "*", max_files: int = 200) -> ToolResult:
        root = resolve_path(path, workspace, must_exist=True)
        files = iter_files(root, pattern, max_files=int(max_files) or 200)
        if not files:
            return ToolResult.success(
                f"在 `{path}` 下没有匹配 `{pattern}` 的文件。",
                count=0,
            )
        rows = []
        total_bytes = 0
        for file in files:
            try:
                size = file.stat().st_size
            except OSError:
                size = 0
            total_bytes += size
            rows.append(
                f"{file.relative_to(workspace).as_posix()}  ({size} B, {language_of(file)})"
            )
        listing = "\n".join(rows)
        return ToolResult.success(
            f"共 {len(files)} 个文件（相对路径 {workspace}）：\n{listing}",
            count=len(files),
            total_bytes=total_bytes,
            files=[r.split("  (")[0] for r in rows],
        )

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="read_file",
        description=(
            "读取文本文件内容，带行号返回（行号用于精确引用问题位置）。"
            "可用 start_line/end_line 只读取片段，避免大文件占满上下文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的文件路径"},
                "start_line": {"type": "integer", "description": "起始行（从 1 开始），默认 1"},
                "end_line": {"type": "integer", "description": "结束行，默认到文件末尾"},
            },
            "required": ["path"],
        },
        examples=["read_file(path='demo/sample_buggy.py')"],
    )
    def read_file(path: str, start_line: int = 1, end_line: int = 0) -> ToolResult:
        target = resolve_path(path, workspace, must_exist=True)
        if target.is_dir():
            return ToolResult.fail(f"`{path}` 是目录，请使用 list_files 查看其内容。")
        if not is_probably_text(target):
            return ToolResult.fail(f"`{path}` 看起来是二进制文件，无法作为文本审查。")
        text, truncated = read_text(target)
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        end = int(end_line or 0) or len(lines)
        end = min(end, len(lines))
        snippet = "\n".join(lines[start - 1 : end])
        header = (
            f"文件 {target.relative_to(workspace).as_posix()} "
            f"（共 {len(lines)} 行，语言 {language_of(target)}"
            + (f"，已截断至前 {len(text)} 字节" if truncated else "")
            + f"，显示第 {start}-{end} 行）"
        )
        return ToolResult.success(
            f"{header}\n{numbered(snippet, start=start)}",
            path=target.relative_to(workspace).as_posix(),
            total_lines=len(lines),
            shown=(start, end),
        )

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="static_analyze",
        description=(
            "对 Python 文件做 AST 静态分析（本地确定性计算，不依赖 LLM）："
            "返回代码度量（行数、函数数、圈复杂度、嵌套深度、类型注解与文档覆盖率）"
            "以及按严重级别排列的问题清单（含精确行号、证据行与修复建议）。"
            "审查 Python 代码时应优先调用本工具，再结合 read_file 给出最终结论。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的 Python 文件路径"},
                "severity_filter": {
                    "type": "string",
                    "description": "只显示不低于该级别的问题：critical / major / minor / info，默认全部",
                },
            },
            "required": ["path"],
        },
        examples=["static_analyze(path='demo/sample_buggy.py')"],
    )
    def static_analyze(path: str, severity_filter: str = "") -> ToolResult:
        target = resolve_path(path, workspace, must_exist=True)
        if target.is_dir():
            return ToolResult.fail("static_analyze 需要具体文件路径，目录请先用 list_files。")
        text, _ = read_text(target)
        metrics, findings = analyze_source(text, str(target))

        threshold = {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(
            (severity_filter or "").strip().lower(), 99
        )
        shown = [
            f
            for f in findings
            if {"critical": 0, "major": 1, "minor": 2, "info": 3}.get(f.severity, 9) <= threshold
        ] if threshold != 99 else findings

        summary = summarize_findings(findings)
        header = (
            f"静态分析结果：{target.relative_to(workspace).as_posix()}\n"
            f"度量：总行数 {metrics.total_lines}｜代码行 {metrics.code_lines}｜注释行 {metrics.comment_lines}｜"
            f"函数 {metrics.functions}｜类 {metrics.classes}｜导入 {metrics.imports}\n"
            f"复杂度：最大 {metrics.max_complexity}｜平均 {metrics.avg_complexity}｜最大嵌套 {metrics.max_nesting}\n"
            f"质量：文档覆盖 {metrics.documented_functions}/{metrics.functions}｜"
            f"类型注解覆盖 {metrics.type_hinted_functions}/{metrics.functions}\n"
            f"问题分布：🔴严重 {summary['critical']}｜🟠重要 {summary['major']}｜"
            f"🟡次要 {summary['minor']}｜🔵提示 {summary['info']}"
        )
        if not metrics.parse_ok:
            header += f"\n⚠️ 语法错误：{metrics.parse_error}"
        body = "\n\n".join(
            [header, f"热点函数（复杂度最高的前 5 个）：\n" + json.dumps(
                metrics.complexity_hotspots, ensure_ascii=False, indent=2
            ), "问题清单：\n" + render_findings(shown)]
        )
        return ToolResult.success(
            body,
            summary=summary,
            metrics=metrics.to_dict(),
            findings=[f.to_dict() for f in shown],
        )

    # ------------------------------------------------------------------ #
    @registry.register_tool(
        name="detect_code_smells",
        description=(
            "检测“坏味道”（重构成因），例如：重复代码块、超长函数、深层嵌套、"
            "参数过多、死代码、魔法数字、注释掉的代码、占位实现等。"
            "适合在重构建议场景使用，输出会按严重程度排序。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的文件路径"},
            },
            "required": ["path"],
        },
        examples=["detect_code_smells(path='demo/sample_smelly.py')"],
    )
    def detect_code_smells(path: str) -> ToolResult:
        target = resolve_path(path, workspace, must_exist=True)
        text, _ = read_text(target)
        metrics, findings = analyze_source(text, str(target))

        smell_rules = {
            "LONG_FUNCTION", "DEEP_NESTING", "TOO_MANY_PARAMS", "DUPLICATE_CODE",
            "HIGH_COMPLEXITY", "MUTABLE_DEFAULT_ARG", "MUTABLE_CLASS_ATTRIBUTE",
            "UNREACHABLE_CODE", "PLACEHOLDER_IMPLEMENTATION", "UNUSED_VARIABLE",
            "UNUSED_IMPORT", "SHADOW_BUILTIN", "MISSING_DOCSTRING", "MISSING_TYPE_HINT",
            "LONG_LINE", "REDUNDANT_FSTRING",
        }
        smells = [f for f in findings if f.rule in smell_rules]

        # 补充：魔法数字与注释掉的代码（这两项需要看原始文本）
        extra = _detect_text_smells(text)
        summary = summarize_findings(smells + extra)
        header = (
            f"坏味道检测：{target.relative_to(workspace).as_posix()}\n"
            f"共 {len(smells) + len(extra)} 项："
            + "｜".join(f"{SEVERITY_ICON[k]}{SEVERITY_LABEL[k]} {v}" for k, v in summary.items())
        )
        return ToolResult.success(
            header + "\n\n" + render_findings(smells + extra),
            summary=summary,
            findings=[f.to_dict() for f in smells + extra],
        )

    return None


# --------------------------------------------------------------------------- #
# 文本级坏味道检测
# --------------------------------------------------------------------------- #

_MAGIC_NUMBER_OK = {"0", "1", "2", "-1", "0.0", "1.0", "100", "10"}
_MAGIC_PATTERN = None


def _detect_text_smells(text: str):
    import re

    from ..analyzer import Finding

    findings = []
    lines = text.splitlines()
    magic_re = re.compile(r"(?<![\w.])(\d{3,}|\d+\.\d+)(?![\w.])")
    comment_code_re = re.compile(r"^\s*#\s*(if |for |while |def |class |return |import |print\()")

    for index, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 跳过字符串常量行，降低误报
        if stripped.startswith(("\"", "'")) or "=" in stripped and stripped.count("\"") >= 2:
            continue
        for match in magic_re.finditer(line):
            if match.group(1) in _MAGIC_NUMBER_OK:
                continue
            findings.append(
                Finding(
                    rule="MAGIC_NUMBER", severity="info", line=index,
                    message=f"出现魔法数字 {match.group(1)}，含义需要靠猜。",
                    suggestion="提取为具名常量（如 `MAX_RETRY = 3`）或枚举，提高可读性。",
                    evidence=stripped[:120], source="smell",
                )
            )
            break

    for index, line in enumerate(lines, 1):
        if comment_code_re.match(line):
            findings.append(
                Finding(
                    rule="COMMENTED_OUT_CODE", severity="minor", line=index,
                    message="疑似被注释掉的代码残留。",
                    suggestion="删除无用代码，交给版本控制记录历史，避免文件越来越脏。",
                    evidence=line.strip()[:120], source="smell",
                )
            )
    return findings
