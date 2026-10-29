"""验收标准第 3 条：用标准库 http.server 起**真实端点**做主验证。

这里不 mock HTTP：真的监听 127.0.0.1 的临时端口，把 provider 的 ``base_url`` 指过去，
由真实的 openai SDK + httpx2 发出请求、解析响应。覆盖：

- 完整多轮对话，并解析真实响应里的 ``usage``；
- 工具调用（function calling）响应的解析，以及回灌 tool 消息后拿到最终回答；
- 真实 401/403 一次即失败（不重试）、429/5xx 退避后重试成功。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

import pytest

from agent.config import ModelConfig, build_config
from agent.loop import AgentLoop
from agent.provider import (
    Provider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderServerError,
    Usage,
)
from agent.registry import build_registry
from agent.storage import Storage

TEST_API_KEY_ENV = "MOCK_SERVER_API_KEY"
FAKE_KEY = "sk-mock-1234567890"

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取工作区内的文本文件",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def chat_completion(
    content: str | None = None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    model: str = "mock-model",
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """一条完整的 OpenAI chat.completion 响应体（含 usage）。"""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 1712345678,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason, "logprobs": None}
        ],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "system_fingerprint": None,
    }


def error_body(message: str, error_type: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": None, "param": None}}


class _ServerRuntime:
    """按脚本依次返回响应；无脚本时用 default，两者都没有则 500。

    ``delay`` 用于让请求「停在半路」，好让测试在请求进行中打断客户端。
    """

    def __init__(
        self,
        script: Iterable[dict[str, Any]] = (),
        default: dict[str, Any] | None = None,
        delay: float = 0.0,
    ):
        self._script = list(script)
        self._default = default
        self.delay = float(delay)
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []

    def record(self, path: str, headers: dict[str, str], body: Any) -> None:
        with self._lock:
            # 头部名统一小写，避免不同客户端大小写差异导致断言脆弱
            self.requests.append(
                {
                    "path": path,
                    "headers": {key.lower(): value for key, value in headers.items()},
                    "body": body,
                }
            )

    def next_response(self) -> tuple[int, dict[str, Any], dict[str, str]]:
        with self._lock:
            if self._script:
                step = self._script.pop(0)
            elif self._default is not None:
                step = self._default
            else:
                step = {"status": 500, "json": error_body("mock server script exhausted")}
        if "status" not in step and "json" not in step:
            step = {"status": 200, "json": step}  # 直接写 completion 体的简写形式
        return (
            int(step.get("status", 200)),
            step.get("json", {}),
            step.get("headers", {}) or {},
        )


def _sse_frames(payload: dict[str, Any], *, chunk_size: int = 2) -> bytes:
    """把一条完整 completion 拆成 SSE 增量帧（供流式路径使用）。

    - content 按 ``chunk_size`` 个字符切分成多个 ``delta.content``；
    - tool_calls 的 ``arguments`` 也切成两段，专门用来验证客户端的「分片拼接」；
    - 最后补一个只带 usage 的空 choices 帧（与 ``stream_options.include_usage`` 一致），
      再以 ``data: [DONE]`` 结束。
    """
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    frames: list[dict[str, Any]] = []

    content = message.get("content")
    if content:
        text = str(content)
        for start in range(0, len(text), chunk_size):
            frames.append({"choices": [{"index": 0, "delta": {"content": text[start : start + chunk_size]}}]})

    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        arguments = str(function.get("arguments") or "{}")
        middle = max(1, len(arguments) // 2)
        frames.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call.get("id"),
                                    "type": "function",
                                    "function": {"name": function.get("name"), "arguments": ""},
                                }
                            ]
                        },
                    }
                ]
            }
        )
        for part in (arguments[:middle], arguments[middle:]):
            if not part:
                continue
            frames.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": index, "function": {"arguments": part}}
                                ]
                            },
                        }
                    ]
                }
            )

    finish_reason = choice.get("finish_reason") or "stop"
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
    frames.append({"choices": [], "usage": payload.get("usage")})

    lines = [f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockOpenAI/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {"__unparsable__": raw.decode("utf-8", "replace")}

        runtime: _ServerRuntime = self.server.runtime  # type: ignore[attr-defined]
        runtime.record(self.path, {k: v for k, v in self.headers.items()}, body)
        if runtime.delay:
            time.sleep(runtime.delay)
        status, payload, headers = runtime.next_response()

        # 客户端请求了 stream=True 且这是一条正常 completion → 回 SSE 增量帧。
        # 错误响应仍按普通 JSON 返回（流式请求的错误也是 HTTP 状态码 + JSON body）。
        streaming = isinstance(body, dict) and body.get("stream") is True
        if streaming and status == 200 and "choices" in payload:
            data = _sse_frames(payload)
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
            return

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # 静音，避免污染测试输出
        return


class ScriptedOpenAIServer:
    """真实 HTTP 端点。端口由系统分配（127.0.0.1:0），避免测试之间抢占。"""

    def __init__(
        self,
        script: Iterable[dict[str, Any]] = (),
        *,
        default: dict[str, Any] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.runtime = _ServerRuntime(script, default, delay=delay)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.runtime = self.runtime  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.runtime.requests

    def start(self) -> "ScriptedOpenAIServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ScriptedOpenAIServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


@pytest.fixture()
def server_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[..., ScriptedOpenAIServer]:
    monkeypatch.setenv(TEST_API_KEY_ENV, FAKE_KEY)
    created: list[ScriptedOpenAIServer] = []

    def make(
        script: Iterable[dict[str, Any]] = (), *, default: dict[str, Any] | None = None
    ) -> ScriptedOpenAIServer:
        server = ScriptedOpenAIServer(script, default=default).start()
        created.append(server)
        return server

    yield make
    for server in created:
        server.stop()


def build_provider(server: ScriptedOpenAIServer, **overrides: Any) -> tuple[Provider, list[float]]:
    """用真实 OpenAI 客户端指向 mock 端点（不注入 http_client）。"""
    slept: list[float] = []
    config = ModelConfig.model_validate(
        {
            "base_url": server.base_url,
            "model": "mock-model",
            "api_key_env": TEST_API_KEY_ENV,
            "temperature": 0.0,
            "max_tokens": 256,
        }
    )
    options: dict[str, Any] = {
        "request_timeout": 5.0,
        "max_retries": 3,
        "jitter": False,
        "sleeper": slept.append,
        "backoff_base": 0.01,
        "backoff_max": 0.5,
    }
    options.update(overrides)
    return Provider(config, **options), slept


# ============================================================ 端到端对话
def test_complete_conversation_with_usage(server_factory: Callable[..., ScriptedOpenAIServer]) -> None:
    server = server_factory(
        [
            chat_completion(
                "你好，我是助手。",
                usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            ),
            chat_completion(
                "你的名字是用户。",
                usage={"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
            ),
        ]
    )
    provider, slept = build_provider(server)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "你是一个严谨、简洁的助手。"},
        {"role": "user", "content": "你好"},
    ]
    first = provider.chat(messages)
    assert first.content == "你好，我是助手。"
    assert first.tool_calls == ()
    assert first.usage == Usage(11, 7, 18)

    messages.append({"role": "assistant", "content": first.content})
    messages.append({"role": "user", "content": "我叫什么？"})
    second = provider.chat(messages)
    assert second.content == "你的名字是用户。"
    total = first.usage + second.usage
    assert total == Usage(32, 16, 48)

    assert slept == []
    assert len(server.requests) == 2
    for request in server.requests:
        assert request["path"] == "/v1/chat/completions"
        assert request["body"]["model"] == "mock-model"
        assert request["body"]["stream"] is False
        assert request["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
    assert len(server.requests[0]["body"]["messages"]) == 2
    assert len(server.requests[1]["body"]["messages"]) == 4


def test_tool_call_round_trip(server_factory: Callable[..., ScriptedOpenAIServer]) -> None:
    server = server_factory(
        [
            chat_completion(
                None,
                tool_calls=[
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                    }
                ],
                usage={"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
                finish_reason="tool_calls",
            ),
            chat_completion(
                "a.txt 的内容是 hello。",
                usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            ),
        ]
    )
    provider, _ = build_provider(server)

    first = provider.chat(
        [{"role": "user", "content": "把 a.txt 读出来"}], tools=[READ_FILE_TOOL]
    )
    assert first.content is None
    assert len(first.tool_calls) == 1
    call = first.tool_calls[0]
    assert (call.id, call.name) == ("call_abc", "read_file")
    assert call.arguments_dict() == {"path": "a.txt"}

    second = provider.chat(
        [
            {"role": "user", "content": "把 a.txt 读出来"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": "hello"},
        ],
        tools=[READ_FILE_TOOL],
    )
    assert second.content == "a.txt 的内容是 hello。"

    # 第一次请求带上了工具定义，第二次请求带上了 tool 结果消息
    assert server.requests[0]["body"]["tools"][0]["function"]["name"] == "read_file"
    assert server.requests[0]["body"]["tool_choice"] == "auto"
    tool_messages = [
        message
        for message in server.requests[1]["body"]["messages"]
        if message["role"] == "tool"
    ]
    assert tool_messages == [
        {"role": "tool", "tool_call_id": "call_abc", "content": "hello", "name": "read_file"}
    ]


# ============================================================ 真实错误分类
def test_real_401_is_not_retried(server_factory: Callable[..., ScriptedOpenAIServer]) -> None:
    server = server_factory(default={"status": 401, "json": error_body("Invalid API key")})
    provider, slept = build_provider(server, max_retries=3)

    with pytest.raises(ProviderAuthError) as excinfo:
        provider.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 401
    assert len(server.requests) == 1, "401 必须一次即失败，不能重试"
    assert slept == []


def test_real_403_is_not_retried(server_factory: Callable[..., ScriptedOpenAIServer]) -> None:
    server = server_factory(default={"status": 403, "json": error_body("Forbidden")})
    provider, slept = build_provider(server, max_retries=3)

    with pytest.raises(ProviderAuthError) as excinfo:
        provider.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 403
    assert len(server.requests) == 1
    assert slept == []


def test_real_404_is_not_retried(server_factory: Callable[..., ScriptedOpenAIServer]) -> None:
    server = server_factory(default={"status": 404, "json": error_body("model not found")})
    provider, slept = build_provider(server, max_retries=3)

    with pytest.raises(ProviderRequestError):
        provider.chat([{"role": "user", "content": "hi"}])
    assert len(server.requests) == 1
    assert slept == []


def test_real_429_is_retried_then_succeeds(
    server_factory: Callable[..., ScriptedOpenAIServer],
) -> None:
    server = server_factory(
        [
            {
                "status": 429,
                "json": error_body("Rate limit reached", "rate_limit_error"),
                "headers": {"retry-after": "0"},
            },
            chat_completion(
                "限流之后成功了。",
                usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            ),
        ]
    )
    provider, slept = build_provider(server, max_retries=2)

    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result.content == "限流之后成功了。"
    assert result.usage == Usage(5, 3, 8)
    assert len(server.requests) == 2
    assert slept == [0.0], "应遵守 Retry-After: 0"


def test_real_500_is_retried_with_backoff(
    server_factory: Callable[..., ScriptedOpenAIServer],
) -> None:
    server = server_factory(
        [
            {"status": 500, "json": error_body("internal error", "server_error")},
            {"status": 503, "json": error_body("unavailable", "server_error")},
            chat_completion("重试两次后成功。"),
        ]
    )
    provider, slept = build_provider(server, max_retries=3, backoff_base=0.01)

    assert provider.chat([{"role": "user", "content": "hi"}]).content == "重试两次后成功。"
    assert len(server.requests) == 3
    assert slept == [0.01, 0.02]


def test_real_500_gives_up_after_max_retries(
    server_factory: Callable[..., ScriptedOpenAIServer],
) -> None:
    server = server_factory(default={"status": 500, "json": error_body("boom", "server_error")})
    provider, slept = build_provider(server, max_retries=2, backoff_base=0.01)

    with pytest.raises(ProviderServerError):
        provider.chat([{"role": "user", "content": "hi"}])
    assert len(server.requests) == 3  # 首次 + 2 次重试
    assert slept == [0.01, 0.02]


def test_server_classifies_rate_limit_error_type(
    server_factory: Callable[..., ScriptedOpenAIServer],
) -> None:
    server = server_factory(default={"status": 429, "json": error_body("slow down", "rate_limit_error")})
    provider, _ = build_provider(server, max_retries=0)

    with pytest.raises(ProviderRateLimitError):
        provider.chat([{"role": "user", "content": "hi"}])
    assert len(server.requests) == 1


# ============================================================ 阶段 5：完整链路
def build_loop_stack(server: ScriptedOpenAIServer, tmp_path: Path, **loop_options: Any):
    """真实链路：Provider（真 SDK 真 HTTP）+ build_registry（真 workspace）+ 真 sqlite。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    config = build_config(
        {
            "default_model": "m",
            "models": {
                "m": {
                    "base_url": server.base_url,
                    "model": "mock-model",
                    "api_key_env": TEST_API_KEY_ENV,
                }
            },
            "agent": {"workspace": str(workspace)},
        }
    )
    provider, _ = build_provider(server)
    registry = build_registry(config)
    storage = Storage(tmp_path / "sessions.db")
    session = storage.create_session("mock-model")
    options: dict[str, Any] = {
        "system_prompt": "你是一个严谨、简洁的助手。",
        "storage": storage,
        "session_id": session.id,
    }
    options.update(loop_options)
    loop = AgentLoop(provider, registry, **options)
    return loop, storage, session, workspace


def test_full_loop_tool_round_end_to_end(
    server_factory: Callable[..., ScriptedOpenAIServer], tmp_path: Path
) -> None:
    """主验证：loop → provider → 假端点 → 解析 tool_calls → 执行工具 → 回填 →
    第二次请求 → 最终回答 → usage 累计 → 落库。"""
    raw_arguments = '{ "path"  :  "note.txt" }'  # 故意保留奇怪空白，验证原样透传
    server = server_factory(
        [
            chat_completion(
                None,
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": raw_arguments},
                    }
                ],
                usage={"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
                finish_reason="tool_calls",
            ),
            chat_completion(
                "note.txt 里是：你好世界",
                usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
            ),
        ]
    )
    loop, storage, session, workspace = build_loop_stack(server, tmp_path)
    (workspace / "note.txt").write_text("你好世界", encoding="utf-8")

    result = loop.run("把 note.txt 读出来")

    # 1) 最终回答与轮数
    assert result.content == "note.txt 里是：你好世界"
    assert result.rounds == 1
    assert result.tool_limit_reached is False

    # 2) usage 两轮累加
    assert result.usage == Usage(60, 22, 82)
    record = storage.require_session(session.id)
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (60, 22, 82)

    # 3) 服务端真的收到了两轮请求
    assert len(server.requests) == 2
    first, second = server.requests
    assert first["body"]["messages"] == [
        {"role": "system", "content": "你是一个严谨、简洁的助手。"},
        {"role": "user", "content": "把 note.txt 读出来"},
    ]
    assert first["body"]["tools"][0]["function"]["name"] == "read_file"
    # 真流式重构后：两轮请求都走 stream=True（README 第 48 条）
    assert first["body"]["stream"] is True and second["body"]["stream"] is True

    # 4) 回填的第二轮请求：assistant(tool_calls) + tool 结果，且读到了真实文件内容
    assert [m["role"] for m in second["body"]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assistant = second["body"]["messages"][2]
    assert assistant["content"] is None and "content" in assistant
    assert assistant["tool_calls"][0]["function"]["arguments"] == raw_arguments, (
        "arguments 必须是模型返回的原始字符串，不能被重新序列化"
    )
    tool_message = second["body"]["messages"][3]
    assert tool_message["tool_call_id"] == "call_read"
    assert tool_message["name"] == "read_file"
    assert "你好世界" in tool_message["content"]
    assert isinstance(tool_message["content"], str)

    # 5) 落库：消息序列完整且合法
    stored = storage.get_messages(session.id)
    assert [m["role"] for m in stored] == ["system", "user", "assistant", "tool", "assistant"]
    assert stored[2]["tool_calls"][0]["id"] == "call_read"
    assert stored[3]["tool_call_id"] == "call_read"
    storage.close()


def test_full_loop_two_tool_calls_match_by_id(
    server_factory: Callable[..., ScriptedOpenAIServer], tmp_path: Path
) -> None:
    """真实链路上的「不串号」验证：一次响应两个 tool_call，读两个内容不同的文件。

    若回填顺序或 id 对不上，第二个文件的内容就会被标到第一个 id 上 ——
    这里通过「线上实际发出去的请求体」来判定。
    """
    server = server_factory(
        [
            chat_completion(
                None,
                tool_calls=[
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                    },
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "b.txt"}'},
                    },
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
                finish_reason="tool_calls",
            ),
            chat_completion(
                "a.txt 是 AAA，b.txt 是 BBB",
                usage={"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
            ),
        ]
    )
    loop, storage, session, workspace = build_loop_stack(server, tmp_path)
    (workspace / "a.txt").write_text("AAA-内容", encoding="utf-8")
    (workspace / "b.txt").write_text("BBB-内容", encoding="utf-8")

    result = loop.run("把 a.txt 和 b.txt 都读出来")
    assert result.content == "a.txt 是 AAA，b.txt 是 BBB"

    second = server.requests[1]["body"]["messages"]
    assistant = [m for m in second if m["role"] == "assistant"][0]
    assert [call["id"] for call in assistant["tool_calls"]] == ["a", "b"]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert assistant["tool_calls"][1]["function"]["arguments"] == '{"path": "b.txt"}'

    tool_messages = [m for m in second if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["a", "b"], "线上顺序必须是 a→b"
    assert {m["tool_call_id"]: m["content"] for m in tool_messages} == {
        "a": tool_messages[0]["content"],
        "b": tool_messages[1]["content"],
    }
    assert "AAA-内容" in tool_messages[0]["content"]
    assert "BBB-内容" in tool_messages[1]["content"]
    assert "BBB-内容" not in tool_messages[0]["content"], "b 的结果不能出现在 a 的消息里"
    assert "AAA-内容" not in tool_messages[1]["content"]

    assert result.usage == Usage(40, 20, 60)
    storage.close()


def test_full_loop_arguments_verbatim_on_the_wire(
    server_factory: Callable[..., ScriptedOpenAIServer], tmp_path: Path
) -> None:
    """要求 3 的真实链路版本：抓到线上请求体，arguments 必须逐字符等于模型原文。"""
    raw = '{"x": 1, "y":2}'
    server = server_factory(
        [
            chat_completion(
                None,
                tool_calls=[
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "list_dir", "arguments": raw},
                    }
                ],
                finish_reason="tool_calls",
            ),
            chat_completion("好的"),
        ]
    )
    loop, storage, session, _ = build_loop_stack(server, tmp_path)
    loop.run("随便测一下")

    assistant = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["function"]["arguments"] == raw
    assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], str)
    storage.close()


def test_full_loop_feeds_tool_error_back_to_model(
    server_factory: Callable[..., ScriptedOpenAIServer], tmp_path: Path
) -> None:
    """工具越界失败时：不崩溃，错误文本作为 tool 消息回灌，模型拿到后给出最终回答。"""
    server = server_factory(
        [
            chat_completion(
                None,
                tool_calls=[
                    {
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "../secret.txt"}'},
                    }
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                finish_reason="tool_calls",
            ),
            chat_completion(
                "我无法读取 workspace 之外的文件。",
                usage={"prompt_tokens": 25, "completion_tokens": 9, "total_tokens": 34},
            ),
        ]
    )
    loop, storage, session, workspace = build_loop_stack(server, tmp_path)
    (tmp_path / "secret.txt").write_text("机密", encoding="utf-8")

    result = loop.run("读 ../secret.txt")

    assert result.content == "我无法读取 workspace 之外的文件。"
    tool_message = server.requests[1]["body"]["messages"][3]
    assert tool_message["role"] == "tool"
    assert tool_message["content"].startswith("错误：")
    assert "越出 workspace" in tool_message["content"]
    assert result.usage == Usage(35, 14, 49)
    storage.close()


def test_full_loop_stops_at_tool_round_limit(
    server_factory: Callable[..., ScriptedOpenAIServer], tmp_path: Path
) -> None:
    """达到上限时不再发请求，历史末尾是已回填的 tool 消息（序列合法）。"""
    tool_step = chat_completion(
        None,
        tool_calls=[
            {
                "id": "call_loop",
                "type": "function",
                "function": {"name": "list_dir", "arguments": '{"path": "."}'},
            }
        ],
        finish_reason="tool_calls",
    )
    server = server_factory(default=tool_step)
    loop, storage, session, _ = build_loop_stack(server, tmp_path, max_tool_rounds=2)

    result = loop.run("一直列目录")

    assert result.tool_limit_reached is True
    assert result.content == "已达工具调用上限（2 轮），已停止继续调用工具。"
    assert result.rounds == 2
    assert len(server.requests) == 2, "第 3 轮请求不应该发出"

    history = loop.messages
    assert history[-1]["role"] == "tool"
    assert history[-2]["role"] == "assistant" and history[-2]["tool_calls"]
    assert history[-2]["tool_calls"][0]["id"] == history[-1]["tool_call_id"]
    storage.close()