"""验证知识库：上传 → 后台解析 → BM25 检索 → 工具输出 → 删除。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8010"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
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


DOC = """# 内部报销制度

## 差旅费
员工出差乘坐高铁一律选择二等座，单程超过 6 小时的可选一等座。
住宿标准：一线城市每晚不超过 600 元，其他城市不超过 400 元。
报销需在出差结束后 15 个工作日内提交，逾期不予受理。

## 餐饮
出差期间每人每天餐补 120 元，不需要发票，按天计算。

## 打车
夜间 22 点后（含）从机场/车站到酒店的打车费可全额报销，需提供行程单。

## 审批流程
金额不超过 3000 元由部门经理审批；超过 3000 元需财务总监审批。
"""

print("=" * 72)
print("① GET /api/kb/status（检索管线与支持格式）")
print("=" * 72)
status, info = call("GET", "/api/kb/status")
print(f"  docs={info['docs']} chunks={info['chunks']} mode={info['mode']}")
print(f"  pipeline: {info['pipeline']}")
print(f"  支持格式: {info['supported']}  单文件上限: {info['max_file_mb']}MB")

print("\n" + "=" * 72)
print("② 上传 .txt → 后台解析 → 轮询到 ready")
print("=" * 72)
document = upload("报销制度.txt", DOC.encode("utf-8"))
doc_id = document["id"]
print(f"  上传返回: id={doc_id} status={document['status']}")
deadline = time.time() + 20
while time.time() < deadline:
    _, listing = call("GET", "/api/kb/docs")
    current = next((item for item in listing["docs"] if item["id"] == doc_id), None)
    if current and current["status"] in ("ready", "failed"):
        break
    time.sleep(0.2)
print(f"  最终状态: {current['status']} pages={current['pages']} chunks={current['chunk_count']} mode={current['mode']}")
if current["status"] == "failed":
    print(f"  错误: {current['error']}")
    sys.exit(1)
assert current["status"] == "ready" and current["chunk_count"] > 0

print("\n" + "=" * 72)
print("③ 工具检索：search_knowledge_base('住宿标准 一线城市')")
print("=" * 72)
sys.path.insert(0, r"D:\MyAi")
from backend.service.knowledge import KnowledgeService  # noqa: E402

knowledge = KnowledgeService(r"D:\MyAi\tools\data\integration.db")
context = knowledge.search_as_context("住宿标准 一线城市 每晚多少钱")
print("  " + context.replace("\n", "\n  ")[:420])
assert "600" in context, "❌ 没有检索到住宿标准"
assert "报销制度.txt" in context, "❌ 没有带来源文件名"

print("\n" + "=" * 72)
print("④ 无关问题：应返回空结果而不是报错")
print("=" * 72)
empty = knowledge.search_as_context("量子色动力学的渐近自由")
print(f"  {empty}")
assert "没有找到" in empty

print("\n" + "=" * 72)
print("⑤ 删除文档 → 切片级联删除")
print("=" * 72)
status, deleted = call("DELETE", f"/api/kb/docs/{doc_id}")
print(f"  删除: {deleted['name']} ({deleted['chunk_count']} 片)")
_, listing = call("GET", "/api/kb/docs")
print(f"  剩余文档数: {len(listing['docs'])}")
assert all(item["id"] != doc_id for item in listing["docs"])

print("\n全部通过 ✅")