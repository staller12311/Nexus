"""真实闭环：多根工作区（主目录 + @libs 只读挂载）+ 系统提示词注入验证。"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(r"D:\MyAi")
DEMO = ROOT / "workspace" / "_mr_demo"
PROJECT = DEMO / "ProjectA"
LIBS = DEMO / "SharedLibs"


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
    with urllib.request.urlopen(request, timeout=240) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


shutil.rmtree(DEMO, ignore_errors=True)
PROJECT.mkdir(parents=True)
(LIBS / "http").mkdir(parents=True)
(PROJECT / "app.py").write_text("import http_client\nprint(http_client.PORT)\n", encoding="utf-8")
(LIBS / "http" / "client.py").write_text(
    '"""共享 HTTP 客户端。"""\nPORT = 8080\n\n\ndef get(url: str) -> str:\n    return f"GET {url}"\n',
    encoding="utf-8",
)

print("=" * 80)
print("① 切换为「主目录 + @libs 只读挂载」")
print("=" * 80)
info = call(
    "POST",
    "/api/config/workspace",
    {"path": str(PROJECT), "extras": [{"name": "libs", "path": str(LIBS), "writable": False}]},
)
print(f"  {json.dumps(info, ensure_ascii=False)}")
assert info["extras"] == [{"name": "libs", "path": str(LIBS.resolve()), "writable": False}]

print("\n② 系统提示词是否真的注入了挂载清单（第 6 步关键）")
print("=" * 80)
sys.path.insert(0, str(ROOT))
from agent.config import load_config  # noqa: E402
from backend.service.agent_service import AgentService  # noqa: E402

service = AgentService(load_config(ROOT / "config.yaml"))
prompt = service._loop_config().system_prompt_text()
added = prompt[prompt.find("【附加工作区】") :]
print("  ---- 注入片段 ----")
for line in added.splitlines()[:8]:
    print(f"  {line}")
print("  ------------------")
assert "【附加工作区】" in prompt
assert "@libs" in prompt and str(LIBS.resolve()) in prompt
assert "只读" in prompt
assert "@libs/http/client.py" in prompt, "提示词示例里应给出可复制用法"

print("\n③ 真实模型：让它用 @libs 读共享库，再在主目录写 usage.py")
print("=" * 80)
session = call("POST", "/api/sessions", {})
sid = session["id"]
start = time.perf_counter()
events = stream(
    sid,
    "main workspace 里有个 app.py。请先用 @libs 读出 http/client.py 的内容，"
    "然后在主目录写 usage.py：导入 client 并打印 client.PORT。"
    "注意共享库是只读的，只能读不能改。",
)
elapsed = (time.perf_counter() - start) * 1000
calls = [(e["name"], e["arguments"]) for e in events if e["type"] == "tool_call_start"]
ends = [(e["name"], e["ok"], e["content"]) for e in events if e["type"] == "tool_call_end"]
answer = "".join(e["delta"] for e in events if e["type"] == "text_delta")
print(f"  耗时 {elapsed:.0f} ms")
for name, arguments in calls:
    print(f"  调用: {name} {arguments}")
print(f"  error: {[e for e in events if e['type'] == 'error'] or '0 个 ✅'}")
print(f"  回答: {answer[:180]}")

lib_reads = [c for c in ends if c[0] == "read_file" and "@libs" in (c[2] or "") or "@libs" in (calls[0][1] if calls else "")]
assert any(c[0] == "read_file" and c[1] for c in ends), "没有成功读取共享库"
assert any("@libs" in arguments for _, arguments in calls), "模型没有使用 @libs 前缀（提示词注入失效）"
usage_file = PROJECT / "usage.py"
print(f"  usage.py 是否写出: {usage_file.exists()}")
if usage_file.exists():
    print(f"  内容: {usage_file.read_text(encoding='utf-8')[:120]!r}")

print("\n④ 只读约束：直接让模型写 @libs（必须被拒、且不落盘）")
print("=" * 80)
events2 = stream(sid, "把 @libs/http/client.py 里的 PORT 改成 9090，直接改那个文件。")
ends2 = [(e["name"], e["ok"], e["content"]) for e in events2 if e["type"] == "tool_call_end"]
answer2 = "".join(e["delta"] for e in events2 if e["type"] == "text_delta")
print(f"  工具结果: {[(n, ok) for n, ok, _ in ends2]}")
for name, ok, content in ends2:
    print(f"    {name} ok={ok} -> {(content or '')[:100]}")
print(f"  回答: {answer2[:160]}")
shared_text = (LIBS / "http" / "client.py").read_text(encoding="utf-8")
assert "PORT = 8080" in shared_text, "只读挂载竟然被改写了"
writes_to_libs = [c for c in ends2 if c[0] == "write_file" and c[1]]
assert not writes_to_libs, "只读挂载的写入竟然成功了"

print("\n⑤ 恢复默认并清理")
print("=" * 80)
info = call("DELETE", "/api/config/workspace")
print(f"  {json.dumps(info, ensure_ascii=False)}")
assert info["is_default"] is True and info["extras"] == []
shutil.rmtree(DEMO, ignore_errors=True)
print("\n全部通过 ✅ 多根生效、提示词注入生效、只读挂载保护生效、模型会用 @libs")