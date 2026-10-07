"""报告输出工具：把 Agent 的最终结论落盘为 Markdown 文件。

Agent 的产出需要「可交付」：一条命令跑完，仓库里就多出一份可以直接提交的报告。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

from .base import ToolRegistry, ToolResult
from .common import resolve_path

DEFAULT_REPORT_DIR = "reports"


def register_report_tool(registry: ToolRegistry, workspace: Path) -> None:
    """注册报告写入工具。"""

    @registry.register_tool(
        name="write_report",
        description=(
            "把代码审查结论保存为 Markdown 报告文件。"
            "在你已经完成分析、准备输出最终结论时调用，报告会写入 reports/ 目录。"
            "传入的 content 必须是完整的 Markdown 正文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "报告的完整 Markdown 正文"},
                "filename": {
                    "type": "string",
                    "description": f"文件名（不含目录），默认自动按时间命名，保存到 {DEFAULT_REPORT_DIR}/ 下",
                },
                "target": {"type": "string", "description": "被审查的目标（文件或目录），用于报告标题"},
            },
            "required": ["content"],
        },
        examples=["write_report(content='## 审查结论...', filename='review.md')"],
    )
    def write_report(content: str, filename: str = "", target: str = "") -> ToolResult:
        if not str(content).strip():
            return ToolResult.fail("报告内容不能为空。")

        report_dir = workspace / DEFAULT_REPORT_DIR
        report_dir.mkdir(parents=True, exist_ok=True)

        if filename and Path(filename).suffix.lower() in {".md", ".markdown"}:
            safe_name = Path(filename).name  # 去掉任何目录成分，防止越界
        else:
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_name = f"review-{stamp}.md"

        destination = (report_dir / safe_name).resolve()
        if report_dir.resolve() not in destination.parents and destination.parent != report_dir.resolve():
            return ToolResult.fail("文件名非法。")

        header = (
            f"<!-- 由 Code Review Agent 自动生成 "
            f"{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -->\n"
        )
        if target and not content.lstrip().startswith("#"):
            header += f"# 代码审查报告：{target}\n\n"

        destination.write_text(header + content.rstrip() + "\n", encoding="utf-8")
        size = destination.stat().st_size
        return ToolResult.success(
            f"报告已写入：{destination.relative_to(workspace).as_posix()}（{size} 字节）",
            path=destination.relative_to(workspace).as_posix(),
            bytes=size,
        )
