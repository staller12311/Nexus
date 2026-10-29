"""真实后端联调：/api/config/workspace 的 GET / POST / DELETE，含危险路径拒绝。"""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(r"D:\MyAi")
PROBE = ROOT / "workspace" / "_ws_api_probe"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


shutil.rmtree(PROBE, ignore_errors=True)
PROBE.mkdir(parents=True, exist_ok=True)
(PROBE / "hello.txt").write_text("hello workspace", encoding="utf-8")

print("=" * 76)
print("① GET 当前工作区")
print("=" * 76)
status, info = call("GET", "/api/config/workspace")
print(f"  {status} {info}")
assert status == 200 and info["workspace"].endswith("workspace")
default_ws = info["workspace"]

print("\n② POST 切换到 probe 目录")
print("=" * 76)
status, info = call("POST", "/api/config/workspace", {"path": str(PROBE)})
print(f"  {status} {info}")
assert status == 200 and info["is_default"] is False
assert Path(info["workspace"]) == PROBE.resolve()

print("\n③ 切换后工具立刻能在新目录读写（通过会话发工具调用太绕，直接查服务内部）")
print("=" * 76)
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from agent.config import load_config  # noqa: E402
from backend.service.agent_service import AgentService  # noqa: E402

service = AgentService(load_config(ROOT / "config.yaml"))
print(f"  服务实例读回的工作区: {service.workspace}")
assert service.workspace == PROBE.resolve()
print(f"  read_file(hello.txt) -> {service.registry.call('read_file', {'path': 'hello.txt'})!r}")

print("\n④ 危险路径必须 400")
print("=" * 76)
for label, path in (
    ("C:\\Windows", r"C:\Windows"),
    ("C:\\Windows\\System32", r"C:\Windows\System32"),
    ("盘符根目录", "D:\\"),
    ("相对路径", "workspace"),
    ("不存在的目录", str(PROBE / "nope")),
    ("UNC", r"\\localhost\C$\Windows"),
    ("AppData", str(Path.home() / "AppData")),
):
    status, body = call("POST", "/api/config/workspace", {"path": path})
    detail = body.get("detail", "")
    print(f"  {status}  {label}: {detail[:80]}")
    assert status == 400, f"{label} 竟然没被拒绝"
    assert "安全" in detail or "绝对路径" in detail or "不存在" in detail

print("\n⑤ DELETE 恢复默认")
print("=" * 76)
status, info = call("DELETE", "/api/config/workspace")
print(f"  {status} {info}")
assert status == 200 and info["is_default"] is True
assert Path(info["workspace"]) == Path(default_ws)

shutil.rmtree(PROBE, ignore_errors=True)
print("\n全部通过 ✅ 工作区接口 GET/POST/DELETE 正常，危险路径一律 400")