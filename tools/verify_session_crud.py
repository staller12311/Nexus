"""验证 PATCH /api/sessions/{id} 与 DELETE /api/sessions/{id}（含消息级联删除）。"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"
DB = ROOT / "tools" / "data" / "integration.db"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def chat(session_id: str, message: str) -> None:
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        for _ in response:
            pass


def control(mode: str) -> None:
    body = json.dumps({"mode": mode}).encode()
    request = urllib.request.Request(
        f"{FAKE}/control", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


def count_messages(session_id: str) -> int:
    connection = sqlite3.connect(str(DB))
    row = connection.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    connection.close()
    return int(row[0])


control("prompt")

print("=" * 66)
print("PATCH 重命名")
print("=" * 66)
status, first = call("POST", "/api/sessions", {"title": "原来的名字"})
sid = first["id"]
chat(sid, "hi")  # 让它有消息，便于验证级联
print(f"  新建 {sid[:8]}，消息数={count_messages(sid)}")

status, updated = call("PATCH", f"/api/sessions/{sid}", {"title": "改成文言文助手"})
print(f"  PATCH -> {status} title={updated['title']!r}")
assert updated["title"] == "改成文言文助手"

status, detail = call("GET", f"/api/sessions/{sid}")
print(f"  GET 详情 title={detail['session']['title']!r}（历史消息不丢，共 {len(detail['messages'])} 条）")
assert detail["session"]["title"] == "改成文言文助手"
assert len(detail["messages"]) >= 3

status, listing = call("GET", "/api/sessions?limit=50")
titles = {item["id"]: item["title"] for item in listing["sessions"]}
print(f"  列表里的标题: {titles.get(sid)!r}")
assert titles.get(sid) == "改成文言文助手"

print("\n  首尾空格会被裁掉：")
status, trimmed = call("PATCH", f"/api/sessions/{sid}", {"title": "   两边有空格   "})
print(f"    -> {trimmed['title']!r}")
assert trimmed["title"] == "两边有空格"

print("\n  空标题 = 清空（前端退回用首条消息兜底）：")
status, cleared = call("PATCH", f"/api/sessions/{sid}", {"title": ""})
print(f"    -> title={cleared['title']!r}")
assert cleared["title"] == ""

print("\n" + "=" * 66)
print("DELETE 删除 + 级联")
print("=" * 66)
before = count_messages(sid)
print(f"  删除前该会话消息数: {before}")
status, deleted = call("DELETE", f"/api/sessions/{sid}")
print(f"  DELETE -> {status} {deleted['deleted']} session={deleted['session']['id'][:8]}")
assert deleted["deleted"] is True
after = count_messages(sid)
print(f"  删除后该会话消息数: {after}（应为 0，外键 ON DELETE CASCADE 生效）")
assert after == 0

try:
    call("GET", f"/api/sessions/{sid}")
    print("  ❌ 删完还能查到")
except urllib.error.HTTPError as exc:
    print(f"  再查该会话 -> {exc.code}（预期 404）")

status, listing = call("GET", "/api/sessions?limit=50")
assert sid not in {item["id"] for item in listing["sessions"]}
print("  列表里已经没有它 ✅")

print("\n" + "=" * 66)
print("错误路径")
print("=" * 66)
for label, method, path, payload in (
    ("重命名不存在的会话", "PATCH", "/api/sessions/nope1234", {"title": "x"}),
    ("删除不存在的会话", "DELETE", "/api/sessions/nope1234", None),
):
    try:
        call(method, path, payload)
        print(f"  ❌ {label} 竟然成功")
    except urllib.error.HTTPError as exc:
        print(f"  {label} -> {exc.code} {exc.read().decode('utf-8')[:40]}")

print("\n全部通过 ✅")