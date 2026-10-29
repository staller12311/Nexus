"""验证「安全沙箱」不是空话：workspace 越界访问必须被拦截。

覆盖的越界手法：
1. 绝对路径直指系统文件（C:\\Windows\\System32\\drivers\\etc\\hosts）
2. ../ 相对路径穿越
3. 正斜杠 / 反斜杠混用
4. Windows 扩展长度路径前缀 \\?\
5. UNC 路径 \\\\localhost\\C$\\...
6. 环境变量式写法（%WINDIR%）
7. workspace 内建一个 junction（目录联接）指向 C:\\Windows —— 最容易被忽略的绕过方式
   代码里先 resolve() 再 is_relative_to()，正是为了防这个
8. write_file / list_dir 的同一套校验

对照组：workspace 内的正常文件必须**能**读到（证明不是「一律报错」）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.registry import build_registry  # noqa: E402

config = load_config(ROOT / "config.yaml")
workspace = config.workspace_path
print("=" * 78)
print(f"workspace = {workspace}")
print(f"系统文件    = C:\\Windows\\System32\\drivers\\etc\\hosts")
print("=" * 78)

registry = build_registry(config)
print(f"已启用工具: {registry.names()}\n")

HOSTS = r"C:\Windows\System32\drivers\etc\hosts"
WIN_INI = r"C:\Windows\win.ini"


def attempt(tool: str, args: dict) -> str:
    """走 registry.call：与模型真实调用路径完全一致（含参数校验与错误包装）。"""
    return registry.call(tool, args)


failures: list[str] = []


def assert_blocked(label: str, tool: str, args: dict, must_not_contain: tuple[str, ...]) -> None:
    result = attempt(tool, args)
    blocked = result.startswith("错误：")
    leaked = [needle for needle in must_not_contain if needle and needle in result]
    status = "✅ 拦截" if blocked and not leaked else "❌ 未拦截"
    print(f"  {status}  {label}")
    print(f"        返回: {result[:150]}")
    if not blocked:
        failures.append(f"{label}：没有被拦截 -> {result[:100]}")
    if leaked:
        failures.append(f"{label}：泄漏了文件内容 {leaked}")


print("① 绝对路径直接读系统文件")
assert_blocked("read_file(hosts 绝对路径)", "read_file", {"path": HOSTS}, ("localhost", "127.0.0.1"))

print("\n② ../ 相对路径穿越")
assert_blocked(
    "read_file(../../../Windows/win.ini)",
    "read_file",
    {"path": "../../../../Windows/win.ini"},
    ("[fonts]", "[mci extensions]"),
)

print("\n③ 正斜杠混写")
assert_blocked(
    "read_file(C:/Windows/win.ini)",
    "read_file",
    {"path": "C:/Windows/win.ini"},
    ("[fonts]",),
)

print("\n④ Windows 扩展长度路径前缀")
assert_blocked(
    r"read_file(\\?\C:\Windows\win.ini)",
    "read_file",
    {"path": r"\\?\C:\Windows\win.ini"},
    ("[fonts]",),
)

print("\n⑤ UNC 路径")
assert_blocked(
    r"read_file(\\localhost\C$\Windows\win.ini)",
    "read_file",
    {"path": r"\\localhost\C$\Windows\win.ini"},
    ("[fonts]",),
)

print("\n⑥ 环境变量式写法（不展开，按相对路径处理）")
assert_blocked(
    "read_file(%WINDIR%/win.ini)",
    "read_file",
    {"path": "%WINDIR%/win.ini"},
    ("[fonts]",),
)

print("\n⑦ junction（目录联接）指到 C:\\Windows —— 最容易绕过的姿势")
junction = workspace / "_sandbox_probe_link"
created = False
try:
    if junction.exists():
        junction.unlink()
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), r"C:\Windows"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    created = completed.returncode == 0 and junction.exists()
    print(f"  （创建 junction: {'成功' if created else '失败（可能是权限限制）'}）")
    if created:
        assert_blocked(
            "read_file(_sandbox_probe_link/win.ini)",
            "read_file",
            {"path": "_sandbox_probe_link/win.ini"},
            ("[fonts]",),
        )
        try:
            os.rmdir(junction)  # junction 用 rmdir 删除，不会动目标目录
            print("  （junction 已清理）")
        except OSError as exc:
            print(f"  （junction 清理失败：{exc}）")
    else:
        print(f"  跳过：{completed.stderr.strip()[:120]}（本环境禁止创建目录联接，非代码问题）")
        print("  替代验证：路径归一化后再判边界（.. 绕出去又绕回来应被允许）")
        inside = attempt("read_file", {"path": "subdir/../readme.txt"})
        print(f"    read_file(subdir/../readme.txt) -> {inside[:80]!r}")
        if "越出 workspace" in inside:
            failures.append(".. 绕回被误判为越界（说明判边界前没做归一化）")
finally:
    if created and junction.exists():
        try:
            os.rmdir(junction)
        except OSError:
            pass

print("\n⑧ write_file 越界写系统目录")
assert_blocked(
    "write_file(C:\\Windows\\evil.txt)",
    "write_file",
    {"path": r"C:\Windows\evil_probe_not_written.txt", "content": "x"},
    (),
)

print("\n⑨ list_dir 列系统目录")
assert_blocked(
    "list_dir(C:\\Windows\\System32\\drivers\\etc)",
    "list_dir",
    {"path": r"C:\Windows\System32\drivers\etc"},
    ("hosts", "networks", "protocol"),
)

print("\n" + "=" * 78)
print("对照组：workspace 内必须正常工作（证明不是无脑拒绝）")
print("=" * 78)
probe = workspace / "sandbox_probe_ok.txt"
workspace.mkdir(parents=True, exist_ok=True)
probe.write_text("hello sandbox", encoding="utf-8")
try:
    read_result = attempt("read_file", {"path": "sandbox_probe_ok.txt"})
    print(f"  read_file(sandbox_probe_ok.txt) -> {read_result!r}")
    assert "hello sandbox" in read_result, "❌ workspace 内读取失败"
    print("  （返回带文件头与字节数，属正常格式）")

    write_result = attempt("write_file", {"path": "sandbox_probe_write.txt", "content": "written"})
    print(f"  write_file(sandbox_probe_write.txt) -> {write_result!r}")
    assert (workspace / "sandbox_probe_write.txt").read_text(encoding="utf-8") == "written"

    list_result = attempt("list_dir", {"path": "."})
    print(f"  list_dir('.') -> {list_result[:120]!r}")
    assert "sandbox_probe_ok.txt" in list_result
finally:
    for name in ("sandbox_probe_ok.txt", "sandbox_probe_write.txt"):
        target = workspace / name
        if target.exists():
            target.unlink()

print("\n" + "=" * 78)
if failures:
    print("❌ 以下越界尝试没有被正确拦截：")
    for item in failures:
        print("   -", item)
    raise SystemExit(1)
print("✅ 沙箱保卫生效：所有越界读/写/列目录都被拒绝，workspace 内一切正常")
print("   （且拦截信息里没有泄漏任何系统文件内容）")