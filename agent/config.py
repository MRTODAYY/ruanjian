"""Code Review Agent —— 配置加载模块。

支持三级优先级（后者覆盖前者）：

1. 代码内置默认值
2. 项目根目录 / 上级目录的 ``.env`` 文件
3. 真实环境变量（``os.environ``）

之所以自己实现 ``.env`` 解析而不依赖 ``python-dotenv``，是为了让本项目
**零第三方依赖**，克隆下来即可运行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, str] = {
    "CODEAGENT_BASE_URL": "https://api.deepseek.com",
    "CODEAGENT_API_KEY": "",
    "CODEAGENT_MODEL": "deepseek-chat",
    "CODEAGENT_MAX_STEPS": "14",
    "CODEAGENT_MAX_RETRIES": "4",
    "CODEAGENT_TIMEOUT": "180",
    "CODEAGENT_TEMPERATURE": "0.2",
    "CODEAGENT_ENABLE_EXEC": "true",
    "CODEAGENT_EXEC_TIMEOUT": "20",
    "CODEAGENT_WORKSPACE": "",
}


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_dotenv(path: Path) -> Dict[str, str]:
    """解析 ``.env`` 文件，返回键值对（忽略注释与空行）。"""
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # 允许 `export KEY=value` 写法
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        # 去掉行尾注释（仅当注释前有空白时，避免误伤 URL 中的 #）
        value = _strip_quotes(value)
        result[key] = value
    return result


def load_env(start: Optional[Path] = None) -> Dict[str, str]:
    """按优先级合并默认值、``.env`` 与环境变量。

    文件查找顺序（后者覆盖前者）：

    1. ``.env``        —— 通用配置，**可以提交到仓库**（只放非敏感项）
    2. ``.env.local``  —— 本机私密配置（放 API Key），**必须被 .gitignore 忽略**
    3. 真实环境变量    —— 优先级最高，适合 CI / 服务器

    之所以分开两个文件，是为了让「仓库里能有一份开箱即用的配置」和
    「密钥不泄露」这两件事同时成立：提交 ``.env`` 与 ``.env.example``，
    把密钥留在永远不会被提交的 ``.env.local`` 里。
    """
    merged = dict(DEFAULTS)
    search_dirs = []
    base = start or PROJECT_ROOT
    search_dirs.append(base)
    search_dirs.append(base.parent)  # 允许把配置放在仓库上一层

    seen = set()
    for directory in search_dirs:
        directory = Path(directory).resolve()
        if directory in seen:
            continue
        seen.add(directory)
        # 先读可提交的 .env，再用本机私密的 .env.local 覆盖
        merged.update(parse_dotenv(directory / ".env"))
        merged.update(parse_dotenv(directory / ".env.local"))

    for key in list(DEFAULTS):
        if key in os.environ and os.environ[key] != "":
            merged[key] = os.environ[key]
    return merged


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: str, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Agent 运行配置。"""

    base_url: str = DEFAULTS["CODEAGENT_BASE_URL"]
    api_key: str = ""
    model: str = DEFAULTS["CODEAGENT_MODEL"]
    max_steps: int = 14
    max_retries: int = 4
    timeout: int = 180
    temperature: float = 0.2
    enable_exec: bool = True
    exec_timeout: int = 20
    workspace: Path = field(default_factory=lambda: Path.cwd())

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, env_file_dir: Optional[Path] = None) -> "Config":
        raw = load_env(env_file_dir)
        workspace = raw.get("CODEAGENT_WORKSPACE", "").strip()
        return cls(
            base_url=raw["CODEAGENT_BASE_URL"].rstrip("/"),
            api_key=raw["CODEAGENT_API_KEY"].strip(),
            model=raw["CODEAGENT_MODEL"].strip(),
            max_steps=max(1, _as_int(raw["CODEAGENT_MAX_STEPS"], 14)),
            max_retries=max(0, _as_int(raw["CODEAGENT_MAX_RETRIES"], 4)),
            timeout=max(5, _as_int(raw["CODEAGENT_TIMEOUT"], 180)),
            temperature=_as_float(raw["CODEAGENT_TEMPERATURE"], 0.2),
            enable_exec=_as_bool(raw["CODEAGENT_ENABLE_EXEC"], True),
            exec_timeout=max(1, _as_int(raw["CODEAGENT_EXEC_TIMEOUT"], 20)),
            workspace=Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve(),
        )

    # ------------------------------------------------------------------ #
    @property
    def has_api_key(self) -> bool:
        key = self.api_key.strip()
        return bool(key) and not key.startswith("sk-your-key")

    @property
    def chat_endpoint(self) -> str:
        """OpenAI 兼容的 chat/completions 端点。

        自动适配不同服务商的路径习惯：
        - ``https://api.deepseek.com``          -> ``.../v1/chat/completions``
        - ``https://api.openai.com/v1``         -> ``.../v1/chat/completions``
        - ``https://api.openai.com/v1/``        -> 去掉多余的尾部斜杠
        - ``.../v1/chat/completions``（已是完整端点）-> 原样返回
        """
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def mask_key(self) -> str:
        """日志脱敏，只保留首尾少量字符。"""
        key = self.api_key.strip()
        if not key:
            return "(未配置)"
        if len(key) <= 10:
            return key[:2] + "*" * (len(key) - 2)
        return f"{key[:6]}{'*' * 6}{key[-4:]}"

    def describe(self) -> str:
        return (
            f"模型={self.model}  接口={self.base_url}  Key={self.mask_key()}  "
            f"最大步数={self.max_steps}  重试={self.max_retries}  "
            f"允许执行代码={self.enable_exec}  工作目录={self.workspace}"
        )
