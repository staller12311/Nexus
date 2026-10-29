"""LLM 客户端统一封装（OpenAI 兼容 API）。

职责：
- 把 ``openai`` SDK 包成与 loop 解耦的 ``Provider.chat()``，返回 ``ChatResult``；
- 错误分类与重试策略集中在 ``_raw_call`` / ``_call_with_retry`` 两层，
  便于用纯 mock 验证退避行为（不需要网络，也不需要任何 HTTP 拦截库）。

设计决策（详见 README「设计决策」）：
- **SDK 自带重试被关闭**（``max_retries=0``）：重试由本模块实现，因为需要
  「401/403 不重试、429/5xx/超时才退避」这种按状态码分流的策略，
  并且要能在测试里确定性地断言重试次数与间隔。
- 分类只认状态码与异常类型，不做「一刀切」：401/403 立即失败，429 与 5xx 指数退避，
  超时（含 HTTP 408）单独分类，其余 4xx 视为请求错误不重试。
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import openai

from . import logging_setup
from .config import AgentConfig, AppConfig, ConfigError, ModelConfig

__all__ = [
    "ChatResult",
    "Provider",
    "ProviderAuthError",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "ToolCall",
    "Usage",
]

DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_MAX = 30.0

#: 401/403：凭据问题，重试没有意义，必须立即暴露
NO_RETRY_STATUS = (401, 403)
#: 429：限流，退避重试
RATE_LIMIT_STATUS = 429
#: HTTP 408：服务端视角的「请求超时」，与 APITimeoutError 归为同一类
TIMEOUT_STATUS = 408


# --------------------------------------------------------------------- 错误
class ProviderError(Exception):
    """所有 provider 层错误的基类。"""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.request_id = request_id


class ProviderAuthError(ProviderError):
    """401/403：密钥无效、无权限。绝不重试。"""

    retryable = False


class ProviderRequestError(ProviderError):
    """其余 4xx：请求本身有问题（模型名错、参数非法等）。绝不重试。"""

    retryable = False


class ProviderRateLimitError(ProviderError):
    """429：限流。退避后重试。"""

    retryable = True


class ProviderServerError(ProviderError):
    """5xx：服务端故障。退避后重试。"""

    retryable = True


class ProviderTimeoutError(ProviderError):
    """超时：既包括客户端 APITimeoutError，也包括 HTTP 408。退避后重试。"""

    retryable = True


class ProviderConnectionError(ProviderError):
    """连接失败（DNS / 拒连 / 断流）。退避后重试。"""

    retryable = True


class ProviderResponseError(ProviderError):
    """响应结构不符合预期。属于程序性错误，重试不会变好。绝不重试。"""

    retryable = False


# --------------------------------------------------------------------- 结果
@dataclass(frozen=True)
class ToolCall:
    """一次工具调用请求。``arguments`` 保持模型给出的原始 JSON 字符串。"""

    id: str
    name: str
    arguments: str = "{}"

    def arguments_dict(self) -> dict[str, Any]:
        """解析参数。非法 JSON 按 ProviderResponseError 抛出（重试无意义）。"""
        raw = (self.arguments or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"工具 {self.name} 的参数不是合法 JSON：{exc.msg}（原文 {raw[:200]!r}）"
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderResponseError(
                f"工具 {self.name} 的参数必须是 JSON 对象，实际是 {type(parsed).__name__}"
            )
        return parsed


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: 缓存命中的 prompt token 数（DeepSeek / OpenAI 等厂商的「省钱指标」）
    #: 取不到就是 0，绝不影响主流程。
    cache_hit_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率（0.0–1.0）；没有 prompt token 时按 0 处理。"""
        if self.prompt_tokens <= 0:
            return 0.0
        return min(self.cache_hit_tokens / self.prompt_tokens, 1.0)

    @classmethod
    def from_raw(cls, raw: Any) -> "Usage":
        if raw is None:
            return cls()

        def field(source: Any, key: str) -> Any:
            if source is None:
                return None
            if isinstance(source, Mapping):
                return source.get(key)
            return getattr(source, key, None)

        def pick(key: str) -> int:
            try:
                return int(field(raw, key) or 0)
            except (TypeError, ValueError):
                return 0

        # 缓存命中字段的两种主流写法：
        # - DeepSeek: usage.prompt_cache_hit_tokens（顶层）
        # - OpenAI:   usage.prompt_tokens_details.cached_tokens（嵌套）
        # 都取不到就是 0；两边都有时以非零的那个为准。
        details = field(raw, "prompt_tokens_details")
        try:
            nested_cached = int(field(details, "cached_tokens") or 0)
        except (TypeError, ValueError):
            nested_cached = 0
        cache_hit_tokens = pick("prompt_cache_hit_tokens") or nested_cached

        return cls(
            prompt_tokens=pick("prompt_tokens"),
            completion_tokens=pick("completion_tokens"),
            total_tokens=pick("total_tokens"),
            cache_hit_tokens=cache_hit_tokens,
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
            self.cache_hit_tokens + other.cache_hit_tokens,
        )


@dataclass(frozen=True)
class ChatResult:
    """一次模型调用的结果。与 loop 解耦：这里不认识会话、工具执行与历史。"""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# --------------------------------------------------------------------- 分类
def classify_status(
    status_code: int,
    *,
    message: str,
    retry_after: float | None = None,
    request_id: str | None = None,
) -> ProviderError:
    """按状态码返回对应错误类型（错误分类的唯一定义处）。"""
    kwargs = {
        "status_code": status_code,
        "retry_after": retry_after,
        "request_id": request_id,
    }
    if status_code in NO_RETRY_STATUS:
        hint = "请检查 api_key_env 指向的环境变量是否正确、是否有该模型权限"
        return ProviderAuthError(f"HTTP {status_code}（认证/权限失败，不重试）：{message}；{hint}", **kwargs)
    if status_code == RATE_LIMIT_STATUS:
        return ProviderRateLimitError(f"HTTP 429（限流，将退避重试）：{message}", **kwargs)
    if 500 <= status_code <= 599:
        return ProviderServerError(f"HTTP {status_code}（服务端错误，将退避重试）：{message}", **kwargs)
    if status_code == TIMEOUT_STATUS:
        return ProviderTimeoutError(f"HTTP {status_code}（请求超时，将退避重试）：{message}", **kwargs)
    return ProviderRequestError(f"HTTP {status_code}（请求错误，不重试）：{message}", **kwargs)


def _parse_retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None  # HTTP-date 形式暂不支持，退回指数退避
    return value if value >= 0 else None


def translate_exception(exc: Exception) -> ProviderError:
    """把 openai SDK 的异常翻译成本模块的错误分类。"""
    if isinstance(exc, ProviderError):
        return exc
    # 注意顺序：APITimeoutError 是 APIConnectionError 的子类，必须先判超时
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError(f"请求超时（将退避重试）：{exc}")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderConnectionError(f"连接失败（将退避重试）：{exc}")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        return classify_status(
            int(status) if status is not None else 0,
            message=str(exc),
            retry_after=_parse_retry_after(getattr(exc, "response", None)),
            request_id=getattr(exc, "request_id", None),
        )
    if isinstance(exc, openai.APIError):
        return ProviderError(f"调用失败：{exc}")
    return ProviderError(str(exc))


# ----------------------------------------------------------------- provider
class Provider:
    """单个模型的调用封装。一个 Provider 对应一个 ``ModelConfig``。"""

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        request_timeout: float = 60.0,
        max_retries: int = 3,
        client: Any | None = None,
        sleeper: Callable[[float], None] | None = None,
        jitter: bool = True,
        rng: random.Random | None = None,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        logger: logging.Logger | None = None,
    ) -> None:
        self.model_config = model_config
        self.request_timeout = float(request_timeout)
        self.max_retries = max(0, int(max_retries))
        self._sleeper: Callable[[float], None] = sleeper or time.sleep
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._backoff_base = float(backoff_base)
        self._backoff_max = float(backoff_max)
        self._logger = logger or logging_setup.get_logger("agent.provider")
        self._client = client if client is not None else self._build_client()

    # ---- 构造 -----------------------------------------------------------
    @classmethod
    def from_config(
        cls, config: AppConfig, model_name: str | None = None, **overrides: Any
    ) -> "Provider":
        agent: AgentConfig = config.agent
        options: dict[str, Any] = {
            "request_timeout": agent.request_timeout,
            "max_retries": agent.max_retries,
        }
        options.update(overrides)
        return cls(config.get_model(model_name), **options)

    def _build_client(self) -> Any:
        try:
            api_key = self.model_config.api_key()
        except ConfigError:
            raise
        logging_setup.register_secret(api_key)
        # max_retries=0：关闭 SDK 自带重试，重试完全由本类控制
        return openai.OpenAI(
            api_key=api_key,
            base_url=self.model_config.base_url,
            timeout=self.request_timeout,
            max_retries=0,
        )

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "Provider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def model_name(self) -> str:
        return self.model_config.model

    # ---- 对外接口 -------------------------------------------------------
    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """非流式调用一次模型。P0 固定 ``stream=False``。"""
        payload = self.build_payload(
            messages,
            tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        response = self._call_with_retry(payload)
        return self._parse_response(response)

    def chat_stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """流式调用一次模型，逐个 yield 增量事件。

        事件协议（loop / backend / 前端都按这个消费）::

            {"type": "reasoning", "text": "…"}   # 思维链增量（reasoning_content）
            {"type": "text",      "text": "…"}   # 正文增量
            {"type": "done", "content": str|None, "reasoning": str|None,
             "tool_calls": (ToolCall, ...), "usage": Usage}

        ``tool_calls`` 在**流结束后**才统一给出：流式分片里每个 tool_call 只有一个
        ``index`` 和碎片化的 ``function.arguments``，必须先按 index 在内存里拼装完整
        JSON 字符串，再一次性交出去（README 第 48 条）。
        """
        payload = self.build_payload(
            messages,
            tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        stream = self._open_stream_with_retry(payload)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason: str | None = None

        try:
            for chunk in stream:
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage is not None:
                    usage = Usage.from_raw(raw_usage)

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                # finish_reason 只在最后一个带 choices 的 chunk 上出现（"stop"/"length"/"tool_calls"）
                raw_finish = getattr(choice, "finish_reason", None)
                if raw_finish:
                    finish_reason = str(raw_finish)

                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                    yield {"type": "reasoning", "text": str(reasoning)}

                content = getattr(delta, "content", None)
                if content:
                    content_parts.append(str(content))
                    yield {"type": "text", "text": str(content)}

                for raw_call in getattr(delta, "tool_calls", None) or ():
                    self._merge_tool_call_delta(tool_slots, raw_call)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关流失败不该盖住真正的异常
                    pass

        yield {
            "type": "done",
            "content": "".join(content_parts) or None,
            "reasoning": "".join(reasoning_parts) or None,
            "tool_calls": self._finalize_tool_calls(tool_slots),
            "usage": usage,
            # 让上层能区分「正常结束」与「被 max_tokens 截断」
            "finish_reason": finish_reason,
        }

    @staticmethod
    def _merge_tool_call_delta(slots: dict[int, dict[str, str]], raw_call: Any) -> None:
        """把一个流式 tool_call 碎片并进内存槽位（按 ``index`` 对齐）。

        碎片形态：首个分片通常带 ``id`` 与 ``function.name``，后续分片只带
        ``function.arguments`` 的一小段 —— 所以 arguments 必须**字符串拼接**，
        绝不能对每个分片单独 json.loads。
        """
        index = getattr(raw_call, "index", None)
        slot = slots.setdefault(
            int(index) if index is not None else len(slots),
            {"id": "", "name": "", "arguments": ""},
        )

        call_id = getattr(raw_call, "id", None)
        if call_id:
            slot["id"] = str(call_id)

        function = getattr(raw_call, "function", None)
        if function is None:
            return

        name = getattr(function, "name", None)
        if name:
            if not slot["name"]:
                slot["name"] = str(name)
            elif str(name) != slot["name"]:
                slot["name"] += str(name)  # 上游按字符分片时也能拼起来

        arguments = getattr(function, "arguments", None)
        if arguments:
            slot["arguments"] += str(arguments)

    @staticmethod
    def _finalize_tool_calls(slots: dict[int, dict[str, str]]) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for index in sorted(slots):
            slot = slots[index]
            if not slot["name"]:
                continue  # 只有参数碎片、没有名字的残片丢弃
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
            )
        return tuple(calls)

    def _open_stream_with_retry(self, payload: dict[str, Any]) -> Any:
        """只对**建立连接阶段**重试；流一旦开始吐数据就不再重试（避免重复输出）。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._raw_call(payload)
            except Exception as exc:
                if isinstance(exc, ProviderError):
                    error = exc
                elif isinstance(exc, openai.OpenAIError):
                    error = translate_exception(exc)
                else:
                    raise

                if not error.retryable or attempt > self.max_retries:
                    self._logger.warning(
                        "流式调用失败（第 %d 次尝试，%s，不再重试）：%s",
                        attempt,
                        type(error).__name__,
                        error,
                    )
                    raise error from exc
                delay = self._delay_for(attempt, error)
                self._logger.warning(
                    "流式调用失败（第 %d 次尝试，%s），%.2fs 后重试：%s",
                    attempt,
                    type(error).__name__,
                    delay,
                    error,
                )
                self._sleeper(delay)

    def build_payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.model_config.model,
            "messages": [dict(message) for message in messages],
            "temperature": (
                self.model_config.temperature if temperature is None else temperature
            ),
            "max_tokens": self.model_config.max_tokens if max_tokens is None else max_tokens,
            "stream": bool(stream),
        }
        if stream:
            # 让 OpenAI / DashScope 在最后一个 chunk 里带回 usage；不支持的服务商忽略该字段
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    # ---- 唯一的 HTTP 出口（测试用子类替换它即可） ------------------------
    def _raw_call(self, payload: dict[str, Any]) -> Any:
        """纯传输层：发出请求，成功则原样返回 SDK 响应，失败则原样抛出 SDK 异常。

        这里**不做**错误分类：分类与退避全部由 ``_call_with_retry`` 负责，
        这样替换本方法就能用真实 SDK 异常验证外层策略，不会绕过分类逻辑。
        """
        return self._client.chat.completions.create(**payload)

    # ---- 重试 -----------------------------------------------------------
    def _call_with_retry(self, payload: dict[str, Any]) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._raw_call(payload)
            except Exception as exc:
                if isinstance(exc, ProviderError):
                    error = exc
                elif isinstance(exc, openai.OpenAIError):
                    error = translate_exception(exc)
                else:
                    raise  # 非 SDK 异常（自己代码的 bug）原样抛出，不伪装成 provider 错误

                if not error.retryable or attempt > self.max_retries:
                    self._logger.warning(
                        "调用失败（第 %d 次尝试，%s，不再重试）：%s",
                        attempt,
                        type(error).__name__,
                        error,
                    )
                    raise error from exc
                delay = self._delay_for(attempt, error)
                self._logger.warning(
                    "调用失败（第 %d 次尝试，%s），%.2fs 后重试（剩余重试次数 %d）：%s",
                    attempt,
                    type(error).__name__,
                    delay,
                    self.max_retries - attempt + 1,
                    error,
                )
                self._sleeper(delay)

    def _delay_for(self, attempt: int, exc: ProviderError) -> float:
        """指数退避：base * 2^(attempt-1)，带 jitter，受 Retry-After 与上限约束。"""
        if exc.retry_after is not None:
            return min(float(exc.retry_after), self._backoff_max)
        delay = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_max)
        if self._jitter:
            delay = self._rng.uniform(delay / 2.0, delay)
        return delay

    # ---- 解析 -----------------------------------------------------------
    @staticmethod
    def _parse_response(response: Any) -> ChatResult:
        choices = getattr(response, "choices", None)
        if not choices:
            raise ProviderResponseError("响应中没有 choices 字段或为空")

        message = getattr(choices[0], "message", None)
        if message is None:
            raise ProviderResponseError("响应 choices[0] 中没有 message")

        content = getattr(message, "content", None)
        if content is not None and not isinstance(content, str):
            content = str(content)
        content = content or None

        tool_calls = Provider._parse_tool_calls(getattr(message, "tool_calls", None))
        if content is None and not tool_calls:
            raise ProviderResponseError("响应既没有 content 也没有 tool_calls")

        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            usage=Usage.from_raw(getattr(response, "usage", None)),
        )

    @staticmethod
    def _parse_tool_calls(raw_calls: Iterable[Any] | None) -> tuple[ToolCall, ...]:
        parsed: list[ToolCall] = []
        for index, raw in enumerate(raw_calls or ()):
            function = getattr(raw, "function", None)
            name = getattr(function, "name", None)
            if not name:
                raise ProviderResponseError(f"第 {index} 个 tool_call 缺少 function.name")
            arguments = getattr(function, "arguments", None)
            if arguments is None:
                arguments = "{}"
            elif not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            call_id = getattr(raw, "id", None) or f"call_{index}"
            parsed.append(ToolCall(id=str(call_id), name=str(name), arguments=arguments))
        return tuple(parsed)

    # ---- 便捷调试 -------------------------------------------------------
    def describe(self) -> str:
        """用于 ``config check`` / ``models list`` 的安全摘要（不含密钥）。"""
        model = self.model_config
        return (
            f"model={model.model} base_url={model.base_url} "
            f"api_key_env={model.api_key_env} timeout={self.request_timeout}s "
            f"max_retries={self.max_retries}"
        )