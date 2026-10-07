# -*- coding: utf-8 -*-
"""提交前安全检查：确保仓库里没有混进 API Key。

用法::

    python 检查密钥.py

退出码 0 表示安全，1 表示发现了疑似密钥（不要提交！）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 只扫描这些类型的文件（跳过二进制、缓存、报告）
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "reports", ".idea", ".vscode"}
SCAN_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini",
                 ".cfg", ".bat", ".sh", ".example", ".env", ""}

# 形如 sk-xxxxx 的密钥；同时覆盖常见前缀
KEY_PATTERN = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{16,}"
    r"|sk-ant-[A-Za-z0-9_\-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16})\b"
)

# 允许出现的占位符
PLACEHOLDERS = {
    "sk-your-key-here", "sk-你的密钥", "sk-xxxx", "sk-test", "sk-test-key",
    "sk-real-key", "sk-abc", "sk-supersecretvalue123",
}

# 明显的「假密钥」片段（单元测试里的固件）
FAKE_FRAGMENTS = (
    "abcdefghij", "abcdefghijk", "1234567890", "0123456789",
    "0000000000", "aaaaaaaaaa", "xxxxxxxxxx",
)

# 这些文件本身是「示例/测试」，出现假密钥是正常的
ALLOWED_FILES = {".env.example"}


def looks_like_placeholder(token: str) -> bool:
    """判断一个 sk-xxx 串是不是「假的」——用于避免对测试固件误报。"""
    if token in PLACEHOLDERS:
        return True
    lowered = token.lower()
    body = lowered[3:] if lowered.startswith("sk-") else lowered

    if any(word in lowered for word in
           ("your", "xxx", "test", "example", "demo", "placeholder", "fake",
            "dummy", "sample", "fixture")):
        return True
    if any(fragment in body for fragment in FAKE_FRAGMENTS):
        return True
    # 真实密钥有较高随机性：如果去掉前缀后种类太少，几乎肯定是假的
    if len(set(body)) <= 8:
        return True
    return False


def main() -> int:
    root = Path(__file__).resolve().parent
    problems: list[tuple[Path, int, str]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES and path.name not in {".env", ".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in KEY_PATTERN.finditer(line):
                token = match.group(1)
                if path.name in ALLOWED_FILES or looks_like_placeholder(token):
                    continue
                problems.append((path.relative_to(root), lineno, token[:12] + "***"))

    print()
    if not problems:
        print("  [OK] 安全检查通过：没有发现真实的 API Key，可以放心提交。")
        print()
        return 0

    print("  [危险] 发现疑似真实密钥，请不要提交！")
    print()
    for path, lineno, masked in problems:
        print(f"    - {path}:{lineno}   {masked}")
    print()
    print("  处理办法：")
    print("    1. 把密钥从这些文件里删掉")
    print("    2. 密钥统一放到 .env.local（该文件已被 .gitignore 忽略）")
    print("    3. 重新运行本脚本确认通过")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
