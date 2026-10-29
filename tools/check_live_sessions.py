"""只读核对：真实库里的中文是否完好，以及最近会话的工具调用结构。"""
from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


print("=== /api/tools ===")
for tool in get("/api/tools")["tools"]:
    print(f"  {tool['name']:11} enabled={tool['enabled']}")

print("\n=== /api/sessions 的 first_user_message（Python 按 UTF-8 解码）===")
sessions = get("/api/sessions?limit=10")["sessions"]
for item in sessions:
    print(f"  {item['id'][:8]}  {item['first_user_message']!r}  msgs={item['message_count']}")

target = next((s for s in sessions if s["message_count"] > 3), sessions[0] if sessions else None)
if target:
    print(f"\n=== 最近一条带工具调用的会话 {target['id'][:8]} 的消息结构 ===")
    detail = get(f"/api/sessions/{target['id']}")
    for index, message in enumerate(detail["messages"], 1):
        role = message["role"]
        if message.get("tool_calls"):
            calls = [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in message["tool_calls"]]
            print(f"  [{index}] {role:9} tool_calls={calls}")
            print(f"        content={message.get('content')!r}")
        elif role == "tool":
            print(f"  [{index}] {role:9} tool_call_id={message['tool_call_id']}")
            print(f"        content={str(message.get('content'))[:60]!r}")
        else:
            print(f"  [{index}] {role:9} content={str(message.get('content'))[:70]!r}")