"""第二层自测：自定义模型是否真的用「库里的 base_url + Key」发请求（且不泄漏 Key）。"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8010"
FAKE = "http://127.0.0.1:8128"
LOG = ROOT / "tools" / "data" / "integration.log"
KEY = "sk-test-1234567890abcd"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return response.status, (json.loads(raw.decode("utf-8")) if raw else None)


def fake_get(path: str):
    with urllib.request.urlopen(f"{FAKE}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def control(mode: str) -> None:
    body = json.dumps({"mode": mode}).encode()
    request = urllib.request.Request(
        f"{FAKE}/control", data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).read()


def chat(session_id: str, message: str) -> tuple[str, str | None]:
    """返回 (拼好的文本, 错误信息)。"""
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    text, error = "", None
    with urllib.request.urlopen(request, timeout=90) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "text_delta":
                text += event["delta"]
            elif event["type"] == "error":
                error = event["message"]
    return text, error


# ---------------------------------------------------------------- 准备
try:
    call("DELETE", "/api/models/custom/mockvendor")
except urllib.error.HTTPError:
    pass
control("prompt")

print("=" * 70)
print("① 添加自定义模型并建会话")
print("=" * 70)
status, model = call(
    "POST",
    "/api/models/custom",
    {
        "name": "mockvendor",
        "base_url": "http://127.0.0.1:8128/v1/",  # 带尾斜杠，应被去掉
        "model": "mock-model",
        "api_key": KEY,
    },
)
print(f"  添加 -> {status} base_url={model['base_url']} masked={model['api_key_masked']}")
status, session = call("POST", "/api/sessions", {"model": "mockvendor"})
sid = session["id"]
print(f"  用自定义模型建会话 -> {status} model={session['model']} id={sid[:8]}")

print("\n" + "=" * 70)
print("② 发一句话：后端是否真的用库里的 base_url + Key 打上游")
print("=" * 70)
text, error = chat(sid, "你好")
print(f"  回答: {text!r}")
print(f"  错误: {error}")
last = fake_get("/last")
print(f"  假上游收到: path={last.get('path')}")
print(f"             authorization={str(last.get('authorization'))[:24]}…")
assert last.get("path", "").endswith("/chat/completions"), f"❌ 上游没收到 chat 请求：{last}"
request_model = last.get("model")
print(f"             请求体里的 model: {request_model!r}")
assert error is None, f"不该有错误：{error}"
assert last["authorization"] == f"Bearer {KEY}", "❌ 上游收到的 Key 不是库里那个"
assert request_model == "mock-model", "❌ 上游收到的 model id 不对"
print("  ✅ 用的就是自定义模型的 base_url / model / api_key")

print("\n" + "=" * 70)
print("③ 假地址应当优雅失败（不崩、不回显 Key）")
print("=" * 70)
try:
    call("DELETE", "/api/models/custom/deadvendor")
except urllib.error.HTTPError:
    pass
call(
    "POST",
    "/api/models/custom",
    {
        "name": "deadvendor",
        "base_url": "http://127.0.0.1:9/v1",  # 没人监听
        "model": "dead-model",
        "api_key": "sk-dead-9999999999",
    },
)
status, dead_session = call("POST", "/api/sessions", {"model": "deadvendor"})
text, error = chat(dead_session["id"], "在吗")
print(f"  回答: {text!r}")
print(f"  错误: {error}")
assert error is not None, "❌ 期望收到 error 事件"
assert "sk-dead-9999999999" not in (error + text), "❌ 错误信息里回显了 Key"
print("  ✅ 错误被正常上报，且没有泄漏 Key")

print("\n" + "=" * 70)
print("④ Provider.describe() 不含密钥")
print("=" * 70)
from agent.config import load_config  # noqa: E402
from backend.service import AgentService  # noqa: E402

service = AgentService.load(str(ROOT / "tools" / "config.integration.yaml"))
provider = service._provider_for("mockvendor")
described = provider.describe()
print(f"  describe(): {described}")
assert KEY not in described, "❌ describe() 泄漏了 Key"
assert "mock-model" in described

print("\n" + "=" * 70)
print("⑤ 删除模型后，旧会话再发消息应给出清晰错误")
print("=" * 70)
call("DELETE", "/api/models/custom/mockvendor")
text, error = chat(sid, "还能用吗")
print(f"  错误: {str(error)[:70]}")
assert error and "mockvendor" in error

print("\n" + "=" * 70)
print("⑥ 日志里有没有完整 Key")
print("=" * 70)
if LOG.exists():
    content = LOG.read_text(encoding="utf-8", errors="replace")
    leaked = KEY in content
    print(f"  日志大小: {len(content)} 字节")
    print(f"  含完整 Key: {leaked}")
    for line in content.splitlines():
        if "mockvendor" in line:
            print(f"  相关日志: {line.strip()[:120]}")
    assert not leaked, "❌ 日志里出现了完整 Key"
else:
    print(f"  （日志文件不存在：{LOG}）")

print("\n第二层全部通过 ✅")