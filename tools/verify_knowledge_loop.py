"""闭环验证：上传文档 → 提问 → 模型主动调用 search_knowledge_base → 基于检索结果回答。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def control(mode: str) -> None:
    body = json.dumps({"mode": mode}).encode()
    request = urllib.request.Request(
        f"{FAKE}/control", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


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
    with urllib.request.urlopen(request, timeout=120) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


DOC = """# 内部报销制度

## 差旅费
住宿标准：一线城市每晚不超过 600 元，其他城市不超过 400 元。
报销需在出差结束后 15 个工作日内提交。

## 餐饮
出差期间每人每天餐补 120 元。
"""

print("=" * 72)
print("① 准备：清理旧文档 + 上传新文档")
print("=" * 72)
_, listing = call("GET", "/api/kb/docs")
for item in listing["docs"]:
    call("DELETE", f"/api/kb/docs/{item['id']}")
    print(f"  清理旧文档: {item['name']}")

document = upload("报销制度.txt", DOC.encode("utf-8"))
doc_id = document["id"]
deadline = time.time() + 20
while time.time() < deadline:
    _, current_list = call("GET", "/api/kb/docs")
    current = next(item for item in current_list["docs"] if item["id"] == doc_id)
    if current["status"] in ("ready", "failed"):
        break
    time.sleep(0.2)
print(f"  上传 {current['name']}: status={current['status']} chunks={current['chunk_count']}")

print("\n" + "=" * 72)
print("② 提问 → 模型调用工具 → 回答")
print("=" * 72)
control("kb")
_, session = call("POST", "/api/sessions", {})
sid = session["id"]
events = stream(sid, "一线城市出差住宿每晚能报多少钱？")

tool_start = next((e for e in events if e["type"] == "tool_call_start"), None)
tool_end = next((e for e in events if e["type"] == "tool_call_end"), None)
answer = "".join(e["delta"] for e in events if e["type"] == "text_delta")
print(f"  工具: {tool_start['name']} 参数={tool_start['arguments']}")
print(f"  工具结果片段: {tool_end['content'][:80]!r}…")
print(f"  最终回答: {answer[:120]!r}")

assert tool_start["name"] == "search_knowledge_base", "❌ 没有调用知识库工具"
assert json.loads(tool_start["arguments"])["query"], "❌ 查询参数为空"
assert tool_end["ok"] is True
assert "600" in tool_end["content"], "❌ 检索结果里没有 600"
assert "600" in answer, "❌ 回答没有用上检索到的数字"
assert "报销制度.txt" in tool_end["content"], "❌ 工具结果没带来源"

print("\n" + "=" * 72)
print("③ 工具已注册进 registry（模型能看到它的 schema）")
print("=" * 72)
sys.path.insert(0, r"D:\MyAi")
from agent.config import load_config  # noqa: E402
from backend.service.agent_service import AgentService  # noqa: E402

service = AgentService(load_config(r"D:\MyAi\tools\config.integration.yaml"))
names = [schema["function"]["name"] for schema in service.registry.schemas()]
print(f"  可用工具: {names}")
assert "search_knowledge_base" in names

print("\n全部通过 ✅ 闭环成立：上传 → 检索 → 基于文档回答")