"""真实闭环：切工作区 → 模型在新目录读文件 → 越界仍被拦 → 恢复默认。"""
from __future__ import annotations

import json
import shutil
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(r"D:\MyAi")
PROJECT = ROOT / "workspace" / "_ws_demo_project"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None


def stream(session_id: str, message: str) -> list[dict]:
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events = []
    with urllib.request.urlopen(request, timeout=180) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


shutil.rmtree(PROJECT, ignore_errors=True)
PROJECT.mkdir(parents=True)
(PROJECT / "hello_demo.py").write_text(
    'def greet(name: str) -> str:\n    """返回问候语。"""\n    return f"你好，{name}！"\n',
    encoding="utf-8",
)

print("=" * 76)
print("① 切换工作区到演示项目")
print("=" * 76)
info = call("POST", "/api/config/workspace", {"path": str(PROJECT)})
print(f"  {info}")
assert info["is_default"] is False

print("\n② 新会话提问：让模型读新目录里的文件")
print("=" * 76)
session = call("POST", "/api/sessions", {})
sid = session["id"]
start = time.perf_counter()
events = stream(sid, "读一下 hello_demo.py，用一句话说它是干什么的。")
elapsed = (time.perf_counter() - start) * 1000
tool_calls = [e for e in events if e["type"] == "tool_call_start"]
tool_ends = [e for e in events if e["type"] == "tool_call_end"]
answer = "".join(e["delta"] for e in events if e["type"] == "text_delta")
errors = [e for e in events if e["type"] == "error"]
print(f"  耗时 {elapsed:.0f} ms，工具调用: {[(t['name'], t['arguments']) for t in tool_calls]}")
print(f"  工具结果: {(tool_ends[0]['content'][:120] if tool_ends else '无')!r}")
print(f"  回答: {answer[:160]}")
print(f"  error: {errors or '0 个 ✅'}")
assert not errors
assert any(t["name"] == "read_file" for t in tool_calls), "模型没调用 read_file"
assert tool_ends and tool_ends[0]["ok"] is True, "工具执行失败"
assert "greet" in (tool_ends[0]["content"] or "") or "问候" in answer

print("\n③ 同一个会话再让它跨目录读系统文件：必须被拦")
print("=" * 76)
events2 = stream(sid, r"再帮我读一下 C:\Windows\System32\drivers\etc\hosts。")
tool_ends2 = [e for e in events2 if e["type"] == "tool_call_end"]
answer2 = "".join(e["delta"] for e in events2 if e["type"] == "text_delta")
blocked = [e for e in tool_ends2 if not e["ok"]]
print(f"  工具结果 ok: {[e['ok'] for e in tool_ends2]}")
print(f"  拦截内容: {blocked[0]['content'][:120] if blocked else '（模型没调工具）'}")
print(f"  回答: {answer2[:140]}")
if blocked:
    assert "越出 workspace" in blocked[0]["content"]
    print("  → 模型尝试调用、被工具层拦截 ✅")
else:
    print("  → 模型自己就拒绝了调用（它知道沙箱限制），改由直接调用工具验证守卫")
    import sys
    sys.path.insert(0, str(ROOT))
    from agent.config import load_config
    from backend.service.agent_service import AgentService

    direct = AgentService(load_config(ROOT / "config.yaml")).registry.call(
        "read_file", {"path": r"C:\Windows\System32\drivers\etc\hosts"}
    )
    print(f"  直接调用 read_file -> {direct[:120]}")
    assert direct.startswith("错误：") and "越出 workspace" in direct

print("\n④ 恢复默认工作区并清理")
print("=" * 76)
info = call("DELETE", "/api/config/workspace")
print(f"  {info}")
assert info["is_default"] is True
shutil.rmtree(PROJECT, ignore_errors=True)
print("\n全部通过 ✅ 切换生效、新目录可读写、越界仍被拦、可恢复默认")