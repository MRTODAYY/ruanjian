"""工具层的公共工具函数：路径安全、文件读写、语言识别。"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

# 常见文本文件扩展名（不做全量 MIME 探测，保持零依赖）
TEXT_SUFFIXES: Set[str] = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift",
    ".scala", ".sh", ".bash", ".ps1", ".sql", ".html", ".htm", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".md", ".rst",
    ".txt", ".env", ".gitignore", ".dockerfile", ".vue", ".lua", ".r", ".m",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".sql": "sql", ".html": "html", ".css": "css",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown",
}

DEFAULT_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".idea", ".vscode", "dist", "build", ".pytest_cache", ".mypy_cache", ".tox",
    "site-packages", ".next", "target", "reports",
}

MAX_FILE_BYTES = 400_000  # 单文件读取上限，防止误读大文件撑爆上下文


class PathSecurityError(ValueError):
    """路径越界或非法。"""


def resolve_path(raw_path: str, workspace: Path, *, must_exist: bool = True) -> Path:
    """把用户/模型给出的路径安全地解析到 ``workspace`` 之内。

    既防御 ``../`` 目录穿越，也统一处理 Windows 下的正反斜杠与绝对路径。
    """
    if raw_path is None:
        raise PathSecurityError("路径不能为空")
    candidate = Path(str(raw_path).strip().strip('"').strip("'"))
    if not str(candidate):
        candidate = Path(".")
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.expanduser().resolve()
    root = workspace.expanduser().resolve()

    if resolved != root and root not in resolved.parents:
        raise PathSecurityError(
            f"拒绝访问工作目录之外的路径：{raw_path}（允许范围：{root}）"
        )
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"路径不存在：{raw_path}")
    return resolved


def is_probably_text(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return True
    if suffix and suffix not in TEXT_SUFFIXES and len(suffix) > 1:
        # 未登记的扩展名：抽样探测是否含 NUL 字节
        try:
            chunk = path.read_bytes()[:2048]
        except OSError:
            return False
        return b"\x00" not in chunk
    return True


def read_text(path: Path, max_bytes: int = MAX_FILE_BYTES) -> Tuple[str, bool]:
    """读取文本文件，返回 ``(内容, 是否被截断)``。"""
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    return text, truncated


def language_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in LANGUAGE_BY_SUFFIX:
        return LANGUAGE_BY_SUFFIX[suffix]
    if path.name.lower() in {"dockerfile", "makefile"}:
        return path.name.lower()
    return "text"


def iter_files(
    root: Path,
    pattern: str = "*",
    *,
    ignore_dirs: Optional[Iterable[str]] = None,
    max_files: int = 400,
) -> List[Path]:
    """遍历目录，返回匹配 ``pattern`` 的文件（跳过忽略目录）。"""
    ignored = set(ignore_dirs or DEFAULT_IGNORED_DIRS)
    patterns = [p.strip() for p in str(pattern).replace(";", ",").split(",") if p.strip()] or ["*"]
    results: List[Path] = []

    if root.is_file():
        for pat in patterns:
            if fnmatch.fnmatch(root.name, pat) or pat == "*":
                return [root]
        return []

    for current_dir, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignored and not d.startswith(".git"))
        for filename in sorted(filenames):
            path = Path(current_dir) / filename
            if any(fnmatch.fnmatch(filename, pat) for pat in patterns):
                results.append(path)
                if len(results) >= max_files:
                    return results
    return results


def numbered(text: str, start: int = 1) -> str:
    """给代码加行号，便于 LLM 精确引用行号。"""
    return "\n".join(
        f"{index:>4} | {line}" for index, line in enumerate(text.splitlines(), start=start)
    )


def slice_lines(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    start = max(1, start)
    end = min(len(lines), end)
    return "\n".join(lines[start - 1 : end])
