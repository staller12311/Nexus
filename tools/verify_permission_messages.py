"""验证权限错误提示（用 mock 精确制造 PermissionError）与 create_dir 行为。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"D:\MyAi")

from agent.tools import ToolError  # noqa: E402
from agent.tools.files import Mount, Workspace, create_dir, list_dir, read_file, write_file  # noqa: E402


def call_tool(fn, workspace, *args):
    """模拟 registry.call 的行为：异常转成「错误：...」字符串。"""
    try:
        return fn(workspace, *args)
    except ToolError as exc:
        return f"错误：{exc}"

base = Path(r"D:\MyAi\workspace\_perm_probe")
shutil.rmtree(base, ignore_errors=True)
base.mkdir(parents=True)
(base / "a.txt").write_text("hi", encoding="utf-8")
ws = Workspace(root=base)

print("=" * 74)
print("① 写文件遇到 PermissionError -> 可操作提示")
print("=" * 74)
with patch.object(Path, "write_bytes", side_effect=PermissionError(13, "拒绝访问")):
    result = call_tool(write_file, ws, "x.txt", "y")
print(" ", result)
assert "当前目录无写入权限" in result and "只读" in result and "create_dir" in result

print("\n② 建目录遇到 PermissionError -> 可操作提示")
print("=" * 74)
with patch.object(Path, "mkdir", side_effect=PermissionError(13, "拒绝访问")):
    result = call_tool(create_dir, ws, "newdir")
print(" ", result)
assert "当前目录无写入权限" in result and "只读" in result

print("\n③ 读文件 / 列目录遇到 PermissionError -> 可操作提示")
print("=" * 74)
with patch.object(Path, "read_bytes", side_effect=PermissionError(13, "拒绝访问")):
    result = call_tool(read_file, ws, "a.txt")
print(" ", result)
assert "没有权限读取" in result

with patch.object(Path, "iterdir", side_effect=PermissionError(13, "拒绝访问")):
    result = call_tool(list_dir, ws, ".")
print(" ", result)
assert "没有权限列出目录" in result

print("\n④ create_dir 正常路径：多级创建 / 幂等 / 撞到文件报错")
print("=" * 74)
print("  多级:", call_tool(create_dir, ws, "src/components/ui"))
assert (base / "src" / "components" / "ui").is_dir()
print("  幂等:", call_tool(create_dir, ws, "src/components/ui"))
assert "已存在" in call_tool(create_dir, ws, "src/components/ui")
print("  撞文件:", call_tool(create_dir, ws, "a.txt"))
assert call_tool(create_dir, ws, "a.txt").startswith("错误：")

print("\n⑤ create_dir 越界 / 只读挂载")
print("=" * 74)
outside = call_tool(create_dir, ws, "../outside")
print("  越界:", outside[:100])
assert outside.startswith("错误：") and "越出 workspace" in outside

libs = Path(r"D:\MyAi\workspace\_perm_libs")
libs.mkdir(exist_ok=True)
ws2 = Workspace(root=base, extras=(Mount(name="libs", path=libs),))
result = call_tool(create_dir, ws2, "@libs/x")
print("  只读挂载:", result)
assert result.startswith("错误：") and "只读" in result

shutil.rmtree(base, ignore_errors=True)
shutil.rmtree(libs, ignore_errors=True)
print("\n全部通过 ✅ 权限错误提示可操作，create_dir 行为正确")