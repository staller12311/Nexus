"""验证工作区切换：合法路径生效、非法/危险路径被拒、持久化与恢复默认。"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from backend.service.agent_service import AgentService  # noqa: E402
from backend.service.workspace import WorkspaceError, validate_workspace  # noqa: E402

workdir = ROOT / "workspace" / "_ws_probe"
shutil.rmtree(workdir, ignore_errors=True)
workdir.mkdir(parents=True, exist_ok=True)
project = workdir / "MyProject"
project.mkdir()
(project / "app.py").write_text("print('hi from project')\n", encoding="utf-8")
(project / "sub").mkdir()
(project / "sub" / "util.ts").write_text("export const x = 1\n", encoding="utf-8")

service = AgentService(load_config(ROOT / "config.yaml"))
default_ws = service.default_workspace
print("=" * 78)
print(f"默认工作区: {default_ws}")
print("=" * 78)


def attempt(label: str, path: str) -> None:
    try:
        resolved = validate_workspace(path)
        print(f"  ❌ 未拒绝 {label}: {path} -> {resolved}")
        raise SystemExit(1)
    except WorkspaceError as exc:
        print(f"  ✅ 拒绝 {label}")
        print(f"        {exc}")


print("① 危险 / 不合法路径必须被拒")
attempt("C:\\Windows", r"C:\Windows")
attempt("C:\\Windows\\System32", r"C:\Windows\System32")
attempt("C:\\Windows\\SysWOW64", r"C:\Windows\SysWOW64")
attempt("C:\\Program Files", r"C:\Program Files")
attempt("C:\\ProgramData", r"C:\ProgramData")
attempt("盘符根目录", "D:\\")
attempt("用户主目录（整体）", str(Path.home()))
attempt("AppData（凭据/应用数据）", str(Path.home() / "AppData"))
attempt(".ssh（密钥目录）", str(Path.home() / ".ssh"))
attempt("相对路径", "workspace")
attempt("不存在的目录", str(workdir / "nope"))
attempt("指向文件而不是目录", str(project / "app.py"))
attempt("UNC 网络路径", r"\\localhost\C$\Windows")
attempt("带 .. 的穿越写法", rf"C:\Windows\System32\..\..\Windows")

print("\n② 合法路径：切换后文件工具立刻能在新目录工作")
resolved = service.set_workspace(str(project))
print(f"  切换成功 -> {resolved}")
assert resolved == project.resolve()

read_result = service.registry.call("read_file", {"path": "app.py"})
print(f"  read_file(app.py) -> {read_result!r}")
assert "hi from project" in read_result

list_result = service.registry.call("list_dir", {"path": "."})
print(f"  list_dir('.') -> {list_result[:90]!r}")
assert "app.py" in list_result and "sub" in list_result

write_result = service.registry.call("write_file", {"path": "new.txt", "content": "written"})
print(f"  write_file(new.txt) -> {write_result!r}")
assert (project / "new.txt").read_text(encoding="utf-8") == "written"

print("\n③ 越界仍然被拦（新工作区之外）")
escape = service.registry.call("read_file", {"path": r"C:\Windows\System32\drivers\etc\hosts"})
print(f"  read_file(C:\\Windows\\...\\hosts) -> {escape[:110]}")
assert escape.startswith("错误：") and "越出 workspace" in escape

print("\n④ 切换是持久的：新实例（模拟重启）仍读回该工作区")
reloaded = AgentService(load_config(ROOT / "config.yaml"))
print(f"  重启后工作区: {reloaded.workspace}")
assert reloaded.workspace == project.resolve()
assert "hi from project" in reloaded.registry.call("read_file", {"path": "app.py"})

print("\n⑤ 恢复默认")
restored = reloaded.reset_workspace()
print(f"  恢复后: {restored}（默认: {default_ws}）")
assert restored == default_ws
assert reloaded.workspace == default_ws
third = AgentService(load_config(ROOT / "config.yaml"))
assert third.workspace == default_ws, "恢复默认后重启又变回去了"
print(f"  重新实例化确认: {third.workspace}")

print("\n⑥ 持久化过的路径失效时（目录被删）自动回退默认")
service.set_workspace(str(project))
shutil.rmtree(project)
fourth = AgentService(load_config(ROOT / "config.yaml"))
print(f"  目录已删除后重启: {fourth.workspace}")
assert fourth.workspace == default_ws
fourth.reset_workspace()

shutil.rmtree(workdir, ignore_errors=True)
print("\n全部通过 ✅ 工作区切换可用、危险路径被拒、持久化与回退正确")