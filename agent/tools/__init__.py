"""工具实现。

- ``files.py``：read_file / write_file / list_dir（受 workspace 约束）
- ``shell.py``：run_shell（默认禁用，命令白名单只接受 PATH 中的真实可执行文件）
- ``python_exec.py``：run_python_code（默认禁用 + 人工审批；任意代码执行，非 OS 级隔离）
- ``http.py``：http_get（默认禁用，域名白名单 + 重定向校验）
- ``search.py``：search_code（只读代码搜索，支持扩展名过滤）
- ``git_status.py``：git_status（只读 git status --short）
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
from .git_status import git_status  # noqa: E402
from .http import http_get  # noqa: E402
from .python_exec import run_python_code  # noqa: E402
from .search import search_code  # noqa: E402
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
    "git_status",
    "http_get",
    "list_dir",
    "read_file",
    "run_python_code",
    "run_shell",
    "search_code",
    "write_file",
]