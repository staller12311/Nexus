"""真实手测：① 联网搜索最近的新闻  ② 点一次「重新生成」。

走的是浏览器同一条路径（POST /api/chat/stream 与 POST /api/sessions/{id}/regenerate），
用真实模型 + 真实 Bing 搜索。
"""
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


def stream(path: str, payload: dict) -> list[dict]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=180) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def summarize(events: list[dict]) -> dict:
    tools = [e for e in events if e["type"] == "tool_call_start"]
    ends = [e for e in events if e["type"] == "tool_call_end"]
    return {
        "tools": [(t["name"], t["arguments"][:70]) for t in tools],
        "tool_output_chars": sum(len(e.get("content") or "") for e in ends),
        "answer": "".join(e["delta"] for e in events if e["type"] == "text_delta"),
        "reasoning_chars": len("".join(e["text"] for e in events if e["type"] == "reasoning_delta")),
        "tool_preview": (ends[0]["content"][:200] if ends else ""),
    }


print("=" * 78)
print("① 联网搜索一条最近的新闻")
print("=" * 78)
QUESTION = "帮我搜一下今天最重要的科技新闻，用一句话概括并给出链接。"
_, session = call("POST", "/api/sessions", {})
sid = session["id"]
start = time.perf_counter()
events = stream("/api/chat/stream", {"session_id": sid, "message": QUESTION})
info = summarize(events)
elapsed = (time.perf_counter() - start) * 1000

print(f"  提问：{QUESTION}")
print(f"  耗时 {elapsed:.0f} ms")
print(f"  调用的工具: {info['tools']}")
print(f"  工具结果片段: {info['tool_preview']!r}…")
print(f"  思考 {info['reasoning_chars']} 字 / 回答 {len(info['answer'])} 字")
print(f"  回答：{info['answer'][:300]}")

called = [name for name, _ in info["tools"]]
assert "web_search" in called, f"❌ 没有联网搜索，实际调用 {called}"
assert "http" in info["answer"], "❌ 回答里没有来源链接"

print("\n" + "=" * 78)
print("② 点一次「重新生成」")
print("=" * 78)
_, before = call("GET", f"/api/sessions/{sid}")
print(f"  重新生成前：{len(before['messages'])} 条，角色={[m['role'] for m in before['messages']]}")
old_answer = [m for m in before["messages"] if m["role"] == "assistant"][-1]["content"]

start = time.perf_counter()
events2 = stream(f"/api/sessions/{sid}/regenerate", {})
info2 = summarize(events2)
elapsed2 = (time.perf_counter() - start) * 1000
print(f"  耗时 {elapsed2:.0f} ms，工具调用={info2['tools']}")
print(f"  新回答（前 200 字）：{info2['answer'][:200]}")

_, after = call("GET", f"/api/sessions/{sid}")
print(f"  重新生成后：{len(after['messages'])} 条，角色={[m['role'] for m in after['messages']]}")
users = [m for m in after["messages"] if m["role"] == "user"]
print(f"  user 消息条数={len(users)}（必须 1 条）")
# 重新生成后工具调用次数可能与上次不同（消息条数自然不同），只校验结构不变量
assert len(users) == 1, "❌ user 消息重复"
prefix_before = [m["role"] for m in before["messages"][:2]]
prefix_after = [m["role"] for m in after["messages"][:2]]
assert prefix_before == prefix_after == ["system", "user"], "❌ 会话前缀被破坏"
assert after["messages"][-1]["role"] == "assistant", "❌ 最后一条不是 assistant"
tool_ids = {m["tool_call_id"] for m in after["messages"] if m["role"] == "tool"}
for message in after["messages"]:
    if message["role"] == "assistant" and message.get("tool_calls"):
        for call in message["tool_calls"]:
            assert call["id"] in tool_ids, f"❌ 孤立的 tool_call：{call['id']}"
new_answer = [m for m in after["messages"] if m["role"] == "assistant"][-1]["content"]
print(f"  旧回答与新回答不同: {old_answer != new_answer}")
assert new_answer and new_answer != old_answer, "❌ 重新生成没有产生新回答"

print("\n" + "=" * 78)
print("③ 重新生成后还能继续追问（会话没被弄坏）")
print("=" * 78)
events3 = stream("/api/chat/stream", {"session_id": sid, "message": "把刚才那条新闻再缩短到 20 字以内。"})
info3 = summarize(events3)
print(f"  工具={info3['tools']}  回答={info3['answer'][:120]!r}")
_, final = call("GET", f"/api/sessions/{sid}")
print(f"  最终角色序列={[m['role'] for m in final['messages']]}")
assert final["messages"][-1]["role"] == "assistant"

print("\n全部通过 ✅ 联网搜索与重新生成都真实跑通")
print(f"（测试会话 {sid}）")