"""真实模型验证：问实时问题 → 模型应主动调用 web_search 并给出带链接的回答。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def stream(message: str) -> list[dict]:
    _, session = call("POST", "/api/sessions", {})
    session_id = session["id"]
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
    return session_id, events


QUESTION = "帮我查一下 Python 最新稳定版本是多少，给出来源链接。"
print("=" * 74)
print(f"提问：{QUESTION}")
print("=" * 74)
start = time.perf_counter()
session_id, events = stream(QUESTION)
elapsed = (time.perf_counter() - start) * 1000

tool_calls = [e for e in events if e["type"] == "tool_call_start"]
tool_ends = [e for e in events if e["type"] == "tool_call_end"]
answer = "".join(e["delta"] for e in events if e["type"] == "text_delta")
reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning_delta")

print(f"  耗时 {elapsed:.0f} ms")
print(f"  调用的工具: {[(t['name'], t['arguments'][:60]) for t in tool_calls]}")
for end in tool_ends:
    print(f"  工具结果片段（{end['name']}）：{end['content'][:160]!r}…")
print(f"  思考 {len(reasoning)} 字")
print(f"  回答 {len(answer)} 字：{answer[:400]}")

called = [t["name"] for t in tool_calls]
print(f"\n  ✅ 调用 web_search: {'web_search' in called}")
assert "web_search" in called, f"❌ 模型没有联网搜索，实际调用 {called}"
assert "http" in answer, "❌ 回答里没有来源链接"

print(f"\n（测试会话 {session_id} 可删除）")