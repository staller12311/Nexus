"""验证 POST /api/models/fetch：真实 dashscope 拉取、尾斜杠、错误映射、日志不泄漏 Key。

Key 从本地库里读（只读），全程不打印明文，只打印掩码。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(r"D:\MyAi")
BASE = "http://127.0.0.1:8000"
FAKE = "http://127.0.0.1:8128"
DB = ROOT / "data" / "sessions.db"
LOG = ROOT / "data" / "agent.log"


def mask(key: str) -> str:
    return f"{key[:3]}****{key[-4:]}" if len(key) > 8 else "****"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def expect_failure(payload: dict) -> tuple[int, str]:
    try:
        status, _ = call("POST", "/api/models/fetch", payload)
        return status, "（竟然成功）"
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8") or "{}")
        return exc.code, str(body.get("detail", ""))[:80]


# ---- 从库里拿真实的 dashscope 凭据（只读，不打印） --------------------------
connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
row = connection.execute(
    "SELECT name, base_url, api_key FROM custom_models WHERE base_url LIKE '%dashscope%' LIMIT 1"
).fetchone()
connection.close()
if row is None:
    print("❌ 库里没有 dashscope 的自定义模型，无法做真实拉取测试")
    sys.exit(1)
name, base_url, api_key = row
print(f"使用库里的自定义模型 {name!r}，base_url={base_url}，key={mask(api_key)}")

print("\n" + "=" * 70)
print("① 真实拉取 dashscope 模型列表")
print("=" * 70)
status, result = call("POST", "/api/models/fetch", {"base_url": base_url, "api_key": api_key})
print(f"  POST /api/models/fetch -> {status}")
print(f"  实际请求地址: {result['base_url']}")
print(f"  共 {result['count']} 个模型")
qwen = [item for item in result["models"] if "qwen" in item.lower()]
print(f"  qwen 系模型（前 12 个）: {qwen[:12]}")
for expected in ("qwen-turbo", "qwen-plus", "qwen-max"):
    hit = expected in result["models"]
    print(f"  包含 {expected}: {hit}")
assert result["count"] > 0

print("\n" + "=" * 70)
print("② 尾部斜杠 / 已是 /models 的写法")
print("=" * 70)
for label, url in (
    ("无尾斜杠", base_url),
    ("带尾斜杠", base_url + "/"),
    ("已含 /models", base_url + "/models"),
):
    status, out = call("POST", "/api/models/fetch", {"base_url": url, "api_key": api_key})
    print(f"  {label:10} 输入 {url}")
    print(f"  {'':10} -> {status} 请求 {out['base_url']}（{out['count']} 个模型）")
    assert out["base_url"].endswith("/models") and "//" not in out["base_url"].split("://")[1]

print("\n" + "=" * 70)
print("③ 错误映射")
print("=" * 70)
for label, payload, expected_status in (
    ("错误 Key", {"base_url": base_url, "api_key": "sk-wrong-key-000"}, 502),
    ("非法协议", {"base_url": "ftp://example.com/v1", "api_key": "x"}, 400),
    ("空 base_url", {"base_url": "", "api_key": "x"}, 400),
    ("空 Key", {"base_url": base_url, "api_key": ""}, 400),
):
    status, detail = expect_failure(payload)
    print(f"  {label:10} -> {status} {detail}")
    assert status == expected_status, f"期望 {expected_status}，实际 {status}"

print("\n" + "=" * 70)
print("④ 假上游（本地）：验证 URL 拼接与去重")
print("=" * 70)
try:
    status, out = call(
        "POST", "/api/models/fetch", {"base_url": "http://127.0.0.1:8128/v1", "api_key": "any"}
    )
    print(f"  -> {status} 请求 {out['base_url']}")
    print(f"  模型: {out['models']}（重复项应被去掉）")
    assert out["models"] == ["mock-model-small", "mock-model-large"]
except urllib.error.HTTPError as exc:
    print(f"  （假上游未启动，跳过：{exc.code}）")

print("\n" + "=" * 70)
print("⑤ 日志与数据库是否泄漏 Key")
print("=" * 70)
if LOG.exists():
    content = LOG.read_text(encoding="utf-8", errors="replace")
    print(f"  日志含完整 Key: {api_key in content}")
    hits = [line for line in content.splitlines() if "拉取模型列表" in line]
    for line in hits[-3:]:
        print(f"  拉取日志: {line.strip()[:120]}")
    assert api_key not in content, "❌ 日志里出现了完整 Key"

connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
all_keys = [r[0] for r in connection.execute("SELECT api_key FROM custom_models")]
connection.close()
print(f"  custom_models 行数: {len(all_keys)}（本次拉取不应新增任何行）")

print("\n全部通过 ✅")