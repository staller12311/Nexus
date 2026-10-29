"""验证 delete_path / move_path + 清理旧目录 + 在 D:\\ProjectA 重做任务。"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, r"D:\MyAi\.venv\Lib\site-packages")
sys.path.insert(0, r"D:\MyAi")

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw.decode()) if raw else None


def tools_at(workspace: str):
    call("POST", "/api/config/workspace", {"path": workspace})
    from agent.config import load_config
    from backend.service.agent_service import AgentService

    return AgentService(load_config(r"D:\MyAi\config.yaml"))


print("=" * 78)
print("① 健康检查里的运行时诊断（解释 WinError 5 的关键信息）")
print("=" * 78)
health = call("GET", "/api/health")
print(f"  python     = {health['python']}")
print(f"  integrity  = {health['integrity']}")
print(f"  write_warning = {health['write_warning'] or '（无，说明进程有完整写权限）'}")
assert health["integrity"] in {"medium", "high", "system"}, "后端应该用未降权的 python"

print("\n② 工具列表里 CRUD 齐全")
print("=" * 78)
info = call("GET", "/api/tools")
enabled = [tool["name"] for tool in info["tools"] if tool.get("enabled")]
print(f"  enabled = {enabled}")
for name in ("read_file", "write_file", "create_dir", "delete_path", "move_path", "list_dir"):
    assert name in enabled, f"缺少 {name}"

print("\n③ delete_path / move_path 行为与安全边界")
print("=" * 78)
probe_root = Path(r"D:\MyAi\workspace\_crud_probe")
import shutil

shutil.rmtree(probe_root, ignore_errors=True)
probe_root.mkdir(parents=True)
(probe_root / "a.txt").write_text("a", encoding="utf-8")
(probe_root / "empty_dir").mkdir()
(probe_root / "full_dir").mkdir()
(probe_root / "full_dir" / "inner.txt").write_text("x", encoding="utf-8")

service = tools_at(str(probe_root))

print("  delete 文件:", service.registry.call("delete_path", {"path": "a.txt"}))
assert not (probe_root / "a.txt").exists()

print("  delete 空目录:", service.registry.call("delete_path", {"path": "empty_dir"}))
assert not (probe_root / "empty_dir").exists()

result = service.registry.call("delete_path", {"path": "full_dir"})
print("  delete 非空目录:", result[:80])
assert result.startswith("错误：") and "不是空目录" in result

result = service.registry.call("delete_path", {"path": "."})
print("  delete 根目录:", result)
assert result.startswith("错误：")

result = service.registry.call("delete_path", {"path": "../outside"})
print("  delete 越界:", result[:80])
assert result.startswith("错误：") and "越出 workspace" in result

print("\n  move 重命名:", service.registry.call("move_path", {"src": "full_dir/inner.txt", "dest": "moved.txt"}))
assert (probe_root / "moved.txt").read_text(encoding="utf-8") == "x"

print("  move 建父目录:", service.registry.call("move_path", {"src": "moved.txt", "dest": "sub/deep/m.txt"}))
assert (probe_root / "sub" / "deep" / "m.txt").exists()

print("  move 覆盖已有:", service.registry.call("move_path", {"src": "sub/deep/m.txt", "dest": "sub"}))
assert (probe_root / "sub" / "deep" / "m.txt").exists()  # 目标已存在 → 拒绝，源还在

(probe_root / "target.txt").write_text("t", encoding="utf-8")
result = service.registry.call("move_path", {"src": "sub/deep/m.txt", "dest": "target.txt"})
print("  move 目标已存在:", result[:70])
assert result.startswith("错误：") and "拒绝覆盖" in result

print("\n④ 清除旧目录 D:\\MyAi\\workspace\\ProjectA（用 delete_path，不用 shell）")
print("=" * 78)
legacy = Path(r"D:\MyAi\workspace\ProjectA")
if legacy.exists():
    svc = tools_at(str(Path(r"D:\MyAi\workspace")))
    print("  剩余内容:", svc.registry.call("list_dir", {"path": "ProjectA"}).replace("\n", " | ")[:110])
    for rel in (
        "ProjectA/main.py",
        "ProjectA/src/components/main.py",
        "ProjectA/src/components",
        "ProjectA/src",
        "ProjectA",
    ):
        print(f"  {rel}:", svc.registry.call("delete_path", {"path": rel}))
    assert not legacy.exists(), "旧目录没删干净"
    print("  ✅ D:\\MyAi\\workspace\\ProjectA 已删除")
else:
    print("  （不存在，无需清理）")

shutil.rmtree(probe_root, ignore_errors=True)

print("\n⑤ 在 D:\\ProjectA 重做任务：create_dir('src/components') + write_file")
print("=" * 78)
project = Path(r"D:\ProjectA")
svc = tools_at(str(project))
print("  当前工作区:", svc.workspace)
print("  create_dir:", svc.registry.call("create_dir", {"path": "src/components"}))
print("  write main.py:", svc.registry.call(
    "write_file",
    {
        "path": "main.py",
        "content": '"""ProjectA 入口。"""\n\nfrom src.components import greet\n\n\n'
        'if __name__ == "__main__":\n    print(greet("ProjectA"))\n',
    },
))
print("  write __init__.py:", svc.registry.call(
    "write_file",
    {
        "path": "src/components/__init__.py",
        "content": '"""组件包。"""\n\n\ndef greet(name: str) -> str:\n    return f"hello {name}"\n',
    },
))
# 清掉之前测试留下的 测试目录
if (project / "测试目录").exists():
    print("  清理测试目录/.gitkeep:", svc.registry.call("delete_path", {"path": "测试目录/.gitkeep"}))
    print("  清理测试目录:", svc.registry.call("delete_path", {"path": "测试目录"}))

print("\n  最终文件树:")
for item in sorted(project.rglob("*")):
    if item.name == "desktop.ini":
        continue
    print("   ", item.relative_to(project).as_posix() + ("/" if item.is_dir() else ""))

print("\n  实际运行 main.py:")
import subprocess  # noqa: E402

run = subprocess.run(
    [sys.executable, "main.py"], cwd=str(project), capture_output=True, text=True, timeout=30
)
print(f"    exit={run.returncode} stdout={run.stdout.strip()!r} stderr={run.stderr.strip()[:80]!r}")
assert run.returncode == 0 and "hello ProjectA" in run.stdout

print("\n全部通过 ✅ CRUD 工具就绪、旧目录已清、D:\\ProjectA 任务完成且可运行")