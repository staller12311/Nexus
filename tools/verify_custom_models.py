"""第一层自测：custom_models 表 + 三个接口（脱敏、去尾斜杠、重名 409、删除）。"""
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
DB = ROOT / "tools" / "data" / "integration.db"
KEY = "sk-test-1234567890abcd"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


print("=" * 68)
print("① 添加自定义模型（base_url 故意带尾部斜杠）")
print("=" * 68)
# 幂等：清掉上一次跑残留的同名记录
try:
    call("DELETE", "/api/models/custom/mockvendor")
    print("  （已清理上次遗留的 mockvendor）")
except urllib.error.HTTPError:
    pass

status, created = call(
    "POST",
    "/api/models/custom",
    {
        "name": "mockvendor",
        "base_url": "http://127.0.0.1:8128/v1/",  # 尾部斜杠
        "model": "mock-model",
        "api_key": KEY,
    },
)
print(f"  POST -> {status} {created}")
assert status == 201
assert created["base_url"] == "http://127.0.0.1:8128/v1", "❌ 尾部斜杠没被去掉"
assert KEY not in json.dumps(created), "❌ 响应里出现了完整 Key"
assert created["api_key_masked"] == "sk-****abcd", created["api_key_masked"]
assert created["has_key"] is True

print("\n" + "=" * 68)
print("② GET /api/models/custom（脱敏）")
print("=" * 68)
status, listing = call("GET", "/api/models/custom")
print(f"  {status} {json.dumps(listing, ensure_ascii=False)}")
assert status == 200
names = [item["name"] for item in listing["models"]]
assert "mockvendor" in names
assert KEY not in json.dumps(listing), "❌ 列表里泄漏了完整 Key"

print("\n" + "=" * 68)
print("③ GET /api/models（合并内置 + 自定义）")
print("=" * 68)
status, models = call("GET", "/api/models")
for item in models:
    print(
        f"  {item['name']:12} is_custom={item['is_custom']!s:5} has_key={item['has_key']!s:5} "
        f"masked={item['api_key_masked']!r:14} model={item['model']}"
    )
assert any(item["is_custom"] for item in models)
assert any(not item["is_custom"] for item in models)
assert KEY not in json.dumps(models), "❌ /api/models 泄漏了完整 Key"

print("\n" + "=" * 68)
print("④ 重名与非法输入")
print("=" * 68)
for label, payload, expect in (
    ("重复 name", {"name": "mockvendor", "base_url": "http://x/v1", "model": "m", "api_key": "k123456"}, 409),
    ("与内置模型重名", {"name": "fake", "base_url": "http://x/v1", "model": "m", "api_key": "k123456"}, 409),
    ("非法 name", {"name": "有 空格", "base_url": "http://x/v1", "model": "m", "api_key": "k123456"}, 400),
    ("非 http base_url", {"name": "bad", "base_url": "ftp://x/v1", "model": "m", "api_key": "k123456"}, 400),
    ("空 Key", {"name": "nokey", "base_url": "http://x/v1", "model": "m", "api_key": "   "}, 400),
):
    try:
        call("POST", "/api/models/custom", payload)
        print(f"  ❌ {label} 竟然成功")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)["detail"]
        except (json.JSONDecodeError, KeyError, TypeError):
            detail = raw[:80]
        print(f"  {label:14} -> {exc.code} {detail[:52]}")
        assert exc.code == expect, f"期望 {expect}，实际 {exc.code}"

print("\n" + "=" * 68)
print("⑤ 数据库里的真实存储")
print("=" * 68)
connection = sqlite3.connect(str(DB))
rows = connection.execute(
    "SELECT name, base_url, model, api_key FROM custom_models"
).fetchall()
columns = [row[1] for row in connection.execute("PRAGMA table_info(custom_models)")]
connection.close()
print(f"  字段: {columns}")
for name, url, model, key in rows:
    print(f"  {name} | {url} | {model} | key 长度={len(key)}（库里是完整明文，接口只给掩码）")

print("\n" + "=" * 68)
print("⑥ 删除")
print("=" * 68)
status, deleted = call("DELETE", "/api/models/custom/mockvendor")
print(f"  DELETE -> {status} {deleted}")
assert deleted["deleted"] is True
status, listing = call("GET", "/api/models/custom")
assert "mockvendor" not in [item["name"] for item in listing["models"]]
print(f"  删除后列表: {[item['name'] for item in listing['models']]}")

try:
    call("DELETE", "/api/models/custom/mockvendor")
    print("  ❌ 重复删除竟然成功")
except urllib.error.HTTPError as exc:
    print(f"  重复删除 -> {exc.code}（预期 404）")

print("\n第一层全部通过 ✅")