"""把真实 dashscope 模型列表导出成 JSON，供前端过滤函数的真机验证使用。"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import urllib.request

ROOT = pathlib.Path(r"D:\MyAi")
DB = ROOT / "data" / "sessions.db"
OUT = ROOT / "tools" / "_real_models.json"

connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
row = connection.execute(
    "SELECT base_url, api_key FROM custom_models WHERE base_url LIKE '%dashscope%' LIMIT 1"
).fetchone()
connection.close()
if row is None:
    print("库里没有 dashscope 凭据")
    sys.exit(1)
base_url, api_key = row

body = json.dumps({"base_url": base_url, "api_key": api_key}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8000/api/models/fetch",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=40) as response:
    data = json.loads(response.read().decode("utf-8"))

OUT.write_text(json.dumps(data["models"], ensure_ascii=False), encoding="utf-8")
print(f"已导出 {data['count']} 个模型到 {OUT.name}（不含任何 Key）")