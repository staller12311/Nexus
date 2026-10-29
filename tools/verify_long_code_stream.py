"""真实手测：让模型输出 100-200 行代码，观察流式帧率/耗时/完整性。"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"

PROMPT = (
    "用 Python 写一个完整的「学生成绩管理系统」，要求 120 行以上：包含 Student 类、"
    "成绩增删改查、按分数排序、统计平均分/最高分/及格率、CSV 读写，并用 docstring 与类型注解。"
    "只输出一个 ```python 代码块，不要解释。"
)


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None


session = call("POST", "/api/sessions", {})
sid = session["id"]
body = json.dumps({"session_id": sid, "message": PROMPT}, ensure_ascii=False).encode()
request = urllib.request.Request(
    f"{BASE}/api/chat/stream",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
)

start = time.perf_counter()
frames: list[tuple[float, int]] = []
answer = ""
errors: list[dict] = []
end_event = None

with urllib.request.urlopen(request, timeout=300) as response:
    for raw in response:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event["type"] == "text_delta":
            answer += event["delta"]
            frames.append(((time.perf_counter() - start) * 1000, len(event["delta"])))
        elif event["type"] == "error":
            errors.append(event)
        elif event["type"] == "message_end":
            end_event = event

total = (time.perf_counter() - start) * 1000
code_lines = len(answer.splitlines())
code_chars = len(answer)

print("=" * 76)
print("真实模型输出长代码（流式）")
print("=" * 76)
print(f"  提问：{PROMPT[:48]}…")
print(f"  text_delta 帧数：{len(frames)}")
print(f"  正文长度：{code_chars} 字 / {code_lines} 行")
print(f"  首帧到达：{frames[0][0]:.0f} ms    末帧：{frames[-1][0]:.0f} ms    总耗时：{total:.0f} ms")
gaps = [frames[i + 1][0] - frames[i][0] for i in range(len(frames) - 1)]
gaps_sorted = sorted(gaps)
print(f"  帧间隔：中位 {gaps_sorted[len(gaps_sorted) // 2]:.1f} ms，最大 {max(gaps):.1f} ms")
print(f"  error 事件：{len(errors)}")
print(f"  message_end: usage={end_event['usage'] if end_event else 'None'}")
print(f"\n  前 200 字：{answer[:200]!r}")

# 断言
assert not errors, f"流式出错：{errors}"
assert end_event is not None, "没有 message_end"
assert len(frames) > 50, "帧数太少，不像逐 token"
assert code_lines >= 100, f"代码只有 {code_lines} 行，没达到要求的 120 行"
assert answer.count('"""') >= 4, "缺少 docstring"
assert "class " in answer and "def " in answer

# 落库校验
detail = call("GET", f"/api/sessions/{sid}")
stored = [m for m in detail["messages"] if m["role"] == "assistant"][-1]["content"]
print(f"\n  落库内容与流式一致：{stored == answer}")
assert stored == answer

print("\n✅ 真实长代码流式通过：无错误、逐 token（%d 帧）、%d 行完整落库" % (len(frames), code_lines))
print(f"（测试会话 {sid}）")