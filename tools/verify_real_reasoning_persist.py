"""真实厂商全链路：reasoning 落库 → 详情接口回显 → 下一轮不把 reasoning 发给模型。"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
DB = Path(r"D:\MyAi\data\sessions.db")
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3.8-flash"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def stream(session_id: str, message: str) -> list[dict]:
    body = json.dumps({"session_id": session_id, "message": message, "model": MODEL}, ensure_ascii=False).encode()
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


status, session = call("POST", "/api/sessions", {"model": MODEL})
sid = session["id"]
print(f"会话 {sid[:8]}，模型 {MODEL}")

print("=" * 72)
print("① 真实一轮：流式收 reasoning + 正文")
print("=" * 72)
start = time.perf_counter()
events = stream(sid, "9.11 和 9.9 哪个大，为什么？请简短回答。")
elapsed = (time.perf_counter() - start) * 1000
reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning_delta")
text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
print(f"  reasoning_delta {len([e for e in events if e['type']=='reasoning_delta'])} 帧 / {len(reasoning)} 字")
print(f"  text_delta      {len([e for e in events if e['type']=='text_delta'])} 帧 / {len(text)} 字")
print(f"  整轮 {elapsed:.0f} ms")

print("\n" + "=" * 72)
print("② 落库检查（直接查 sqlite）")
print("=" * 72)
connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
row = connection.execute(
    "SELECT seq, length(content), length(reasoning_content), reasoning_content IS NOT NULL"
    " FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY seq DESC LIMIT 1",
    (sid,),
).fetchone()
connection.close()
print(f"  seq={row[0]} content_len={row[1]} reasoning_len={row[2]} 非空={bool(row[3])}")
assert row[2] and row[2] > 0, "❌ 库里没有 reasoning_content"

print("\n" + "=" * 72)
print("③ GET /api/sessions/{id} 回显（前端刷新后靠这个渲染面板）")
print("=" * 72)
status, detail = call("GET", f"/api/sessions/{sid}")
assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
stored = assistant.get("reasoning_content") or ""
print(f"  reasoning_content 长度={len(stored)}")
print(f"  与流式内容一致: {stored == reasoning}")
assert stored == reasoning.strip() or stored == reasoning

print("\n" + "=" * 72)
print("④ 第二轮对话：历史里的 reasoning 不应发给模型（看日志无 400 且正常返回）")
print("=" * 72)
events2 = stream(sid, "用一个词总结你的结论。")
errors = [e for e in events2 if e["type"] == "error"]
print(f"  第二轮事件数={len(events2)}  error 事件={len(errors)}")
assert not errors, f"❌ 第二轮报错：{errors}"
text2 = "".join(e["delta"] for e in events2 if e["type"] == "text_delta")
print(f"  第二轮正文: {text2[:60]!r}")

print("\n✅ 真实全链路：reasoning 落库、详情回显、回放不污染模型请求")
print(f"（清理提示：测试会话 {sid} 可用 DELETE /api/sessions/{sid} 删除）")