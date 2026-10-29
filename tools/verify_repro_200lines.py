"""真实复现原故障：让 deepseek 写 200 行代码（原先 4096 预算被思考烧光 → 报错）。"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
PROMPT = "写一个200行的python代码"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None


session = call("POST", "/api/sessions", {})
sid = session["id"]
body = json.dumps({"session_id": sid, "message": PROMPT}, ensure_ascii=False).encode()
request = urllib.request.Request(
    f"{BASE}/api/chat/stream",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
)

start = time.perf_counter()
text = ""
reasoning_len = 0
errors: list[dict] = []
end_event = None
tool_calls: list[str] = []

with urllib.request.urlopen(request, timeout=300) as response:
    for raw in response:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event["type"] == "text_delta":
            text += event["delta"]
        elif event["type"] == "reasoning_delta":
            reasoning_len += len(event["text"])
        elif event["type"] == "tool_call_start":
            tool_calls.append(event["name"])
        elif event["type"] == "error":
            errors.append(event)
        elif event["type"] == "message_end":
            end_event = event

elapsed = (time.perf_counter() - start) * 1000
print("=" * 76)
print(f"提问：{PROMPT}")
print("=" * 76)
print(f"  耗时 {elapsed:.0f} ms")
print(f"  error 事件: {errors if errors else '0 个 ✅'}")
print(f"  工具调用: {tool_calls or '无'}")
print(f"  思考: {reasoning_len} 字")
print(f"  正文: {len(text)} 字 / {len(text.splitlines())} 行")
print(f"  usage: {end_event['usage'] if end_event else None}")
print(f"  正文开头: {text[:220]!r}")

assert not errors, f"❌ 仍然报错：{errors}"
assert end_event is not None
assert text, "❌ 正文为空"

detail = call("GET", f"/api/sessions/{sid}")
roles = [m["role"] for m in detail["messages"]]
assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
print(f"\n  落库角色: {roles}")
print(f"  assistant.content 长度: {len(assistant.get('content') or '')}")
print(f"  assistant.reasoning_content 长度: {len(assistant.get('reasoning_content') or '')}")
assert roles[-1] == "assistant"
assert len(assistant.get("content") or "") > 0

truncated_notice = "用完 max_tokens" in text
print(f"\n  是否触发「思考耗尽预算」兜底: {truncated_notice}")
if truncated_notice:
    print("  → 说明 8192 仍不够，需要继续调大 max_tokens（下面的建议正确显示给用户了）")
else:
    print("  → 8192 预算下正常产出了正文 ✅")

print("\n✅ 原场景不再报「assistant 消息必须至少有 content 或 tool_calls」")
print(f"（测试会话 {sid}）")