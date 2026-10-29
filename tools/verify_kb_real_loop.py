"""真实闭环：上传 README.md → 直接问「这个文件里写了啥」→ 模型必须自动调 search_knowledge_base。

走的是浏览器同一条路径（POST /api/kb/docs + POST /api/chat/stream），用真实模型。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def upload(filename: str, data: bytes) -> dict:
    boundary = "----kb" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{BASE}/api/kb/docs",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def stream(session_id: str, message: str) -> list[dict]:
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


MD = """# 项目 README：排班系统

## 项目简介
这是一个面向连锁门店的排班系统，支持多门店、多班次的自动排班。

## 技术栈
后端使用 Python 3.11 + FastAPI，数据库用 SQLite，前端是 React 18 + TypeScript。

## 核心功能
1. 员工管理：录入员工、技能标签、可用时间段。
2. 自动排班：根据客流预测和员工可用性生成班表。
3. 冲突检测：同一员工同一时段不能被排两个班次。
4. 导出：班表可导出为 Excel。

## 部署方式
使用 Docker Compose 一键部署，默认监听 8000 端口。数据库文件挂在 /data 目录下。

## 已知限制
暂不支持跨门店调班；排班算法为贪心策略，不保证全局最优。
"""

print("=" * 74)
print("① 后端已识别 PDF/Word（装完依赖后 supported 应变全）")
print("=" * 74)
_, status = call("GET", "/api/kb/status")
print(f"  supported = {status['supported']}")
print(f"  mode      = {status['mode']}  pipeline = {status['pipeline']}")
assert ".pdf" in status["supported"] and ".docx" in status["supported"], "❌ 没识别到 pypdf/python-docx"

print("\n" + "=" * 74)
print("② 上传 README.md（走和浏览器相同的接口）")
print("=" * 74)
_, listing = call("GET", "/api/kb/docs")
for item in listing["docs"]:
    call("DELETE", f"/api/kb/docs/{item['id']}")
document = upload("README.md", MD.encode("utf-8"))
deadline = time.time() + 20
while time.time() < deadline:
    _, listing = call("GET", "/api/kb/docs")
    current = next((d for d in listing["docs"] if d["id"] == document["id"]), None)
    if current and current["status"] in ("ready", "failed"):
        break
    time.sleep(0.2)
print(f"  {current['name']}: status={current['status']} chunks={current['chunk_count']}")

print("\n" + "=" * 74)
print("③ 新会话直接问「这个文件里写了啥」→ 模型应自动调用 search_knowledge_base")
print("=" * 74)
_, session = call("POST", "/api/sessions", {})
sid = session["id"]
start = time.perf_counter()
events = stream(sid, "这个文件里写了啥？简单说说它是干什么的。")
elapsed = (time.perf_counter() - start) * 1000

tool_calls = [e for e in events if e["type"] == "tool_call_start"]
tool_ends = [e for e in events if e["type"] == "tool_call_end"]
answer = "".join(e["delta"] for e in events if e["type"] == "text_delta")
reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning_delta")

print(f"  耗时 {elapsed:.0f} ms，事件数 {len(events)}")
print(f"  模型调用的工具: {[t['name'] for t in tool_calls]}")
for start_event in tool_calls:
    print(f"    - {start_event['name']} 参数={start_event['arguments']}")
print(f"  思考 {len(reasoning)} 字，回答 {len(answer)} 字")
print(f"  回答: {answer[:300]}")

called_kb = any(t["name"] == "search_knowledge_base" for t in tool_calls)
print(f"\n  ✅ 调用了 search_knowledge_base: {called_kb}")
assert called_kb, f"❌ 模型没有调用知识库工具，实际调用了 {[t['name'] for t in tool_calls]}"
assert tool_ends and tool_ends[0]["ok"] is True

# 回答里应体现 README 的真实内容（排班系统 / FastAPI / React 等）
keywords = [word for word in ("排班", "FastAPI", "React", "SQLite", "Docker") if word.lower() in answer.lower()]
print(f"  回答中命中的关键信息: {keywords}")
assert keywords, "❌ 回答没有用上知识库内容"

print("\n" + "=" * 74)
print("④ 落库检查：工具卡片与回答都在会话里")
print("=" * 74)
_, detail = call("GET", f"/api/sessions/{sid}")
tools_in_db = [m for m in detail["messages"] if m.get("tool_calls")]
print(f"  会话消息数: {len(detail['messages'])}，带工具调用的消息: {len(tools_in_db)}")
if tools_in_db:
    print(f"  工具名: {[c['function']['name'] for c in tools_in_db[0]['tool_calls']]}")

print("\n全部通过 ✅ 真实闭环：上传 md → 提问 → 自动检索 → 基于文档回答")
print(f"\n（测试会话 id={sid}，可 DELETE /api/sessions/{sid} 清理）")