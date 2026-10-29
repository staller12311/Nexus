"""阶段 3：provider 的纯 mock 测试（不联网、不用任何 HTTP 拦截库）。

分三层：
1. 解析：把「模型响应」翻译成 ChatResult（含 tool_calls 解析）；
2. 分类：SDK 异常 → provider 错误类型（401/403 不重试、429/5xx/超时可重试）；
3. 退避：替换 ``_raw_call`` 后断言重试次数与 sleep 间隔。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx2
import openai
import pytest

from agent.config import ModelConfig
from agent.provider import (
    ChatResult,
    Provider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
    ToolCall,
    Usage,
    classify_status,
    translate_exception,
)

TEST_API_KEY_ENV = "PROVIDER_TEST_API_KEY"


# ------------------------------------------------------------------ 工具
def make_model_config(**overrides: Any) -> ModelConfig:
    data = {
        "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model",
        "api_key_env": TEST_API_KEY_ENV,
        "temperature": 0.25,
        "max_tokens": 128,
    }
    data.update(overrides)
    return ModelConfig.model_validate(data)


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "http://127.0.0.1:9/v1/chat/completions")


def _response(status: int = 200, headers: dict[str, str] | None = None) -> httpx2.Response:
    response = httpx2.Response(status, headers=headers or {}, json={})
    response.request = _request()  # SDK 读取 request_id 等属性时需要
    return response


def status_error(
    cls: type[openai.APIStatusError], status: int, headers: dict[str, str] | None = None
) -> openai.APIStatusError:
    return cls(f"HTTP {status}", response=_response(status, headers), body=None)


def fake_tool_call(
    name: str = "read_file", arguments: Any = '{"path": "a.txt"}', call_id: str | None = "call_1"
) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def fake_response(
    content: str | None = "你好",
    tool_calls: list[Any] | None = None,
    usage: Any = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


class ScriptedProvider(Provider):
    """把唯一 HTTP 出口换成脚本：断言「调用了几次、睡了几秒」。"""

    def __init__(self, script: list[Any], **kwargs: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.slept: list[float] = []
        kwargs.setdefault("sleeper", self.slept.append)
        kwargs.setdefault("jitter", False)
        kwargs.setdefault("logger", logging.getLogger("agent.test.provider"))
        super().__init__(kwargs.pop("model_config", None) or make_model_config(),
                         client=object(), **kwargs)

    def _raw_call(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        if not self.script:
            raise AssertionError("脚本已用尽，说明重试次数超出预期")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def ok_result(**kwargs: Any) -> SimpleNamespace:
    return fake_response(**kwargs)


# ================================================================== 解析
class TestParsing:
    def test_chat_returns_content_and_usage(self) -> None:
        provider = ScriptedProvider(
            [
                fake_response(
                    content="最终回答",
                    usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
                )
            ]
        )
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert isinstance(result, ChatResult)
        assert result.content == "最终回答"
        assert result.tool_calls == ()
        assert result.wants_tools is False
        assert result.usage == Usage(11, 7, 18)

    def test_chat_parses_tool_calls(self) -> None:
        provider = ScriptedProvider(
            [
                fake_response(
                    content=None,
                    tool_calls=[
                        fake_tool_call("read_file", '{"path": "a.txt"}', "call_a"),
                        fake_tool_call("list_dir", '{"path": "."}', "call_b"),
                    ],
                    finish_reason="tool_calls",
                )
            ]
        )
        result = provider.chat([{"role": "user", "content": "读文件"}])
        assert result.content is None
        assert result.wants_tools is True
        assert result.tool_calls == (
            ToolCall(id="call_a", name="read_file", arguments='{"path": "a.txt"}'),
            ToolCall(id="call_b", name="list_dir", arguments='{"path": "."}'),
        )
        assert result.tool_calls[0].arguments_dict() == {"path": "a.txt"}

    def test_tool_call_without_name_is_rejected(self) -> None:
        provider = ScriptedProvider(
            [fake_response(content=None, tool_calls=[fake_tool_call(name=None)])]
        )
        with pytest.raises(ProviderResponseError) as excinfo:
            provider.chat([{"role": "user", "content": "x"}])
        assert "function.name" in str(excinfo.value)

    def test_tool_call_id_falls_back_to_index(self) -> None:
        provider = ScriptedProvider(
            [fake_response(content=None, tool_calls=[fake_tool_call(call_id=None)])]
        )
        result = provider.chat([{"role": "user", "content": "x"}])
        assert result.tool_calls[0].id == "call_0"

    def test_non_string_arguments_are_jsonified(self) -> None:
        provider = ScriptedProvider(
            [fake_response(content=None, tool_calls=[fake_tool_call(arguments={"path": "b"})])]
        )
        result = provider.chat([{"role": "user", "content": "x"}])
        assert result.tool_calls[0].arguments == '{"path": "b"}'

    def test_null_arguments_become_empty_object(self) -> None:
        provider = ScriptedProvider(
            [fake_response(content=None, tool_calls=[fake_tool_call(arguments=None)])]
        )
        result = provider.chat([{"role": "user", "content": "x"}])
        assert result.tool_calls[0].arguments == "{}"
        assert result.tool_calls[0].arguments_dict() == {}

    def test_usage_from_mapping_and_missing(self) -> None:
        assert Usage.from_raw({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}) == (
            Usage(1, 2, 3)
        )
        assert Usage.from_raw(None) == Usage(0, 0, 0)
        assert Usage.from_raw({"prompt_tokens": "bad"}) == Usage(0, 0, 0)

    def test_usage_is_additive(self) -> None:
        assert Usage(1, 2, 3) + Usage(4, 5, 6) == Usage(5, 7, 9)

    def test_response_without_choices_is_rejected(self) -> None:
        provider = ScriptedProvider([SimpleNamespace(choices=[], usage=None)])
        with pytest.raises(ProviderResponseError):
            provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.calls) == 1  # 解析失败不触发重试

    def test_response_without_message_is_rejected(self) -> None:
        provider = ScriptedProvider([SimpleNamespace(choices=[SimpleNamespace()], usage=None)])
        with pytest.raises(ProviderResponseError):
            provider.chat([{"role": "user", "content": "x"}])

    def test_empty_content_without_tool_calls_is_rejected(self) -> None:
        provider = ScriptedProvider([fake_response(content="")])
        with pytest.raises(ProviderResponseError) as excinfo:
            provider.chat([{"role": "user", "content": "x"}])
        assert "content" in str(excinfo.value)

    def test_empty_string_content_is_normalized_to_none(self) -> None:
        provider = ScriptedProvider(
            [fake_response(content="", tool_calls=[fake_tool_call()])]
        )
        assert provider.chat([{"role": "user", "content": "x"}]).content is None

    def test_arguments_dict_rejects_invalid_json(self) -> None:
        with pytest.raises(ProviderResponseError):
            ToolCall(id="c", name="read_file", arguments="{not json").arguments_dict()

    def test_arguments_dict_rejects_non_object(self) -> None:
        with pytest.raises(ProviderResponseError) as excinfo:
            ToolCall(id="c", name="read_file", arguments="[1, 2]").arguments_dict()
        assert "JSON 对象" in str(excinfo.value)


# ================================================================== 请求体
class TestPayload:
    def test_stream_is_always_false(self) -> None:
        provider = ScriptedProvider([ok_result()])
        provider.chat([{"role": "user", "content": "x"}])
        assert provider.calls[0]["stream"] is False

    def test_tools_included_only_when_given(self) -> None:
        provider = ScriptedProvider([ok_result(), ok_result()])
        provider.chat([{"role": "user", "content": "x"}])
        assert "tools" not in provider.calls[0]
        assert "tool_choice" not in provider.calls[0]
        provider.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        assert provider.calls[1]["tools"] == [{"type": "function"}]
        assert provider.calls[1]["tool_choice"] == "auto"

    def test_uses_config_defaults_and_allows_overrides(self) -> None:
        provider = ScriptedProvider([ok_result(), ok_result()])
        provider.chat([{"role": "user", "content": "x"}])
        assert provider.calls[0]["model"] == "test-model"
        assert provider.calls[0]["temperature"] == 0.25
        assert provider.calls[0]["max_tokens"] == 128
        provider.chat([{"role": "user", "content": "x"}], model="other", temperature=1.0, max_tokens=9)
        assert provider.calls[1]["model"] == "other"
        assert provider.calls[1]["temperature"] == 1.0
        assert provider.calls[1]["max_tokens"] == 9

    def test_messages_are_copied_not_aliased(self) -> None:
        provider = ScriptedProvider([ok_result()])
        source = [{"role": "user", "content": "x"}]
        provider.chat(source)
        assert provider.calls[0]["messages"] == source
        assert provider.calls[0]["messages"] is not source


# ================================================================== 分类
class TestClassification:
    def test_401_and_403_are_not_retryable(self) -> None:
        for cls, status in ((openai.AuthenticationError, 401), (openai.PermissionDeniedError, 403)):
            error = translate_exception(status_error(cls, status))
            assert isinstance(error, ProviderAuthError)
            assert error.retryable is False
            assert error.status_code == status

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_is_retryable(self, status: int) -> None:
        error = translate_exception(status_error(openai.InternalServerError, status))
        assert isinstance(error, ProviderServerError)
        assert error.retryable is True

    def test_429_is_retryable_and_reads_retry_after(self) -> None:
        error = translate_exception(
            status_error(openai.RateLimitError, 429, {"retry-after": "2.5"})
        )
        assert isinstance(error, ProviderRateLimitError)
        assert error.retryable is True
        assert error.retry_after == 2.5

    def test_invalid_retry_after_is_ignored(self) -> None:
        error = translate_exception(
            status_error(openai.RateLimitError, 429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        )
        assert error.retry_after is None

    def test_408_is_timeout_and_retryable(self) -> None:
        error = classify_status(408, message="timeout")
        assert isinstance(error, ProviderTimeoutError)
        assert error.retryable is True

    @pytest.mark.parametrize("status", [400, 404, 422])
    def test_other_4xx_is_not_retryable(self, status: int) -> None:
        error = classify_status(status, message="bad")
        assert isinstance(error, ProviderRequestError)
        assert error.retryable is False

    def test_api_timeout_error_maps_to_provider_timeout(self) -> None:
        error = translate_exception(openai.APITimeoutError(request=_request()))
        assert isinstance(error, ProviderTimeoutError)
        assert error.retryable is True

    def test_api_connection_error_maps_to_provider_connection(self) -> None:
        error = translate_exception(openai.APIConnectionError(request=_request()))
        assert isinstance(error, ProviderConnectionError)
        assert error.retryable is True

    def test_unknown_api_error_maps_to_base_error(self) -> None:
        error = translate_exception(openai.APIError("weird", _request(), body=None))
        assert type(error) is ProviderError
        assert error.retryable is False

    def test_provider_error_passes_through(self) -> None:
        original = ProviderAuthError("already classified")
        assert translate_exception(original) is original


# ================================================================== 退避
class TestRetry:
    def test_retries_429_then_succeeds(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.RateLimitError, 429), ok_result(content="ok")]
        )
        result = provider.chat([{"role": "user", "content": "x"}])
        assert result.content == "ok"
        assert len(provider.calls) == 2
        assert provider.slept == [1.0]

    def test_backoff_is_exponential(self) -> None:
        provider = ScriptedProvider(
            [
                status_error(openai.InternalServerError, 500),
                status_error(openai.InternalServerError, 503),
                status_error(openai.InternalServerError, 502),
                ok_result(content="ok"),
            ],
            max_retries=3,
        )
        provider.chat([{"role": "user", "content": "x"}])
        assert provider.slept == [1.0, 2.0, 4.0]
        assert len(provider.calls) == 4

    def test_gives_up_after_max_retries(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.InternalServerError, 500) for _ in range(5)], max_retries=2
        )
        with pytest.raises(ProviderServerError):
            provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.calls) == 3  # 首次 + 2 次重试
        assert provider.slept == [1.0, 2.0]

    def test_zero_max_retries_means_single_attempt(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.InternalServerError, 500), ok_result()], max_retries=0
        )
        with pytest.raises(ProviderServerError):
            provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.calls) == 1
        assert provider.slept == []

    def test_401_is_not_retried(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.AuthenticationError, 401), ok_result(content="never")]
        )
        with pytest.raises(ProviderAuthError):
            provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.calls) == 1
        assert provider.slept == []

    def test_403_is_not_retried(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.PermissionDeniedError, 403), ok_result(content="never")]
        )
        with pytest.raises(ProviderAuthError):
            provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.calls) == 1
        assert provider.slept == []

    def test_400_is_not_retried(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.BadRequestError, 400), ok_result(content="never")]
        )
        with pytest.raises(ProviderRequestError):
            provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.calls) == 1

    def test_timeout_is_retried(self) -> None:
        provider = ScriptedProvider(
            [openai.APITimeoutError(request=_request()), ok_result(content="ok")]
        )
        assert provider.chat([{"role": "user", "content": "x"}]).content == "ok"
        assert provider.slept == [1.0]

    def test_connection_error_is_retried(self) -> None:
        provider = ScriptedProvider(
            [openai.APIConnectionError(request=_request()), ok_result(content="ok")]
        )
        assert provider.chat([{"role": "user", "content": "x"}]).content == "ok"
        assert provider.slept == [1.0]

    def test_retry_after_header_is_honoured(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.RateLimitError, 429, {"retry-after": "2.5"}), ok_result()]
        )
        provider.chat([{"role": "user", "content": "x"}])
        assert provider.slept == [2.5]

    def test_retry_after_is_capped_by_backoff_max(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.RateLimitError, 429, {"retry-after": "9999"}), ok_result()],
            backoff_max=5.0,
        )
        provider.chat([{"role": "user", "content": "x"}])
        assert provider.slept == [5.0]

    def test_backoff_is_capped_by_backoff_max(self) -> None:
        provider = ScriptedProvider(
            [status_error(openai.InternalServerError, 500) for _ in range(6)],
            max_retries=5,
            backoff_max=4.0,
        )
        with pytest.raises(ProviderServerError):
            provider.chat([{"role": "user", "content": "x"}])
        assert provider.slept == [1.0, 2.0, 4.0, 4.0, 4.0]

    def test_408_backoff_matches_5xx_backoff(self) -> None:
        """408 归类为瞬态超时，退避时长必须与 5xx 完全一致。"""
        timeout_provider = ScriptedProvider(
            [status_error(openai.APIStatusError, 408), ok_result()], backoff_base=1.0
        )
        server_provider = ScriptedProvider(
            [status_error(openai.InternalServerError, 500), ok_result()], backoff_base=1.0
        )
        timeout_provider.chat([{"role": "user", "content": "x"}])
        server_provider.chat([{"role": "user", "content": "x"}])
        assert timeout_provider.slept == server_provider.slept == [1.0]

    def test_jitter_stays_within_half_to_full_delay(self) -> None:
        import random

        provider = ScriptedProvider(
            [status_error(openai.InternalServerError, 500), ok_result()],
            jitter=True,
            rng=random.Random(1234),
            backoff_base=8.0,
            backoff_max=100.0,
        )
        provider.chat([{"role": "user", "content": "x"}])
        assert len(provider.slept) == 1
        assert 4.0 <= provider.slept[0] <= 8.0


# ============================================== 真实 _raw_call（MagicMock 客户端）
class TestRawCallAndTranslation:
    """``_raw_call`` 是纯传输层：原样抛 SDK 异常；分类发生在外层 ``chat()``。"""

    def _provider(self, side_effect: Any, **kwargs: Any) -> Provider:
        client = MagicMock()
        client.chat.completions.create.side_effect = side_effect
        kwargs.setdefault("sleeper", lambda seconds: None)
        kwargs.setdefault("jitter", False)
        return Provider(make_model_config(), client=client, **kwargs)

    def test_raw_call_passes_sdk_exception_through(self) -> None:
        provider = self._provider(status_error(openai.AuthenticationError, 401))
        with pytest.raises(openai.AuthenticationError):
            provider._raw_call({"model": "m", "messages": []})

    def test_chat_classifies_401_from_real_raw_call(self) -> None:
        provider = self._provider(status_error(openai.AuthenticationError, 401))
        with pytest.raises(ProviderAuthError):
            provider.chat([{"role": "user", "content": "x"}])

    def test_chat_retries_through_real_raw_call(self) -> None:
        slept: list[float] = []
        provider = self._provider(
            [status_error(openai.RateLimitError, 429), fake_response(content="ok")],
            sleeper=slept.append,
        )
        result = provider.chat([{"role": "user", "content": "x"}])
        assert result.content == "ok"
        assert provider._client.chat.completions.create.call_count == 2
        assert slept == [1.0]

    def test_401_through_real_raw_call_is_not_retried(self) -> None:
        provider = self._provider(status_error(openai.AuthenticationError, 401))
        with pytest.raises(ProviderAuthError):
            provider.chat([{"role": "user", "content": "x"}])
        assert provider._client.chat.completions.create.call_count == 1

    def test_unexpected_exception_is_not_swallowed(self) -> None:
        provider = self._provider(TypeError("payload 拼错了"))
        with pytest.raises(TypeError):
            provider.chat([{"role": "user", "content": "x"}])


# ================================================================== 其他
def test_describe_never_leaks_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEST_API_KEY_ENV, "sk-secret-value-123456")
    provider = Provider(make_model_config(), client=object())
    described = provider.describe()
    assert "sk-secret-value-123456" not in described
    assert TEST_API_KEY_ENV in described


def test_from_config_wires_agent_settings(tmp_path: Any) -> None:
    from agent.config import build_config

    config = build_config(
        {
            "default_model": "m",
            "models": {
                "m": {
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "test-model",
                    "api_key_env": TEST_API_KEY_ENV,
                }
            },
            "agent": {"request_timeout": 12, "max_retries": 5},
        }
    )
    provider = Provider.from_config(config, client=object())
    assert provider.request_timeout == 12.0
    assert provider.max_retries == 5
    assert provider.model_name == "test-model"

# ================================================================== 缓存命中
class TestUsageCacheParsing:
    """Usage 解析两种主流缓存字段：DeepSeek 顶层 / OpenAI 嵌套。"""

    def test_deepseek_prompt_cache_hit_tokens(self) -> None:
        raw = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_cache_hit_tokens=80,
        )
        usage = Usage.from_raw(raw)
        assert usage.cache_hit_tokens == 80
        assert usage.cache_hit_rate == pytest.approx(0.8)

    def test_openai_nested_cached_tokens(self) -> None:
        raw = {
            "prompt_tokens": 200,
            "completion_tokens": 10,
            "total_tokens": 210,
            "prompt_tokens_details": {"cached_tokens": 150},
        }
        usage = Usage.from_raw(raw)
        assert usage.cache_hit_tokens == 150
        assert usage.cache_hit_rate == pytest.approx(0.75)

    def test_missing_cache_fields_default_to_zero(self) -> None:
        usage = Usage.from_raw({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
        assert usage.cache_hit_tokens == 0
        assert usage.cache_hit_rate == 0.0
        assert Usage.from_raw(None).cache_hit_rate == 0.0
        # 没有 prompt token 时不用 0 做除数
        assert Usage(prompt_tokens=0, cache_hit_tokens=3).cache_hit_rate == 0.0

    def test_usage_add_accumulates_cache(self) -> None:
        total = Usage(10, 2, 12, 7) + Usage(5, 1, 6, 3)
        assert total == Usage(15, 3, 18, 10)
