"""终端输出工具。

Windows 控制台默认编码经常是 GBK，直接打印中文/emoji 会抛 ``UnicodeEncodeError``
或者变成乱码。这里统一把标准输出切到 UTF-8，并提供带颜色的小封装。
"""

from __future__ import annotations

import io
import os
import sys

_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}


def setup_console() -> None:
    """把 stdout/stderr 切到 UTF-8，并尽量启用 ANSI 颜色。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
        elif hasattr(stream, "buffer"):
            try:
                setattr(
                    sys,
                    stream_name,
                    io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
                )
            except (ValueError, OSError):
                pass

    if os.name == "nt":
        try:  # 让 Windows 终端识别 ANSI 转义序列
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:  # noqa: BLE001 - 非交互终端会失败，忽略即可
            pass


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CODEAGENT_NO_COLOR"):
        return False
    return sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False


class Console:
    """极简控制台输出封装（无第三方依赖）。"""

    def __init__(self, *, color: bool | None = None, quiet: bool = False):
        self.color = supports_color() if color is None else color
        self.quiet = quiet

    # ------------------------------------------------------------------ #
    def paint(self, text: str, *styles: str) -> str:
        if not self.color:
            return text
        prefix = "".join(_COLORS.get(style, "") for style in styles)
        return f"{prefix}{text}{_COLORS['reset']}" if prefix else text

    def write(self, text: str = "", *, end: str = "\n", force: bool = False) -> None:
        if self.quiet and not force:
            return
        try:
            print(text, end=end, flush=True)
        except UnicodeEncodeError:  # 极端环境下的兜底
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + end)
            sys.stdout.flush()

    # ------------------------------------------------------------------ #
    def title(self, text: str) -> None:
        self.write()
        self.write(self.paint(f"  {text}", "bold", "cyan"))
        self.write(self.paint("  " + "─" * max(10, len(text) * 2), "gray"))

    def info(self, text: str) -> None:
        self.write(f"  {text}")

    def dim(self, text: str) -> None:
        self.write(self.paint(f"  {text}", "gray"))

    def success(self, text: str) -> None:
        self.write(self.paint(f"  ✔ {text}", "green"))

    def warn(self, text: str) -> None:
        self.write(self.paint(f"  ⚠ {text}", "yellow"))

    def error(self, text: str) -> None:
        self.write(self.paint(f"  ✖ {text}", "red"))

    def step(self, current: int, total: int, text: str = "") -> None:
        badge = self.paint(f"[{current}/{total}]", "magenta", "bold")
        self.write(f"\n  {badge} {self.paint(text, 'bold')}" if text else f"\n  {badge}")

    def banner(self, subtitle: str = "") -> None:
        self.write()
        self.write(self.paint("  ╭──────────────────────────────────────────────╮", "cyan"))
        self.write(
            self.paint("  │", "cyan")
            + self.paint("   Code Review Agent  ·  代码审查智能体      ", "bold")
            + self.paint("│", "cyan")
        )
        self.write(self.paint("  ╰──────────────────────────────────────────────╯", "cyan"))
        if subtitle:
            self.dim(subtitle)
        self.write()
