"""端到端验证真流式：SSE 里逐 token 的 text_delta + reasoning_delta + 工具分片。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"


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
print("① reasoning_delta + 逐 token 的 text_delta")
print("=" * 72)
control("reason")
status, session = call("POST", "/api/sessions", {})
sid = session["id"]
start = time.perf_counter()
events = stream(sid, "9.11 和 9.9 哪个大？")
elapsed = (time.perf_counter() - start) * 1000

kinds = [e["type"] for e in events]
from collections import Counter

print(f"  总耗时 {elapsed:.0f} ms，事件计数: {dict(Counter(kinds))}")
reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning_delta")
text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
print(f"  思考内容（{len(reasoning)} 字）: {reasoning[:40]}…")
print(f"  正文（{len(text)} 字）: {text[:40]}…")
end = next(e for e in events if e["type"] == "message_end")
print(f"  message_end: usage={end['usage']} rounds={end['rounds']}")

reasoning_count = kinds.count("reasoning_delta")
text_count = kinds.count("text_delta")
print(f"  reasoning_delta 帧数={reasoning_count}  text_delta 帧数={text_count}")
assert reasoning_count > 1, "❌ 思考内容没有分片推送"
assert text_count > 1, "❌ 正文不是逐 token 推送"
assert "9.90" in reasoning and "9.11" in text
assert end["usage"]["total_tokens"] > 0

print("\n" + "=" * 72)
print("② 工具调用：流式分片拼装后仍能正确执行")
print("=" * 72)
control("simple")
status, session2 = call("POST", "/api/sessions", {})
sid2 = session2["id"]
events2 = stream(sid2, "读一下 readme.txt")
kinds2 = [e["type"] for e in events2]
print(f"  事件序列: {kinds2}")
tool_start = next(e for e in events2 if e["type"] == "tool_call_start")
tool_end = next(e for e in events2 if e["type"] == "tool_call_end")
print(f"  tool_call_start: {tool_start['name']} {tool_start['arguments']}")
print(f"  tool_call_end:   ok={tool_end['ok']} content={tool_end['content'][:30]!r}")
assert tool_start["name"] == "read_file"
assert json.loads(tool_start["arguments"]) == {"path": "readme.txt"}
assert tool_end["ok"] is True and "hi" in tool_end["content"]
text2 = "".join(e["delta"] for e in events2 if e["type"] == "text_delta")
print(f"  最终回答: {text2[:40]!r}")

print("\n" + "=" * 72)
print("③ 落库内容（流式拼接的结果与正文一致）")
print("=" * 72)
status, detail = call("GET", f"/api/sessions/{sid}")
assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
print(f"  库里 assistant.content 长度={len(assistant['content'] or '')}")
assert assistant["content"] == text
print(f"  tokens: {detail['session']['total_tokens']}")

print("\n全部通过 ✅")