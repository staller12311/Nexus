"""真实全链路：POST /api/chat/stream → 真实厂商（qwen3.8-flash）→ reasoning_delta + text_delta。

验证的正是浏览器走的那条路径，只是这里直接读 SSE 帧。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3.8-flash"
QUESTION = "9.11 和 9.9 哪个大，为什么？请简短回答。"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


status, session = call("POST", "/api/sessions", {})
sid = session["id"]
print(f"会话 {sid[:8]}，模型 {MODEL}")
print(f"提问：{QUESTION}\n" + "=" * 70)

body = json.dumps({"session_id": sid, "message": QUESTION, "model": MODEL}, ensure_ascii=False).encode()
request = urllib.request.Request(
    f"{BASE}/api/chat/stream",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
)

start = time.perf_counter()
first_reasoning = first_text = None
reasoning_frames = text_frames = 0
reasoning_text = text = ""
end_event = None
error = None

with urllib.request.urlopen(request, timeout=180) as response:
    for raw in response:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        kind = event.get("type")
        if kind == "reasoning_delta":
            if first_reasoning is None:
                first_reasoning = (time.perf_counter() - start) * 1000
            reasoning_frames += 1
            reasoning_text += event.get("text") or ""
        elif kind == "text_delta":
            if first_text is None:
                first_text = (time.perf_counter() - start) * 1000
            text_frames += 1
            text += event.get("delta") or ""
        elif kind == "message_end":
            end_event = event
        elif kind == "error":
            error = event

total = (time.perf_counter() - start) * 1000
print(f"  reasoning_delta 帧数 = {reasoning_frames}")
if first_reasoning:
    print(f"  首个思考 token 到达: {first_reasoning:.0f} ms")
print(f"  text_delta 帧数 = {text_frames}")
if first_text:
    print(f"  首个正文 token 到达: {first_text:.0f} ms")
print(f"  整轮耗时: {total:.0f} ms")
if end_event:
    print(f"  message_end usage: {end_event['usage']} rounds={end_event['rounds']}")
print(f"\n  思考: {reasoning_text[:100]!r}")
print(f"  正文: {text[:150]!r}")

if error:
    print(f"  ❌ error 事件: {error}")
    sys.exit(1)

assert end_event is not None, "没收到 message_end"
assert text_frames > 1, "❌ 正文不是逐 token"
assert first_text is not None and first_text < total, "❌ 首字没有早于整体结束"

status, detail = call("GET", f"/api/sessions/{sid}")
assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
assert assistant["content"] == text, f"库里内容与流式拼接不一致: {assistant['content']!r} vs {text!r}"
print(f"\n  落库内容一致（{len(assistant['content'])} 字），会话 total_tokens={detail['session']['total_tokens']}")
print("\n✅ 真实全链路通过：思考→正文都是流式增量，且落库一致")