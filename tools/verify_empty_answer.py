"""验证「模型只思考不输出正文」的兜底：不抛错、不落非法 assistant 消息、给用户人话提示。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.loop import AgentLoop  # noqa: E402
from agent.registry import ToolRegistry  # noqa: E402
from agent.storage import Storage  # noqa: E402


class ReasoningOnlyProvider:
    """复刻线上情况：只吐 reasoning，正文一个字都没有，finish_reason=length。"""

    def __init__(self, finish_reason: str | None = "length") -> None:
        self.finish_reason = finish_reason
        self.model_name = "fake-reasoner"

    def chat_stream(self, messages, tools=None, **kwargs):
        yield {"type": "reasoning", "text": "让我想想…" * 200}
        yield {"type": "reasoning", "text": "（思考被 max_tokens 截断）"}
        yield {
            "type": "done",
            "content": None,
            "reasoning": "让我想想…",
            "tool_calls": (),
            "usage": type("U", (), {"prompt_tokens": 100, "completion_tokens": 4096, "total_tokens": 4196})(),
            "finish_reason": self.finish_reason,
        }

    def chat(self, messages, tools=None, **kwargs):  # run() 兼容用
        raise AssertionError("不应该走非流式路径")


def build_loop(provider, storage, session_id) -> AgentLoop:
    return AgentLoop(
        provider,
        ToolRegistry(),
        system_prompt="你是助手",
        max_tool_rounds=3,
        max_history_messages=40,
        storage=storage,
        session_id=session_id,
    )


print("=" * 76)
print("① finish_reason=length：给出「思考用完预算」的人话提示，并正常落库")
print("=" * 76)
db = Path(tempfile.mkdtemp()) / "reason.db"
storage = Storage(db)
session = storage.create_session("fake-reasoner")
loop = build_loop(ReasoningOnlyProvider("length"), storage, session.id)

events = list(loop.run_stream("写一个 200 行的 Python 脚本"))
kinds = [e["type"] for e in events]
text = "".join(e["text"] for e in events if e["type"] == "text")
done = next(e for e in events if e["type"] == "done")
print(f"  事件类型: {kinds}")
print(f"  正文提示: {text}")
print(f"  LoopResult: content={done['result'].content!r} finish={done['result'].finish}")

assert "思考阶段就用完了 max_tokens" in text
assert "8192" not in text  # 不该把配置细节塞给用户
assert done["result"].content == text

history = storage.get_messages(session.id, with_reasoning=True)
print(f"  落库: {[(m['role'], len(m.get('content') or '')) for m in history]}")
roles = [m["role"] for m in history]
assert roles == ["system", "user", "assistant"], f"❌ 落库形状不对：{roles}"
assistant = history[-1]
assert assistant.get("content"), "❌ assistant 仍然没有 content"
assert assistant.get("reasoning_content"), "❌ 思考过程没落库"

print("\n" + "=" * 76)
print("② 非 length（模型就是没说话）：给通用提示，同样不抛错")
print("=" * 76)
db2 = Path(tempfile.mkdtemp()) / "empty.db"
storage2 = Storage(db2)
session2 = storage2.create_session("fake-reasoner")
loop2 = build_loop(ReasoningOnlyProvider(None), storage2, session2.id)
events2 = list(loop2.run_stream("在吗？"))
text2 = "".join(e["text"] for e in events2 if e["type"] == "text")
print(f"  提示: {text2}")
assert "没有返回正文" in text2
assert [m["role"] for m in storage2.get_messages(session2.id)] == ["system", "user", "assistant"]

print("\n" + "=" * 76)
print("③ 正常回答不受影响（有正文时绝不插提示）")
print("=" * 76)


class NormalProvider:
    model_name = "fake"

    def chat_stream(self, messages, tools=None, **kwargs):
        yield {"type": "text", "text": "正常回答"}
        yield {
            "type": "done",
            "content": "正常回答",
            "reasoning": None,
            "tool_calls": (),
            "usage": type("U", (), {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})(),
            "finish_reason": "stop",
        }


db3 = Path(tempfile.mkdtemp()) / "ok.db"
storage3 = Storage(db3)
session3 = storage3.create_session("fake")
loop3 = build_loop(NormalProvider(), storage3, session3.id)
events3 = list(loop3.run_stream("你好"))
text3 = "".join(e["text"] for e in events3 if e["type"] == "text")
print(f"  输出: {text3!r}")
assert text3 == "正常回答"

print("\n" + "=" * 76)
print("④ 手写空 assistant 仍被 storage 拦住（校验没有被削弱）")
print("=" * 76)
try:
    storage3.append_message(session3.id, {"role": "assistant", "content": None})
    print("  ❌ 非法消息竟然入库了")
    raise SystemExit(1)
except Exception as exc:  # noqa: BLE001
    print(f"  仍然拒绝: {exc}")

print("\n全部通过 ✅ 空回答不再 500，历史合法，用户能看到原因")