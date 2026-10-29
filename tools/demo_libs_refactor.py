"""真实跑一遍：挂载 @libs，发一句人话，看模型怎么改。"""
import json, sys, time, urllib.request
sys.path.insert(0, r"D:\MyAi")
BASE = "http://127.0.0.1:8000"

def call(method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw.decode()) if raw else None

def stream(sid, message):
    data = json.dumps({"session_id": sid, "message": message}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{BASE}/api/chat/stream", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    events = []
    with urllib.request.urlopen(req, timeout=240) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events

print("=" * 74)
print("步骤：主目录 = ProjectA，附加 @libs = D:\\SharedLibs（只读）")
print("=" * 74)
info = call("POST", "/api/config/workspace", {
    "path": r"D:\MyAi\workspace\ProjectA",
    "extras": [{"name": "libs", "path": r"D:\SharedLibs", "writable": False}],
})
print("  ", json.dumps(info, ensure_ascii=False))

prompt = "读一下 @libs/calculator.py 提供哪些函数，然后把 main.py 里的 add 改成从共享库导入，乘法也改成调用共享库。"
print("\n我发的问题：", prompt)
session = call("POST", "/api/sessions", {})
start = time.perf_counter()
events = stream(session["id"], prompt)
elapsed = (time.perf_counter() - start) * 1000
calls = [(e["name"], e["arguments"]) for e in events if e["type"] == "tool_call_start"]
ends = [(e["name"], e["ok"], e["content"]) for e in events if e["type"] == "tool_call_end"]
answer = "".join(e["delta"] for e in events if e["type"] == "text_delta")
print(f"\n模型动作（{elapsed:.0f} ms）:")
for name, args in calls:
    print(f"  · {name} {args}")
print("\n回答:", answer[:200].replace("\n", " "))
print("\n最终 main.py 内容:")
print("-" * 60)
from pathlib import Path
print(Path(r"D:\MyAi\workspace\ProjectA\main.py").read_text(encoding="utf-8"))
print("-" * 60)
call("DELETE", "/api/config/workspace")
print("（已恢复默认工作区）")
