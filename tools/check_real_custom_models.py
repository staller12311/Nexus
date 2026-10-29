"""只读核对：真实后端里的自定义模型（持久化 / 脱敏 / 日志泄漏）。"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import urllib.request

ROOT = pathlib.Path(r"D:\MyAi")
BASE = "http://127.0.0.1:8000"


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


custom = get("/api/models/custom")["models"]
models = get("/api/models")

print("=== GET /api/models/custom（脱敏视图）===")
for item in custom:
    print(f"  {item['name']:12} base_url={item['base_url']}")
    print(f"  {'':12} model={item['model']}  masked={item['api_key_masked']}  has_key={item['has_key']}")

print("\n=== GET /api/models（合并视图）===")
for item in models:
    tag = "自定义" if item["is_custom"] else "内置"
    print(f"  {item['name']:12} [{tag}] has_key={item['has_key']} masked={item['api_key_masked']!r}")

raw = json.dumps(custom) + json.dumps(models)
print(f"\n响应体里是否出现 sk-test-123456: {'sk-test-123456' in raw}")

print("\n=== 数据库里的真实存储（只读打开）===")
db = ROOT / "data" / "sessions.db"
connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print(f"  表: {sorted(tables)}")
for name, url, model, key in connection.execute(
    "SELECT name, base_url, model, api_key FROM custom_models"
):
    print(f"  {name} | {url} | {model} | key 长度={len(key)}（库里明文，接口只给掩码）")
connection.close()

print("\n=== 日志里是否泄漏完整 Key ===")
log = ROOT / "data" / "agent.log"
if log.exists():
    content = log.read_text(encoding="utf-8", errors="replace")
    print(f"  日志大小: {len(content)} 字节")
    print(f"  含 'sk-test-123456': {'sk-test-123456' in content}")
    hits = [line for line in content.splitlines() if "mockvendor" in line]
    for line in hits[:4]:
        print(f"  相关日志: {line.strip()[:130]}")
else:
    print("  （日志文件不存在）")