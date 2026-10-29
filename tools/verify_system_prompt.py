"""验证 PATCH /api/sessions/{id}/system_prompt：改完下一句立即生效（含"原本没有 system 行"的插入路径）。"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def control(mode: str) -> None:
    body = json.dumps({"mode": mode}).encode()
    request = urllib.request.Request(
        f"{FAKE}/control", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


def chat(session_id: str, message: str) -> str:
    """打 SSE 流，返回拼起来的最终文本。"""
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    text = ""
    with urllib.request.urlopen(request, timeout=60) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "text_delta":
                text += event["delta"]
            elif event["type"] == "error":
                raise RuntimeError(event)
    return text


print("=" * 70)
print("场景 A：会话已有 system 行（先聊一句，默认提示词来自 config）→ 再 PATCH → 下一句生效")
print("=" * 70)
control("prompt")
status, session = call("POST", "/api/sessions", {})
sid = session["id"]
print(f"新建会话 {sid}")

first = chat(sid, "第一句")
print(f"  第 1 句回答: {first}")

new_prompt = "你是一个不讲人话、满口文言文的学者。"
status, result = call("PATCH", f"/api/sessions/{sid}/system_prompt", {"system_prompt": new_prompt})
print(f"  PATCH -> {status} {result}")

second = chat(sid, "你是谁")
print(f"  第 2 句回答: {second}")
assert new_prompt in second, "❌ 新提示词没有生效"
print("  ✅ 新提示词已进入第 2 句请求")

status, detail = call("GET", f"/api/sessions/{sid}")
print(f"  GET 详情 system_prompt 字段: {detail['system_prompt']!r}")
assert detail["system_prompt"] == new_prompt
system_rows = [m for m in detail["messages"] if m["role"] == "system"]
print(f"  system 消息条数: {len(system_rows)}（必须为 1，不能重复插入）")
assert len(system_rows) == 1, f"❌ system 消息重复：{system_rows}"

print()
print("=" * 70)
print("场景 B：会话还没有任何消息（无 system 行）→ PATCH 走 INSERT 路径 → 再聊")
print("=" * 70)
status, session_b = call("POST", "/api/sessions", {})
sid_b = session_b["id"]
print(f"新建会话 {sid_b}（此时 message_count=0）")

insert_prompt = "你只讲冷笑话，且每句结尾加一个 emoji。"
status, result_b = call("PATCH", f"/api/sessions/{sid_b}/system_prompt", {"system_prompt": insert_prompt})
print(f"  PATCH -> {status} {result_b}")
assert result_b["created"] is True, "❌ 应该走 INSERT 路径"
assert result_b["seq"] == 0, f"❌ 期望 seq=0，实际 {result_b['seq']}"

answer_b = chat(sid_b, "讲个笑话")
print(f"  回答: {answer_b}")
assert insert_prompt in answer_b, "❌ 插入的提示词没生效"

status, detail_b = call("GET", f"/api/sessions/{sid_b}")
system_rows_b = [m for m in detail_b["messages"] if m["role"] == "system"]
print(f"  system 消息条数: {len(system_rows_b)}（必须为 1：插入的那条；loop 不能再补 config 默认）")
assert len(system_rows_b) == 1, f"❌ system 消息条数异常：{len(system_rows_b)}"
print(f"  system 内容: {system_rows_b[0]['content']!r}")
assert system_rows_b[0]["content"] == insert_prompt

print()
print("=" * 70)
print("场景 C：空提示词 / 不存在的会话")
print("=" * 70)
status, result_c = call("PATCH", f"/api/sessions/{sid}/system_prompt", {"system_prompt": "   "})
print(f"  空串 -> {status} {result_c}")
status, detail_c = call("GET", f"/api/sessions/{sid}")
print(f"  置空后 system 内容: {detail_c['system_prompt']!r}")
assert detail_c["system_prompt"] == ""

try:
    call("PATCH", "/api/sessions/nope1234/system_prompt", {"system_prompt": "x"})
    print("  ❌ 不存在的会话竟然成功了")
except urllib.error.HTTPError as exc:
    print(f"  不存在的会话 -> {exc.code} {exc.read().decode('utf-8')[:60]}")

print()
print("=" * 70)
print("场景 D：空串 + 本来就没有 system 行 → 应该什么都不做，默认提示词仍然生效")
print("=" * 70)
status, session_d = call("POST", "/api/sessions", {})
sid_d = session_d["id"]
status, result_d = call("PATCH", f"/api/sessions/{sid_d}/system_prompt", {"system_prompt": ""})
print(f"  PATCH 空串 -> {status} {result_d}")
assert result_d["created"] is False, "❌ 空串不应该插入空 system 行"
answer_d = chat(sid_d, "你好")
print(f"  回答: {answer_d}")
assert "你是配置里的默认提示词。" in answer_d, "❌ 空 system 行把配置默认挡住了"
print("  ✅ 没有插入空行，配置默认提示词照常生效")

print()
print("=" * 70)
print("数据库状态")
print("=" * 70)
import sqlite3  # noqa: E402

db = ROOT / "tools" / "data" / "integration.db"
connection = sqlite3.connect(str(db))
print("  journal_mode:", connection.execute("PRAGMA journal_mode").fetchone()[0])
rows = connection.execute(
    "SELECT session_id, seq, role, substr(content,1,28) FROM messages"
    " WHERE role='system' ORDER BY session_id, seq"
).fetchall()
for row in rows:
    print(f"  {row[0][:8]} seq={row[1]} role={row[2]} content={row[3]!r}")
connection.close()
print("\n全部通过 ✅")