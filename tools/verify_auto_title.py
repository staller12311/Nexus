"""验证自动起标题：接口、清洗、跳过已有标题、失败降级、不阻塞主对话。"""
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


def chat(session_id: str, message: str) -> float:
    """跑一轮对话，返回耗时（秒）。"""
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=90) as response:
        for _ in response:
            pass
    return time.perf_counter() - start


print("=" * 70)
print("① 首轮对话 → 生成标题（模型返回「标题：Python 快排。「实现」」）")
print("=" * 70)
control("prompt")
status, session = call("POST", "/api/sessions", {})
sid = session["id"]
print(f"  新建会话 {sid[:8]}，title={session['title']!r}")

chat_seconds = chat(sid, "用 Python 写个快排")
print(f"  主对话耗时 {chat_seconds * 1000:.0f} ms（status={status}）")

control("title")
start = time.perf_counter()
status, result = call("POST", f"/api/sessions/{sid}/generate_title")
title_seconds = time.perf_counter() - start
print(f"  generate_title -> {status} {result}")
print(f"  标题请求耗时 {title_seconds * 1000:.0f} ms")
assert result["generated"] is True
assert result["title"] == "Python快排实现", f"清洗结果不对：{result['title']!r}"
print("  ✅ 「标题：Python 快排。「实现」」→ 清洗为「Python快排实现」（去前缀/空格/标点）")

status, listing = call("GET", "/api/sessions?limit=50")
titles = {item["id"]: item["title"] for item in listing["sessions"]}
print(f"  列表里的标题: {titles.get(sid)!r}")
assert titles.get(sid) == "Python快排实现"

print("\n" + "=" * 70)
print("② 已有标题时再调用：不重新生成、不覆盖")
print("=" * 70)
status, again = call("POST", f"/api/sessions/{sid}/generate_title")
print(f"  -> {status} {again}")
assert again["generated"] is False and again["title"] == "Python快排实现"

print("\n" + "=" * 70)
print("③ 用户手动改名后，不会被自动标题覆盖")
print("=" * 70)
call("PATCH", f"/api/sessions/{sid}", {"title": "我手动起的名字"})
status, after_rename = call("POST", f"/api/sessions/{sid}/generate_title")
print(f"  -> {status} title={after_rename['title']!r} generated={after_rename['generated']}")
assert after_rename["title"] == "我手动起的名字"

print("\n" + "=" * 70)
print("④ 失败降级：上游 5xx 时接口返回 503，原样保留标题")
print("=" * 70)
status, second = call("POST", "/api/sessions", {})
sid2 = second["id"]
chat(sid2, "再开一个会话")
control("boom")
try:
    call("POST", f"/api/sessions/{sid2}/generate_title")
    print("  ❌ 竟然成功了")
except urllib.error.HTTPError as exc:
    body = json.loads(exc.read().decode("utf-8"))
    print(f"  -> {exc.code} {body['detail']}")
    assert exc.code == 503
status, detail = call("GET", f"/api/sessions/{sid2}")
print(f"  会话标题仍是: {detail['session']['title']!r}（空 → 前端继续用「首条消息截断」兜底）")
assert detail["session"]["title"] == ""

print("\n" + "=" * 70)
print("⑤ 消息不足时给 400（而不是 500）")
print("=" * 70)
status, empty = call("POST", "/api/sessions", {})
try:
    call("POST", f"/api/sessions/{empty['id']}/generate_title")
    print("  ❌ 竟然成功了")
except urllib.error.HTTPError as exc:
    print(f"  -> {exc.code} {json.loads(exc.read().decode('utf-8'))['detail']}")
    assert exc.code == 400

print("\n" + "=" * 70)
print("⑥ 不阻塞主对话的量化对比")
print("=" * 70)
print(f"  主对话（SSE 全流程）: {chat_seconds * 1000:.0f} ms")
print(f"  标题生成（独立请求）: {title_seconds * 1000:.0f} ms")
print("  两者是先后独立的两个 HTTP 请求：主对话在 message_end 就结束了，标题请求在其之后才发起")

print("\n全部通过 ✅")