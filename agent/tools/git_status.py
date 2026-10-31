"""只读 Git 状态：``git_status``。

固定执行 ``git status --short``，不接受用户参数、不经过 shell；
用于让模型知道 workspace 里哪些文件已被修改。
"""

from __future__ import annotations

import subprocess

from . import ToolError
from .files import Workspace

GIT_TIMEOUT_SECONDS = 15.0


def git_status(workspace: Workspace) -> str:
    """返回 workspace 的 ``git status --short``；非 Git 仓库/缺 git 时给可读错误。"""
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(workspace.root),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ToolError(
            "找不到 git 可执行文件，请确认已安装 Git 并把 git 加入 PATH。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"git status 超时（{GIT_TIMEOUT_SECONDS:g}s）") from exc
    except OSError as exc:
        raise ToolError(f"执行 git status 失败：{exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ToolError(
            "git status 执行失败（这可能不是 Git 仓库）。"
            + (f" Git 输出：{detail[:500]}" if detail else "")
        )

    output = (completed.stdout or "").strip()
    if not output:
        return "工作区干净：git status --short 没有输出。"
    return "$ git status --short\n" + output