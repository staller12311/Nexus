"""验证 Provider.chat_stream()：增量文本 / reasoning / 工具调用分片拼接 / usage / 重试。

用假 client 返回构造好的 chunk，不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as Obj

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.config import ModelConfig  # noqa: E402
from agent.provider import Provider, ProviderAuthError, ProviderServerError  # noqa: E402


def delta(content=None, reasoning=None, tool_calls=None) -> Obj:
    return Obj(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def chunk(d=None, usage=None) -> Obj:
    return Obj(choices=[Obj(delta=d)] if d is not None else [], usage=usage)


def tool_frag(index, call_id=None, name=None, arguments=None) -> Obj:
    function = Obj(name=name, arguments=arguments)
    return Obj(index=index, id=call_id, function=function)


class FakeStream:
    def __init__(self, chunks, raise_after=None):
        self._chunks = chunks
        self._raise_after = raise_after
        self.closed = False

    def __iter__(self):
        for position, item in enumerate(self._chunks):
            if self._raise_after is not None and position == self._raise_after:
                raise RuntimeError("流中途断开")
            yield item

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, stream, error=None):
        self._stream = stream
        self._error = error
        self.payloads: list[dict] = []
        self.chat = Obj(completions=Obj(create=self._create))

    def _create(self, **payload):
        self.payloads.append(payload)
        if self._error is not None:
            raise self._error
        return self._stream


def make_provider(client) -> Provider:
    config = ModelConfig(
        base_url="http://127.0.0.1:9/v1", model="mock-model", api_key_env="MOCK_KEY", max_tokens=64
    )
    return Provider(config, request_timeout=5, max_retries=1, client=client, sleeper=lambda _: None)


print("=" * 70)
print("① 增量文本 + reasoning + usage")
print("=" * 70)
chunks = [
    chunk(delta(reasoning="先想想…")),
    chunk(delta(reasoning=" 9.11 更大")),
    chunk(delta(content="9.11")),
    chunk(delta(content=" 比 9.9 大")),
    chunk(usage=Obj(prompt_tokens=11, completion_tokens=7, total_tokens=18)),
]
client = FakeClient(FakeStream(chunks))
provider = make_provider(client)

events = list(provider.chat_stream([{"role": "user", "content": "9.11 和 9.9 哪个大"}]))
for event in events:
    if event["type"] != "done":
        print(f"  {event['type']:9} {event['text']!r}")
done = events[-1]
print(f"  done: content={done['content']!r} reasoning={done['reasoning']!r} usage={done['usage']}")

assert client.payloads[0]["stream"] is True, "❌ 请求没有带 stream=True"
assert client.payloads[0]["stream_options"] == {"include_usage": True}
assert [e["text"] for e in events if e["type"] == "text"] == ["9.11", " 比 9.9 大"]
assert done["content"] == "9.11 比 9.9 大"
assert done["reasoning"] == "先想想… 9.11 更大"
assert done["usage"].total_tokens == 18
assert done["tool_calls"] == ()

print("\n" + "=" * 70)
print("② 工具调用分片拼接（两条并行、参数被切成 4 段、交错到达）")
print("=" * 70)
chunks2 = [
    chunk(delta(tool_calls=[tool_frag(0, call_id="call_a", name="read_file", arguments='{"pa')])),
    chunk(delta(tool_calls=[tool_frag(1, call_id="call_b", name="get_time", arguments='{"zo')])),
    chunk(delta(tool_calls=[tool_frag(0, arguments='th": "read')])),
    chunk(delta(tool_calls=[tool_frag(1, arguments='ne": "UTC"}')])),
    chunk(delta(tool_calls=[tool_frag(0, arguments='me.txt"}')])),
    chunk(usage=Obj(prompt_tokens=3, completion_tokens=4, total_tokens=7)),
]
client2 = FakeClient(FakeStream(chunks2))
provider2 = make_provider(client2)
events2 = list(provider2.chat_stream([{"role": "user", "content": "读文件并看时间"}]))
done2 = events2[-1]
print(f"  content={done2['content']!r}（纯工具调用时为 None）")
for call in done2["tool_calls"]:
    print(f"  {call.id}  {call.name}  arguments={call.arguments!r}")
assert done2["content"] is None
assert len(done2["tool_calls"]) == 2
assert done2["tool_calls"][0].id == "call_a"
assert done2["tool_calls"][0].name == "read_file"
assert done2["tool_calls"][0].arguments == '{"path": "readme.txt"}'
assert done2["tool_calls"][1].name == "get_time"
assert done2["tool_calls"][1].arguments == '{"zone": "UTC"}'
print("  ✅ 分片拼成了完整 JSON 字符串（且没有对碎片做 json.loads）")

print("\n" + "=" * 70)
print("③ 只有一个分片带 id/name，后续只带 arguments（常见形态）")
print("=" * 70)
chunks3 = [
    chunk(delta(tool_calls=[Obj(index=0, id="call_x", function=Obj(name="list_dir", arguments=""))])),
    chunk(delta(tool_calls=[Obj(index=0, id=None, function=Obj(name=None, arguments='{"path":'))])),
    chunk(delta(tool_calls=[Obj(index=0, id=None, function=Obj(name=None, arguments=' "."}'))])),
]
client3 = FakeClient(FakeStream(chunks3))
done3 = list(make_provider(client3).chat_stream([{"role": "user", "content": "ls"}]))[-1]
print(f"  {done3['tool_calls'][0]}")
assert done3["tool_calls"][0].arguments == '{"path": "."}'

print("\n" + "=" * 70)
print("④ 401 不重试；5xx 重试后成功（只重试建连阶段）")
print("=" * 70)
client_auth = FakeClient(FakeStream([]), error=ProviderAuthError("密钥无效", status_code=401))
try:
    list(make_provider(client_auth).chat_stream([{"role": "user", "content": "x"}]))
    print("  ❌ 401 竟然通过了")
except ProviderAuthError as exc:
    print(f"  401 -> {type(exc).__name__}: {exc}（调用次数={len(client_auth.payloads)}）")
    assert len(client_auth.payloads) == 1


class FlakyClient(FakeClient):
    def _create(self, **payload):
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            raise ProviderServerError("boom", status_code=500)
        return self._stream


flaky = FlakyClient(FakeStream([chunk(delta(content="ok"))]))
events4 = list(make_provider(flaky).chat_stream([{"role": "user", "content": "x"}]))
print(f"  5xx 重试 -> 调用 {len(flaky.payloads)} 次，结果 {events4[-1]['content']!r}")
assert len(flaky.payloads) == 2
assert events4[-1]["content"] == "ok"

print("\n全部通过 ✅")