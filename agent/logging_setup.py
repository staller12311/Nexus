"""日志初始化与脱敏。

要点（见 README「设计决策」）：
- 日志文件用 UTF-8 + 轮转，父目录自动创建；
- 用自定义 Formatter 对**格式化后的整行**脱敏，异常堆栈里的密钥同样被覆盖；
- 已注册的密钥值来自环境变量（只取名字像密钥的那些），也可运行时 ``register_secret()``；
- ``force_utf8_stdio()`` 解决 Windows 控制台非 UTF-8 代码页下中文乱码。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

REDACTED = "***"

#: 环境变量名命中即视为敏感值
SENSITIVE_NAME_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|secret|token|password|passwd|credential|authorization)",
    re.IGNORECASE,
)

#: 文本中形如密钥的字面量（兜底，防止密钥出现在非环境变量的位置）
SECRET_PATTERN_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}"          # OpenAI 风格
    r"|sk-or-v1-[A-Za-z0-9_\-]{6,}"    # OpenRouter
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9_\-]{20,}"
    r"|(?i:Bearer)\s+[A-Za-z0-9._\-]{8,})"
)

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

#: 运行时注册的敏感字面量（例如 provider 从环境变量取到的密钥）
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """登记一个需要在日志中屏蔽的字面量（长度过短的不登记，避免误伤正常文本）。"""
    if value and len(value.strip()) >= 6:
        _registered_secrets.add(value.strip())


def clear_registered_secrets() -> None:
    _registered_secrets.clear()


def _collect_env_secrets(environ: dict[str, str] | None = None) -> set[str]:
    env = os.environ if environ is None else environ
    secrets = set()
    for name, value in env.items():
        if value and len(value) >= 6 and SENSITIVE_NAME_RE.search(name):
            secrets.add(value)
    return secrets


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """把文本中的已知密钥与常见密钥形态替换为 ``***``。"""
    result = text
    for secret in sorted(set(extra_secrets) | _registered_secrets, key=len, reverse=True):
        if secret and secret in result:
            result = result.replace(secret, REDACTED)
    return SECRET_PATTERN_RE.sub(REDACTED, result)


class RedactingFormatter(logging.Formatter):
    """先按常规格式化（含异常堆栈），再整体脱敏。"""

    def __init__(self, fmt: str = _DEFAULT_FORMAT, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - logging API
        return redact(super().format(record), self._secrets)


def force_utf8_stdio() -> None:
    """把 stdin/stdout/stderr 都切到 UTF-8。

    Windows 默认代码页（936）下：
    - stdout/stderr 不设，中文日志与中文报错会变乱码；
    - **stdin 也要设**：管道/重定向输入时 Python 用本地代码页解码，
      ``echo 你好 | python -m agent chat`` 会读成乱码（真实终端走 PEP 528 本来就是 UTF-8，
      但管道不是）。这里是进程内解决，用户不必手动设 PYTHONIOENCODING。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # 已被重定向/替换等场景，忽略
                pass


def setup_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    console: bool = False,
    logger_name: str = "agent",
    environ: dict[str, str] | None = None,
) -> logging.Logger:
    """配置 ``agent`` 日志器；可重复调用（不会重复叠加 handler）。"""
    level_value = getattr(logging, str(level).upper(), None)
    if not isinstance(level_value, int):
        raise ValueError(f"非法日志级别：{level!r}")

    logger = logging.getLogger(logger_name)
    logger.setLevel(level_value)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(secrets=_collect_env_secrets(environ))

    if log_file is not None:
        path = Path(log_file)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level_value)
        logger.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(level_value)
        logger.addHandler(stream_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


def get_logger(name: str = "agent") -> logging.Logger:
    return logging.getLogger(name)