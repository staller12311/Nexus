"""持久化验证：加一个自定义模型 → 重启后端进程 → 模型还在 → 删除。

需要先手动停掉 8010 再重启（脚本只负责前后两次读取与写操作）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8010"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


action = sys.argv[1] if len(sys.argv) > 1 else "check"

if action == "add":
    try:
        call("DELETE", "/api/models/custom/persistme")
    except urllib.error.HTTPError:
        pass
    status, created = call(
        "POST",
        "/api/models/custom",
        {
            "name": "persistme",
            "base_url": "http://127.0.0.1:8128/v1",
            "model": "persist-model",
            "api_key": "sk-persist-000111222333",
        },
    )
    print(f"添加 -> {status} {created['name']} masked={created['api_key_masked']}")
elif action == "check":
    status, listing = call("GET", "/api/models/custom")
    names = [item["name"] for item in listing["models"]]
    print(f"重启后 /api/models/custom -> {status} 名字: {names}")
    ok = "persistme" in names
    item = next((x for x in listing["models"] if x["name"] == "persistme"), None)
    if item:
        print(f"  base_url={item['base_url']}  model={item['model']}  masked={item['api_key_masked']}")
        print(f"  完整 Key 是否泄漏: {'sk-persist-000111222333' in json.dumps(listing)}")
    print("持久化:", "✅ 还在" if ok else "❌ 丢了")
    sys.exit(0 if ok else 1)
elif action == "clean":
    status, result = call("DELETE", "/api/models/custom/persistme")
    print(f"清理 -> {status} {result}")