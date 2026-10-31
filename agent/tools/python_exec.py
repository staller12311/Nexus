"""run_python_code：在 workspace 内执行一段任意 Python 代码（默认禁用 + 人工审批）。

安全与设计边界（与 run_shell 同一套纪律）：
- **不是 OS 级隔离**：代码以当前后端/CLI 进程的权限运行，能读写该权限触达的文件
  （因此只在用户明确要求时启用，并配合 ``require_approval`` 人工审批）；
- 代码落在**主工作区**的 ``.agent_tmp/`` 下（``run_<uuid6>.py``），路径先过
  ``Workspace.resolve_with_mount()`` 的越界校验；只读挂载不能作为临时文件位置；
- 用 ``sys.executable -I -B -X utf8`` 执行：``-I`` 隔离模式（忽略环境变量与用户 site），
  ``-B`` 不写 .pyc，``-X utf8`` 强制子进程 stdio 用 UTF-8（``-I`` 会忽略
  ``PYTHONIOENCODING``，不加这个在 GBK 机器上中文输出会乱码）；
  子进程环境收窄到 SYSTEMROOT/PATH/TEMP/TMP；
- 单次执行有超时上限（默认 10s），stdout/stderr 各自截断到 ``max_output_chars``；
- 无论成功、非零退出、超时还是异常，临时文件都会被删除，``.agent_tmp/`` 为空时也一并删除。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import ToolError
from .files import Workspace

#: 单段代码上限（字节）；超过直接拒绝，避免把巨大脚本写进工作区
MAX_CODE_BYTES = 20 * 1024
#: 临时文件目录名（相对主工作区）
TEMP_DIR_NAME = ".agent_tmp"
#: 子进程允许看到的环境变量（其余一律不传，避免密钥/代理设置被代码读到）
_ENV_KEYS = ("SYSTEMROOT", "PATH", "TEMP", "TMP")


def run_python_code(
    workspace: Workspace,
    code: str,
    *,
    timeout: float,
    max_output_chars: int,
) -> str:
    """执行一段 Python 代码并返回「退出码 / 用时 / stdout / stderr」。

    可预期失败（代码为空/过长、超时、临时文件写不进去）抛 ``ToolError``；
    代码本身非零退出不算工具失败，退出码会如实返回给模型。
    """
    if not isinstance(code, str):
        raise ToolError("code 必须是字符串")
    if not code.strip():
        raise ToolError("code 不能为空")
    encoded = code.encode("utf-8")
    if len(encoded) > MAX_CODE_BYTES:
        raise ToolError(
            f"代码过长：上限 {MAX_CODE_BYTES // 1024}KB，当前 {len(encoded)} 字节。"
            "请拆成多次调用，或把逻辑写进 workspace 里的文件再读取。"
        )
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ToolError("timeout 必须是大于 0 的秒数")
    try:
        limit = max(1, int(max_output_chars))
    except (TypeError, ValueError):
        raise ToolError("max_output_chars 必须是正整数") from None

    temp_file, temp_dir = _write_temp_script(workspace, encoded)

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [_python_executable(), "-I", "-B", "-X", "utf8", str(temp_file)],
            cwd=str(workspace.root),
            env=_minimal_env(),
            timeout=timeout,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"执行超时（{timeout:g}s），已终止") from exc
    except OSError as exc:
        raise ToolError(f"Python 进程启动失败：{exc}") from exc
    finally:
        _cleanup(temp_file, temp_dir)
    elapsed = time.perf_counter() - started

    stdout = _clip(completed.stdout or "", limit)
    stderr = _clip(completed.stderr or "", limit)

    parts = [
        f"退出码：{completed.returncode}",
        f"用时：{elapsed:.2f}s",
        "--- stdout ---",
        stdout if stdout else "（无输出）",
    ]
    if stderr:
        parts.append("--- stderr ---\n" + stderr)
    return "\n".join(parts)


# ---------------------------------------------------------------- 临时文件
def _write_temp_script(workspace: Workspace, encoded: bytes) -> tuple[Path, Path]:
    """在主工作区 ``.agent_tmp/`` 下写临时脚本，返回 ``(脚本路径, 目录)``。"""
    temp_dir, mount = workspace.resolve_with_mount(TEMP_DIR_NAME)
    if mount is not None:
        kind = "只读挂载" if not mount.writable else "附加挂载"
        raise ToolError(
            f"@{mount.name} 是{kind}，run_python_code 的临时文件只写在主工作区的 "
            f"{TEMP_DIR_NAME}/ 下，已拒绝。"
        )

    temp_file = temp_dir / f"run_{uuid.uuid4().hex[:6]}.py"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file.write_bytes(encoded)
    except PermissionError as exc:
        _cleanup(temp_file, temp_dir)
        raise ToolError(
            f"没有权限在主工作区创建临时脚本 {TEMP_DIR_NAME}/{temp_file.name}"
            f"（{exc.strerror or exc}）。请检查工作区是否只读，或换一个有写权限的工作区。"
        ) from exc
    except OSError as exc:
        _cleanup(temp_file, temp_dir)
        raise ToolError(f"写入临时脚本失败：{exc}") from exc
    return temp_file, temp_dir


def _cleanup(temp_file: Path, temp_dir: Path) -> None:
    """删脚本；目录为空时也删掉（非空说明工作区里还有别的东西，别动）。"""
    try:
        temp_file.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 删除失败不影响本次结果
        pass
    try:
        temp_dir.rmdir()
    except OSError:  # pragma: no cover - 目录非空/已删除
        pass


def _python_executable() -> str:
    """打包版使用随安装包附带的 embeddable Python；开发/CLI 仍用当前解释器。"""
    configured = os.environ.get("NEXUS_PYTHON_EXECUTABLE", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return sys.executable


def _minimal_env() -> dict[str, str]:
    """只给子进程绝对必要的环境变量。"""
    env = {key: os.environ[key] for key in _ENV_KEYS if os.environ.get(key)}
    fallback = tempfile.gettempdir()
    env.setdefault("TEMP", fallback)
    env.setdefault("TMP", fallback)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _clip(text: str, limit: int) -> str:
    """按 ``limit`` 截断，并注明省略了多少字符。"""
    cleaned = text.rstrip("\n")
    if len(cleaned) <= limit:
        return cleaned
    omitted = len(cleaned) - limit
    return (
        cleaned[:limit].rstrip("\n")
        + f"\n...（输出过长，已截断到 {limit} 字符，省略 {omitted} 字符）"
    )