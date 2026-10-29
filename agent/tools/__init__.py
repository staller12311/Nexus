"""工具实现。

- ``files.py``：read_file / write_file / list_dir（受 workspace 约束）
- ``shell.py``：run_shell（默认禁用，命令白名单只接受 PATH 中的真实可执行文件）
- ``http.py``：http_get（默认禁用，域名白名单 + 重定向校验）
- ``time.py``：get_time（示例工具，演示「新增一个工具」的完整步骤）

共享的 ``ToolError`` 定义在这里：工具抛出它表示「可预期的失败」，
registry 会把它转成一段模型能看懂的文本继续对话，而不是让主循环崩掉。
"""

from __future__ import annotations


class ToolError(Exception):
    """工具的可预期失败（路径越界、白名单拒绝、文件不存在等）。"""


from .files import (  # noqa: E402
    Mount,
    Workspace,
    WorkspaceViolation,
    create_dir,
    delete_path,
    list_dir,
    move_path,
    read_file,
    write_file,
)
from .http import http_get  # noqa: E402
from .shell import run_shell  # noqa: E402
from .time import get_time  # noqa: E402

__all__ = [
    "ToolError",
    "Mount",
    "create_dir",
    "delete_path",
    "move_path",
    "Workspace",
    "WorkspaceViolation",
    "get_time",
    "http_get",
    "list_dir",
    "read_file",
    "run_shell",
    "write_file",
]