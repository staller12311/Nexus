"""探测：现有自定义模型端点是否支持 POST /embeddings（不打印任何 Key）。"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import urllib.error
import urllib.request

DB = pathlib.Path(r"D:\MyAi\data\sessions.db")


def probe(label: str, base_url: str, api_key: str, model: str) -> None:
    url = base_url.rstrip("/") + "/embeddings"
    body = json.dumps({"model": model, "input": ["你好，世界"]}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") or []
        dims = len(data[0].get("embedding") or []) if data else 0
        print(f"  {label}: HTTP 200 ✅ model={payload.get('model')} 维度={dims}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        print(f"  {label}: HTTP {exc.code} ❌ {detail}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label}: 连接失败 {type(exc).__name__}: {exc}")


connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
rows = connection.execute("SELECT name, base_url, model, api_key FROM custom_models").fetchall()
connection.close()

print("=" * 70)
print("探测各端点 /embeddings 支持情况（不打印 Key）")
print("=" * 70)
for name, base_url, model, api_key in rows:
    candidate = "text-embedding-v2" if "dashscope" in base_url else model
    probe(f"{name} ({base_url}) + {candidate}", base_url, api_key, candidate)

print("\n（DashScope 官方 embedding 模型名通常是 text-embedding-v2 / text-embedding-v3）")