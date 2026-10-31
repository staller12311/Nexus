"""run_shell（默认禁用）。

安全模型（README「设计决策」第 14 条）：
- 默认空白名单：``allowed_commands: []`` 时**任何命令都被拒绝**；
- 只接受**裸命令名**（不含路径分隔符），避免 ``C:\\evil\\git.exe`` 绕过白名单；
- 只接受 PATH 中真实存在的可执行文件；``dir`` / ``type`` / ``copy`` 这类 cmd 内建命令
  不经过 shell 解析，一律 ``FileNotFoundError``，不会被执行；
- 始终 ``shell=False``：``;`` ``|`` ``&&`` ``>`` 之类只是普通参数字符，不会被当作控制符。
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Sequence

from . import ToolError

DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 20_000
#: 即使出错也要保证 PATH 查找可用
_ENV_OVERRIDES = {"PYTHONIOENCODING": "utf-8"}
logger = logging.getLogger("agent.tools.shell")


def split_command(command: str) -> list[str]:
    """把命令行切成 argv。

    刻意不用 ``shlex``：``shlex.split(posix=True)`` 会把 ``C:\\tmp\\x.py`` 里的反斜杠
    当成转义符吃掉，``posix=False`` 又会把引号留在参数里，两者在 Windows 上都有坑。
    这里只处理双/单引号分组，其余字符（含反斜杠、``;``、``|``）原样保留。
    """
    text = (command or "").strip()
    if not text:
        return []

    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in "\"'":
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if quote is not None:
        raise ToolError(f"命令里的引号没有闭合：{command!r}")
    if current:
        tokens.append("".join(current))
    return tokens


def run_shell(
    cwd: Path,
    command: str,
    allowed_commands: Sequence[str] = (),
    timeout: float = DEFAULT_TIMEOUT,
    *,
    allow_unsafe_shell: bool = False,
) -> str:
    """在 ``cwd`` 下执行命令。

    默认 ``shell=False``，只执行白名单里的裸命令名；
    ``allow_unsafe_shell=True`` 时改用 ``shell=True``，**绕过白名单**，
    由 AgentLoop 强制审批。它不是 OS 隔离，审批只是人工确认。
    """
    if allow_unsafe_shell:
        return _run_unsafe_shell(cwd, command, timeout)
    argv = split_command(command)
    if not argv:
        raise ToolError("命令不能为空")

    program = argv[0]
    if program != Path(program).name or "/" in program or "\\" in program:
        raise ToolError(
            f"只允许裸命令名（不含路径），收到 {program!r}。"
            f"例如用 git，而不是 C:\\tools\\git.exe。"
        )

    allowed = [item.strip().lower() for item in allowed_commands if item and item.strip()]
    if not allowed:
        raise ToolError(
            "run_shell 白名单为空，已拒绝执行任何命令。"
            "如需启用，请在 config.yaml 的 tools.run_shell.allowed_commands 中"
            "填入 PATH 中真实存在的可执行文件名（如 git、python）。"
        )
    if program.lower() not in allowed:
        raise ToolError(
            f"命令 {program!r} 不在白名单中。白名单：{allowed}。"
            f"需要新增请修改 config.yaml 的 tools.run_shell.allowed_commands。"
        )

    env = {**os.environ, **_ENV_OVERRIDES}
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ToolError(
            f"找不到可执行文件 {program!r}：{exc}。"
            f"注意 cmd 内建命令（dir / type / copy / echo 等）不受支持，"
            f"白名单只能填 PATH 中真实存在的程序（用 python -c \"import shutil;print(shutil.which('git'))\" 可确认）。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"命令超时（{timeout:g}s）已被终止：{command}") from exc
    except OSError as exc:
        raise ToolError(f"命令执行失败：{exc}") from exc

    stdout = _clip(completed.stdout or "")
    stderr = _clip(completed.stderr or "")
    parts = [f"$ {command}", f"退出码：{completed.returncode}"]
    if stdout:
        parts.append("--- stdout ---\n" + stdout)
    if stderr:
        parts.append("--- stderr ---\n" + stderr)
    if not stdout and not stderr:
        parts.append("（没有输出）")
    return "\n".join(parts)


def _run_unsafe_shell(cwd: Path, command: str, timeout: float) -> str:
    """显式开启的高危模式：shell=True，完整命令交给系统 Shell。"""
    text = (command or "").strip()
    if not text:
        raise ToolError("命令不能为空")
    logger.warning("run_shell 使用 allow_unsafe_shell=True 执行命令：%s", text[:200])

    env = {**os.environ, **_ENV_OVERRIDES}
    try:
        completed = subprocess.run(
            text,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"命令超时（{timeout:g}s）已被终止：{text}") from exc
    except OSError as exc:
        raise ToolError(f"命令执行失败：{exc}") from exc

    stdout = _clip(completed.stdout or "")
    stderr = _clip(completed.stderr or "")
    parts = [
        f"$ {text}",
        "模式：shell=True（allow_unsafe_shell）",
        f"退出码：{completed.returncode}",
    ]
    if stdout:
        parts.append("--- stdout ---\n" + stdout)
    if stderr:
        parts.append("--- stderr ---\n" + stderr)
    if not stdout and not stderr:
        parts.append("（没有输出）")
    return "\n".join(parts)


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text.rstrip("\n")
    return text[:MAX_OUTPUT_CHARS].rstrip("\n") + f"\n...（输出过长，已截断到 {MAX_OUTPUT_CHARS} 字符）"