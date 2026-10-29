"""端到端：reasoning 落库 → 会话详情返回 → 回放给模型时不带 reasoning_content。"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"
DB = pathlib.Path(r"D:\MyAi\tools\data\integration.db")


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def control(mode: str) -> None:
    body = json.dumps({"mode": mode}).encode()
    request = urllib.request.Request(
        f"{FAKE}/control", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


def stream(session_id: str, message: str) -> list[dict]:
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events = []
    with urllib.request.urlopen(request, timeout=90) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


print("=" * 72)
print("① 流式一轮（假上游返回 reasoning + 正文）")
print("=" * 72)
control("reason")
status, session = call("POST", "/api/sessions", {})
sid = session["id"]
events = stream(sid, "9.11 和 9.9 哪个大？")
reasoning_streamed = "".join(e["text"] for e in events if e["type"] == "reasoning_delta")
text_streamed = "".join(e["delta"] for e in events if e["type"] == "text_delta")
print(f"  流式 reasoning {len(reasoning_streamed)} 字 / 正文 {len(text_streamed)} 字")

print("\n" + "=" * 72)
print("② GET /api/sessions/{id} 必须带回 reasoning_content（给前端回放）")
print("=" * 72)
status, detail = call("GET", f"/api/sessions/{sid}")
assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
print(f"  assistant.content = {assistant['content'][:30]!r}…")
print(f"  assistant.reasoning_content = {str(assistant.get('reasoning_content'))[:40]!r}…")
assert assistant.get("reasoning_content") == reasoning_streamed, "❌ 落库的 reasoning 与流式内容不一致"

print("\n" + "=" * 72)
print("③ 第二轮请求：发给模型的历史里不能有 reasoning_content")
print("=" * 72)
control("simple")
stream(sid, "再读一下 readme.txt")
last = json.loads(urllib.request.urlopen(f"{FAKE}/last", timeout=10).read().decode("utf-8"))
raw = last.get("raw", "")
print(f"  上游收到的最后一轮请求片段: {raw[:60]!r}…")
assert "reasoning_content" not in raw, "❌ reasoning_content 被发给了模型"

print("\n" + "=" * 72)
print("④ 直接查库确认列与值")
print("=" * 72)
import sqlite3

connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
rows = connection.execute(
    "SELECT seq, role, length(content), length(reasoning_content) FROM messages"
    " WHERE session_id = ? ORDER BY seq",
    (sid,),
).fetchall()
connection.close()
for seq, role, content_len, reasoning_len in rows:
    print(f"  seq={seq} role={role:<9} content_len={content_len} reasoning_len={reasoning_len}")
assert any(reasoning_len for _, _, _, reasoning_len in rows), "❌ 库里没有 reasoning_content"

print("\n全部通过 ✅")