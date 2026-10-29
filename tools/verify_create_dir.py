"""验证：create_dir 工具 + D:\\ProjectA 真实可写 + 权限错误提示。"""
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


print("=" * 76)
print("① 工具列表里出现 create_dir")
print("=" * 76)
info = call("GET", "/api/tools")
enabled = [tool["name"] for tool in info["tools"] if tool.get("enabled")]
print(f"  enabled = {enabled}")
assert "create_dir" in enabled
print(f"  create_dir 描述: {[t for t in info['tools'] if t['name'] == 'create_dir'][0]['description'][:90]}…")

print("\n② 切到 D:\\ProjectA，用 create_dir 建目录 + 写 main.py")
print("=" * 76)
info = call("POST", "/api/config/workspace", {"path": r"D:\ProjectA"})
print(f"  工作区 = {info['workspace']}")

from agent.config import load_config  # noqa: E402
from backend.service.agent_service import AgentService  # noqa: E402

service = AgentService(load_config(r"D:\MyAi\config.yaml"))
print(f"  服务读回 = {service.workspace}")

result = service.registry.call("create_dir", {"path": "src/components"})
print(f"  create_dir('src/components') -> {result}")
assert (Path(r"D:\ProjectA") / "src" / "components").is_dir()

result = service.registry.call("create_dir", {"path": "src/components"})
print(f"  重复 create_dir -> {result}")
assert "已存在" in result

result = service.registry.call(
    "write_file",
    {"path": "main.py", "content": "from src.components import app\n\nprint('hello ProjectA')\n"},
)
print(f"  write_file(main.py) -> {result}")
assert (Path(r"D:\ProjectA") / "main.py").read_text(encoding="utf-8").startswith("from src")

print("\n③ 只读挂载不能建目录")
print("=" * 76)
call("POST", "/api/config/workspace", {
    "path": r"D:\ProjectA",
    "extras": [{"name": "libs", "path": r"D:\SharedLibs", "writable": False}],
})
service = AgentService(load_config(r"D:\MyAi\config.yaml"))
result = service.registry.call("create_dir", {"path": "@libs/newdir"})
print(f"  create_dir('@libs/newdir') -> {result}")
assert result.startswith("错误：") and "只读" in result

print("\n④ 权限错误提示（对一个只读目录写入）")
print("=" * 76)
readonly_dir = Path(r"D:\MyAi\workspace\_ro_probe")
readonly_dir.mkdir(parents=True, exist_ok=True)
import subprocess  # noqa: E402

subprocess.run(
    ["icacls", str(readonly_dir), "/deny", "Everyone:(W)"],
    capture_output=True,
    text=True,
)
call("POST", "/api/config/workspace", {"path": str(readonly_dir)})
service = AgentService(load_config(r"D:\MyAi\config.yaml"))
result = service.registry.call("write_file", {"path": "x.txt", "content": "y"})
print(f"  write_file -> {result[:160]}")
result2 = service.registry.call("create_dir", {"path": "sub"})
print(f"  create_dir -> {result2[:160]}")
subprocess.run(["icacls", str(readonly_dir), "/remove:d", "Everyone"], capture_output=True, text=True)

print("\n⑤ 收尾：恢复默认工作区、清理")
print("=" * 76)
call("DELETE", "/api/config/workspace")
import shutil  # noqa: E402

shutil.rmtree(readonly_dir, ignore_errors=True)
print("  已恢复默认工作区")
print("\n完成 ✅")