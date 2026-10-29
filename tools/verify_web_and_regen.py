"""验证 web_search（真实联网）+ regenerate（历史不重复、回答被覆盖）。"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"
sys.path.insert(0, r"D:\MyAi")

from agent.config import load_config  # noqa: E402
from backend.service.agent_service import AgentService  # noqa: E402


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def control(mode: str) -> None:
    body = json.dumps({"mode": mode}).encode()
    request = urllib.request.Request(
        f"{FAKE}/control", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


def stream(path: str) -> list[dict]:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=b"{}",
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


def chat_stream(session_id: str, message: str) -> list[dict]:
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
    return events


print("=" * 74)
print("① web_search 工具真实联网（Bing 主 / 百度兜底）")
print("=" * 74)
service = AgentService(load_config(r"D:\MyAi\tools\config.integration.yaml"))
names = [schema["function"]["name"] for schema in service.registry.schemas()]
print(f"  已注册工具: {names}")
assert "web_search" in names

spec = service.registry.get("web_search")
assert spec is not None
context = spec.handler(query="Python 3.13 发布 新特性", top_k=3)
print("  " + context.replace("\n", "\n  ")[:600])
assert "来源：" in context and "http" in context, "❌ 搜索结果缺少链接"
assert "错误" not in context.split("\n")[0], "❌ 搜索失败"

print("\n" + "=" * 74)
print("② 缓存：同一 query 第二次应几乎瞬时")
print("=" * 74)
import time  # noqa: E402

start = time.perf_counter()
service.registry.get("web_search").handler(query="Python 3.13 发布 新特性", top_k=3)
elapsed_cached = (time.perf_counter() - start) * 1000
print(f"  第二次耗时 {elapsed_cached:.1f} ms（缓存命中应 < 50ms）")
assert elapsed_cached < 500

print("\n" + "=" * 74)
print("③ regenerate：历史不重复、旧回答被覆盖")
print("=" * 74)
control("simple")
_, session = call("POST", "/api/sessions", {})
sid = session["id"]
events1 = chat_stream(sid, "读一下 readme.txt")
answer1 = "".join(e["delta"] for e in events1 if e["type"] == "text_delta")
_, detail1 = call("GET", f"/api/sessions/{sid}")
print(f"  首次对话：消息数={len(detail1['messages'])}，回答={answer1[:40]!r}")
first_roles = [m["role"] for m in detail1["messages"]]
print(f"  角色序列: {first_roles}")

events2 = stream(f"/api/sessions/{sid}/regenerate")
answer2 = "".join(e["delta"] for e in events2 if e["type"] == "text_delta")
_, detail2 = call("GET", f"/api/sessions/{sid}")
roles2 = [m["role"] for m in detail2["messages"]]
user_messages = [m for m in detail2["messages"] if m["role"] == "user"]
print(f"  重新生成后：消息数={len(detail2['messages'])}，角色序列={roles2}")
print(f"  回答={answer2[:40]!r}")
print(f"  user 消息条数={len(user_messages)}（必须还是 1 条，不能重复）")

assert roles2 == first_roles, f"❌ 角色序列被破坏：{first_roles} -> {roles2}"
assert len(user_messages) == 1, "❌ user 消息重复了"
assert answer2 == answer1, "❌ 假上游同输入应给同答案（说明确实重跑了）"
assert len(detail2["messages"]) == len(detail1["messages"]), "❌ 消息数膨胀"

print("\n" + "=" * 74)
print("④ 重新生成后仍能正常追加新一轮")
print("=" * 74)
events3 = chat_stream(sid, "再读一次")
_, detail3 = call("GET", f"/api/sessions/{sid}")
roles3 = [m["role"] for m in detail3["messages"]]
print(f"  角色序列: {roles3}")
assert roles3.count("user") == 2
assert roles3[-1] == "assistant"

print("\n全部通过 ✅ web_search 可用；regenerate 覆盖且历史不重复")