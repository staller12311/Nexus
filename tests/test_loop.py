"""阶段 5：主循环测试。

覆盖 6 条硬约束：
1. 历史按「消息组」裁剪（不产生孤立 tool 消息，system 不计入且永远保留）；
2. 多个 tool_calls 按 id 顺序回填，content 必须是 str；
3. assistant 消息格式严格（content 字段在、arguments 是原始字符串）；
4. max_tool_rounds 达到上限时历史合法且提示正确；
5. usage 每轮累加并落库；
6. 端到端脚本化多轮在 tests/test_mock_server.py。
另加：工具抛异常/参数非法时不崩溃，错误文本回填给模型。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from agent.config import build_config
from agent.loop import AgentLoop, LoopResult, trim_history
from agent.provider import ChatResult, ToolCall, Usage
from agent.registry import ERROR_PREFIX, ToolRegistry, ToolSpec, build_registry
from agent.storage import Storage
from agent.tools import ToolError


# ------------------------------------------------------------------ 工具
def fake_provider(script: Sequence[ChatResult | Exception], **attributes: Any):
    """最小可用的 provider 替身：按脚本依次返回，并记录每次请求。"""

    class FakeProvider:
        def __init__(self) -> None:
            self.script = list(script)
            self.requests: list[dict[str, Any]] = []
            for key, value in attributes.items():
                setattr(self, key, value)

        @property
        def model_name(self) -> str:
            return "fake-model"

        def chat(self, messages, tools=None, **kwargs: Any) -> ChatResult:
            self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
            if not self.script:
                raise AssertionError("provider 脚本已用尽（请求次数超预期）")
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        def chat_stream(self, messages, tools=None, **kwargs: Any):
            """把同一个脚本转成流式事件（loop.run() 现在内部走流式核心）。"""
            result = self.chat(messages, tools=tools, **kwargs)
            if result.content:
                yield {"type": "text", "text": result.content}
            yield {
                "type": "done",
                "content": result.content,
                "reasoning": None,
                "tool_calls": result.tool_calls,
                "usage": result.usage,
            }

    return FakeProvider()


def tool_result(*calls: ToolCall, content: str | None = None, usage: Usage | None = None):
    return ChatResult(content=content, tool_calls=tuple(calls), usage=usage or Usage())


def final(content: str, usage: Usage | None = None) -> ChatResult:
    return ChatResult(content=content, usage=usage or Usage())


def make_registry(tmp_path: Path, *specs: ToolSpec, enabled: Sequence[str] = ()) -> ToolRegistry:
    if specs:
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        return registry
    config = build_config(
        {
            "default_model": "m",
            "models": {
                "m": {
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "test-model",
                    "api_key_env": "LOOP_TEST_KEY",
                }
            },
            "agent": {"workspace": str(tmp_path / "workspace")},
            "tools": {"enabled": list(enabled) or ["read_file", "write_file", "list_dir"]},
        }
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return build_registry(config)


def string_schema(properties: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": [],
        "additionalProperties": False,
    }


def echo_spec(name: str = "echo") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="回显参数",
        parameters=string_schema({"text": {"type": "string", "description": "文本"}}),
        handler=lambda text="": f"echo:{text}",
    )


# 用户给的序列：[system, user, assistant(tc×2), tool, tool, assistant, user]
FULL_SEQUENCE = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "第一个问题"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": "{}"}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "r1"},
    {"role": "tool", "tool_call_id": "c2", "name": "echo", "content": "r2"},
    {"role": "assistant", "content": "第二个回答"},
    {"role": "user", "content": "第二个问题"},
]


def assert_no_orphan_tool_messages(messages: Sequence[Mapping[str, Any]]) -> None:
    seen_call_ids: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                seen_call_ids.add(call["id"])
        elif role == "tool":
            assert message.get("tool_call_id") in seen_call_ids, (
                f"出现没有前驱 assistant(tool_calls) 的 tool 消息：{message}"
            )
            assert isinstance(message.get("content"), str)


# ============================================================ 1. 按组裁剪
class TestTrimHistory:
    def test_user_sequence_with_limit_three(self) -> None:
        trimmed = trim_history(FULL_SEQUENCE, 3)
        assert trimmed[0] == {"role": "system", "content": "系统提示"}
        assert trimmed[1:] == [
            {"role": "assistant", "content": "第二个回答"},
            {"role": "user", "content": "第二个问题"},
        ]
        assert_no_orphan_tool_messages(trimmed)

    def test_group_is_kept_whole_when_it_fits(self) -> None:
        trimmed = trim_history(FULL_SEQUENCE, 5)
        # assistant(tool_calls) + 两条 tool 必须一起进来
        roles = [message["role"] for message in trimmed]
        assert roles == ["system", "assistant", "tool", "tool", "assistant", "user"]
        assert_no_orphan_tool_messages(trimmed)

    def test_never_splits_a_tool_group(self) -> None:
        for limit in range(1, 9):
            trimmed = trim_history(FULL_SEQUENCE, limit)
            assert_no_orphan_tool_messages(trimmed)
            assert trimmed[0]["role"] == "system", "system 必须永远保留"
            body = trimmed[1:]
            assert len(body) <= max(limit, 3), f"limit={limit} 时正文不应超过上限太多"

    def test_system_is_pinned_and_not_counted(self) -> None:
        messages = [
            {"role": "system", "content": "s1"},
            {"role": "system", "content": "s2"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        trimmed = trim_history(messages, 1)
        assert [m["role"] for m in trimmed] == ["system", "system", "assistant"]

    def test_limit_larger_than_history_keeps_everything(self) -> None:
        assert trim_history(FULL_SEQUENCE, 100) == FULL_SEQUENCE

    def test_degenerate_limit_keeps_last_group(self) -> None:
        """最后一组本身就超限时，宁可略微超限也不能发出只有 system 的空请求。"""
        trimmed = trim_history(FULL_SEQUENCE, 1)
        assert [m["role"] for m in trimmed] == ["system", "user"]
        assert_no_orphan_tool_messages(trimmed)

    def test_orphan_tool_message_is_dropped(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "tool", "tool_call_id": "ghost", "content": "孤立"},
            {"role": "user", "content": "u"},
        ]
        trimmed = trim_history(messages, 10)
        assert [m["role"] for m in trimmed] == ["system", "user"]


# ============================================================ 2/3. 回填格式
class TestMessageShapes:
    def test_multiple_tool_calls_are_filled_in_order_by_id(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider(
            [
                tool_result(
                    ToolCall("call_a", "echo", json.dumps({"text": "A"})),
                    ToolCall("call_b", "echo", json.dumps({"text": "B"})),
                ),
                final("都做完了"),
            ]
        )
        loop = AgentLoop(provider, registry, system_prompt="sys")
        result = loop.run("执行两个工具")

        assert result.content == "都做完了"
        second_request = provider.requests[1]["messages"]
        tool_messages = [m for m in second_request if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b"]
        assert [m["content"] for m in tool_messages] == ["echo:A", "echo:B"]
        assert all(isinstance(m["content"], str) for m in tool_messages)

        assistant = [m for m in second_request if m["role"] == "assistant"][0]
        assert [c["id"] for c in assistant["tool_calls"]] == ["call_a", "call_b"]

    def test_assistant_message_shape_is_strict(self) -> None:
        result = ChatResult(
            content=None,
            tool_calls=(ToolCall("call_1", "read_file", '{"path":   "a.txt"}'),),
        )
        message = AgentLoop.build_assistant_message(result)
        assert message["role"] == "assistant"
        assert "content" in message and message["content"] is None
        assert message["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":   "a.txt"}'},
            }
        ]

    def test_arguments_string_is_passed_through_verbatim(self) -> None:
        """原始字符串不能被重新序列化：空白与转义必须原样保留。"""
        raw = '{ "text" : "换行\\n与\\"引号\\"",\n  "unicode":"\\u4e2d\\u6587" }'
        result = ChatResult(content=None, tool_calls=(ToolCall("c", "echo", raw),))
        message = AgentLoop.build_assistant_message(result)
        assert message["tool_calls"][0]["function"]["arguments"] == raw
        # 确认重新序列化确实会改变字符串（说明这条约束不是多余的）
        assert json.dumps(json.loads(raw), ensure_ascii=False) != raw

    def test_no_tool_calls_means_no_tool_calls_key(self) -> None:
        message = AgentLoop.build_assistant_message(final("普通回答"))
        assert message == {"role": "assistant", "content": "普通回答"}
        assert "tool_calls" not in message

    def test_tool_message_rejects_non_string_output(self) -> None:
        with pytest.raises(TypeError):
            AgentLoop.build_tool_message(ToolCall("c", "echo", "{}"), {"not": "str"})  # type: ignore[arg-type]

    def test_dict_tool_result_is_json_serialized(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="dict_tool",
                description="返回 dict",
                parameters=string_schema(),
                handler=lambda: {"a": 1, "b": [1, 2]},
            )
        )
        provider = fake_provider(
            [tool_result(ToolCall("c1", "dict_tool", "{}")), final("ok")]
        )
        loop = AgentLoop(provider, registry)
        loop.run("go")
        tool_message = [
            m for m in provider.requests[1]["messages"] if m["role"] == "tool"
        ][0]
        assert tool_message["content"] == '{"a": 1, "b": [1, 2]}'
        assert isinstance(tool_message["content"], str)


# ============================================================ 2/3. 专用用例
def test_two_tool_calls_are_not_swapped_by_id(tmp_path: Path) -> None:
    """要求 2 专用：一次响应返回两个 tool_call（id = a、b）。

    断言：两条都在、顺序是 a→b、tool_call_id 一一对应，
    且 **a 拿到 a 的结果、b 拿到 b 的结果**（防止真实模型下「结果张冠李戴」）。
    """
    registry = ToolRegistry()
    registry.register(echo_spec())
    provider = fake_provider(
        [
            tool_result(
                ToolCall("a", "echo", json.dumps({"text": "AAA"})),
                ToolCall("b", "echo", json.dumps({"text": "BBB"})),
            ),
            final("两个都做完了"),
        ]
    )
    loop = AgentLoop(provider, registry, system_prompt="sys")
    result = loop.run("并行执行 a 和 b")

    assert result.content == "两个都做完了"

    second_request = provider.requests[1]["messages"]
    assistant = [m for m in second_request if m["role"] == "assistant"][0]
    assert [call["id"] for call in assistant["tool_calls"]] == ["a", "b"]

    tool_messages = [m for m in second_request if m["role"] == "tool"]
    assert len(tool_messages) == 2, "两个 tool_call 都必须被回填"
    assert [m["tool_call_id"] for m in tool_messages] == ["a", "b"], (
        "回填顺序必须与 tool_calls 一致"
    )
    assert [m["content"] for m in tool_messages] == ["echo:AAA", "echo:BBB"]
    # 逐条配对：id → 结果，串号会在这里立刻暴露
    assert {m["tool_call_id"]: m["content"] for m in tool_messages} == {
        "a": "echo:AAA",
        "b": "echo:BBB",
    }


def test_arguments_are_passed_verbatim_to_next_request(tmp_path: Path) -> None:
    """要求 3 专用：模型给的 arguments 必须逐字符原样出现在下一轮请求里。

    取 ``'{"x": 1, "y":2}'``（``x`` 后有双空格、``y`` 后无空格）：
    任何「解析再序列化」都会把它变成 ``'{"x": 1, "y": 2}'``。
    """
    raw = '{"x": 1, "y":2}'
    registry = ToolRegistry()
    registry.register(echo_spec())
    storage = Storage(tmp_path / "sessions.db")
    session = storage.create_session("fake-model")
    provider = fake_provider([tool_result(ToolCall("a", "echo", raw)), final("done")])
    loop = AgentLoop(provider, registry, storage=storage, session_id=session.id)
    loop.run("原样透传")

    # 1) 下一轮请求里的 assistant 消息
    second_request = provider.requests[1]["messages"]
    assistant = [m for m in second_request if m["role"] == "assistant"][0]
    sent = assistant["tool_calls"][0]["function"]["arguments"]
    assert sent == raw
    assert list(sent) == list(raw), "必须逐字符相等"
    assert assistant["content"] is None and "content" in assistant

    # 2) 证明这条约束不是多余的：重新序列化确实会改变字符串
    assert json.dumps(json.loads(raw)) == '{"x": 1, "y": 2}'
    assert json.dumps(json.loads(raw)) != raw

    # 3) 内存历史与落库读回都不能变形
    assert loop.messages[1]["tool_calls"][0]["function"]["arguments"] == raw
    stored = storage.get_messages(session.id)
    stored_assistant = [m for m in stored if m["role"] == "assistant"][0]
    assert stored_assistant["tool_calls"][0]["function"]["arguments"] == raw
    storage.close()


# ============================================================ 4. 轮数上限
class TestToolRoundLimit:
    def test_limit_notice_and_valid_history(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        # 模型永远返回 tool_calls，必须在上限处停下
        script = [
            tool_result(ToolCall(f"c{i}", "echo", '{"text": "x"}')) for i in range(10)
        ]
        provider = fake_provider(script)
        loop = AgentLoop(provider, registry, system_prompt="sys", max_tool_rounds=2)

        result = loop.run("无限循环吧")

        assert result.tool_limit_reached is True
        assert result.finish == "tool_limit"
        assert result.rounds == 2
        assert result.content == "已达工具调用上限（2 轮），已停止继续调用工具。"
        assert len(provider.requests) == 2, "第 N+1 轮请求不应该发出"
        assert_no_orphan_tool_messages(loop.messages)

        last = loop.messages[-1]
        assert last["role"] == "tool", "退出时历史末尾应是已回填的 tool 消息"
        assistant_before = loop.messages[-2]
        assert assistant_before["role"] == "assistant"
        assert assistant_before["tool_calls"]
        assert assistant_before["tool_calls"][0]["id"] == last["tool_call_id"]

    def test_no_truncated_tool_call_round(self, tmp_path: Path) -> None:
        """不允许出现「assistant 带 tool_calls 但 tool 没回填」就退出。"""
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider(
            [tool_result(ToolCall("c1", "echo", "{}")) for _ in range(4)]
        )
        loop = AgentLoop(provider, registry, max_tool_rounds=3)
        loop.run("go")

        for index, message in enumerate(loop.messages):
            if message["role"] == "assistant" and message.get("tool_calls"):
                expected = {call["id"] for call in message["tool_calls"]}
                actual = {
                    m["tool_call_id"]
                    for m in loop.messages[index + 1 : index + 1 + len(expected)]
                    if m["role"] == "tool"
                }
                assert actual == expected, "每个 tool_call 都必须被回填"

    def test_single_round_limit(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider(
            [tool_result(ToolCall("c1", "echo", "{}")), final("不该到这里")]
        )
        loop = AgentLoop(provider, registry, max_tool_rounds=1)
        result = loop.run("go")
        assert result.rounds == 1
        assert result.tool_limit_reached is True
        assert len(provider.requests) == 1


# ============================================================ 5. usage 累加
class TestUsageAccumulation:
    def test_usage_sums_across_tool_rounds(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider(
            [
                tool_result(
                    ToolCall("c1", "echo", "{}"), usage=Usage(10, 5, 15)
                ),
                tool_result(
                    ToolCall("c2", "echo", "{}"), usage=Usage(20, 6, 26)
                ),
                final("结束", usage=Usage(30, 7, 37)),
            ]
        )
        loop = AgentLoop(provider, registry)
        result = loop.run("三轮")

        assert result.rounds == 2
        assert result.usage == Usage(60, 18, 78)
        assert result.usage == Usage(10, 5, 15) + Usage(20, 6, 26) + Usage(30, 7, 37)

    def test_usage_is_persisted_per_round(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        storage = Storage(tmp_path / "sessions.db")
        session = storage.create_session("fake-model")
        provider = fake_provider(
            [
                tool_result(ToolCall("c1", "echo", "{}"), usage=Usage(10, 5, 15)),
                final("结束", usage=Usage(2, 3, 5)),
            ]
        )
        loop = AgentLoop(provider, registry, storage=storage, session_id=session.id)
        result = loop.run("go")

        record = storage.require_session(session.id)
        assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (12, 8, 20)
        assert result.usage == Usage(12, 8, 20)
        storage.close()

    def test_session_usage_accumulates_across_runs(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        storage = Storage(tmp_path / "sessions.db")
        session = storage.create_session("fake-model")
        provider = fake_provider([final("一", usage=Usage(1, 1, 2)), final("二", usage=Usage(3, 4, 7))])
        loop = AgentLoop(provider, registry, storage=storage, session_id=session.id)

        assert loop.run("第一次").usage == Usage(1, 1, 2)
        assert loop.run("第二次").usage == Usage(3, 4, 7)
        record = storage.require_session(session.id)
        assert record.total_tokens == 9
        assert record.prompt_tokens == 4
        assert record.completion_tokens == 5
        storage.close()


# ============================================================ 工具失败不崩
class TestToolFailuresAreFedBack:
    def test_tool_exception_becomes_error_text(self) -> None:
        registry = ToolRegistry()

        def boom(**kwargs: Any) -> str:
            raise ToolError("磁盘炸了")

        registry.register(
            ToolSpec("boom", "会失败", string_schema(), boom)
        )
        provider = fake_provider([tool_result(ToolCall("c1", "boom", "{}")), final("我看到错误了")])
        loop = AgentLoop(provider, registry)
        result = loop.run("试试")

        assert result.content == "我看到错误了"
        tool_message = [m for m in provider.requests[1]["messages"] if m["role"] == "tool"][0]
        assert tool_message["content"].startswith(ERROR_PREFIX)
        assert "磁盘炸了" in tool_message["content"]
        assert result.rounds == 1

    def test_unexpected_exception_becomes_error_text(self) -> None:
        registry = ToolRegistry()

        def crash(**kwargs: Any) -> str:
            raise ZeroDivisionError("内部 bug")

        registry.register(ToolSpec("crash", "内部异常", string_schema(), crash))
        provider = fake_provider([tool_result(ToolCall("c1", "crash", "{}")), final("继续")])
        loop = AgentLoop(provider, registry)
        assert loop.run("go").content == "继续"
        tool_message = [m for m in provider.requests[1]["messages"] if m["role"] == "tool"][0]
        assert "内部异常" in tool_message["content"]

    def test_invalid_json_arguments_are_blocked_locally(self) -> None:
        """非法 arguments 绝不能原样回填历史：上游会 400，模型会疯狂重试。"""
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider(
            [tool_result(ToolCall("c1", "echo", "{not json")), final("我改正了")]
        )
        loop = AgentLoop(provider, registry)
        assert loop.run("go").content == "我改正了"

        messages = provider.requests[1]["messages"]
        tool_message = [m for m in messages if m["role"] == "tool"][0]
        assert "不是合法 JSON" in tool_message["content"]
        assert "请重新生成" in tool_message["content"]
        # 回填/发给上游的 arguments 必须能通过 json.loads（否则 400）
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {}
        assert_no_orphan_tool_messages(loop.messages)

    def test_only_invalid_call_is_blocked(self) -> None:
        """一次返回多个 tool_call 时，合法调用照常执行，非法调用单独拦截。"""
        executed: list[str] = []

        def echo(text: str = "") -> str:
            executed.append(text)
            return f"echo:{text}"

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="echo",
                description="回显",
                parameters=string_schema(
                    {"text": {"type": "string", "description": "文本"}}
                ),
                handler=echo,
            )
        )
        provider = fake_provider(
            [
                tool_result(
                    ToolCall("good", "echo", '{"text": "ok"}'),
                    ToolCall("bad", "echo", '{"text": "unterminated}'),
                ),
                final("完成"),
            ]
        )
        loop = AgentLoop(provider, registry)
        assert loop.run("go").content == "完成"

        assert executed == ["ok"]
        messages = provider.requests[1]["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        parsed = [
            json.loads(call["function"]["arguments"])
            for call in assistant["tool_calls"]
        ]
        assert parsed[0] == {"text": "ok"}
        assert parsed[1] == {}
        tool_messages = [m for m in messages if m["role"] == "tool"]
        assert tool_messages[0]["content"] == "echo:ok"
        assert "不是合法 JSON" in tool_messages[1]["content"]
        assert_no_orphan_tool_messages(loop.messages)

    def test_unknown_tool_becomes_error_text(self) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider([tool_result(ToolCall("c1", "nope", "{}")), final("好的")])
        loop = AgentLoop(provider, registry)
        loop.run("go")
        tool_message = [m for m in provider.requests[1]["messages"] if m["role"] == "tool"][0]
        assert "未知工具" in tool_message["content"]


# ============================================================ 其他
class TestLoopMisc:
    def test_system_prompt_is_first_and_persisted(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        storage = Storage(tmp_path / "sessions.db")
        session = storage.create_session("fake-model")
        provider = fake_provider([final("好")])
        loop = AgentLoop(
            provider, registry, system_prompt="你是助手", storage=storage, session_id=session.id
        )
        loop.run("你好")

        assert loop.messages[0] == {"role": "system", "content": "你是助手"}
        stored = storage.get_messages(session.id)
        assert [m["role"] for m in stored] == ["system", "user", "assistant"]
        storage.close()

    def test_system_prompt_is_not_reordered_for_existing_session(self, tmp_path: Path) -> None:
        """已有消息的会话不会被追加一条 system（那会让它有两条 system 且顺序错乱），
        但发给模型的请求里依然带 system。"""
        storage = Storage(tmp_path / "sessions.db")
        session = storage.create_session("fake-model")
        storage.append_message(session.id, {"role": "user", "content": "旧问题"})
        storage.append_message(session.id, {"role": "assistant", "content": "旧回答"})

        registry = ToolRegistry()
        provider = fake_provider([final("新回答")])
        loop = AgentLoop(
            provider, registry, system_prompt="补上的提示词", storage=storage, session_id=session.id
        )
        loop.run("新问题")

        assert loop.messages[0] == {"role": "system", "content": "补上的提示词"}
        assert provider.requests[0]["messages"][0]["role"] == "system"

        stored_roles = [m["role"] for m in storage.get_messages(session.id)]
        assert stored_roles == ["user", "assistant", "user", "assistant"], "不应往库里插 system"
        storage.close()

    def test_resume_does_not_duplicate_history(self, tmp_path: Path) -> None:
        existing = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
        ]
        registry = ToolRegistry()
        registry.register(echo_spec())
        provider = fake_provider([final("新回答")])
        loop = AgentLoop(provider, registry, system_prompt="不该被插入", history=existing)
        loop.run("新问题")

        assert [m["role"] for m in loop.messages] == [
            "system",
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert loop.messages[0]["content"] == "sys"

    def test_tools_are_only_sent_when_registry_is_not_empty(self) -> None:
        empty = ToolRegistry()
        provider = fake_provider([final("无工具")])
        AgentLoop(provider, empty).run("hi")
        assert provider.requests[0]["tools"] is None

        registry = ToolRegistry()
        registry.register(echo_spec())
        provider2 = fake_provider([final("有工具")])
        AgentLoop(provider2, registry).run("hi")
        assert provider2.requests[0]["tools"][0]["function"]["name"] == "echo"

    def test_empty_user_input_is_rejected(self) -> None:
        registry = ToolRegistry()
        provider = fake_provider([final("x")])
        with pytest.raises(ValueError):
            AgentLoop(provider, registry).run("   ")

    def test_request_messages_uses_trimmed_history(self) -> None:
        registry = ToolRegistry()
        registry.register(echo_spec())
        history = [{"role": "system", "content": "sys"}] + [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
            for index in range(6)
        ]
        provider = fake_provider([final("ok")])
        loop = AgentLoop(provider, registry, max_history_messages=4, history=history)
        loop.run("最后")
        request = provider.requests[0]["messages"]
        assert request[0]["role"] == "system"
        assert len(request) == 5  # system + 最近 4 条
        assert request[-1] == {"role": "user", "content": "最后"}

    def test_from_config_wires_settings(self, tmp_path: Path) -> None:
        config = build_config(
            {
                "default_model": "m",
                "models": {
                    "m": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "test-model",
                        "api_key_env": "LOOP_TEST_KEY",
                    }
                },
                "agent": {
                    "workspace": str(tmp_path / "workspace"),
                    "system_prompt": "来自配置",
                    "max_tool_rounds": 3,
                    "max_history_messages": 7,
                },
            }
        )
        registry = ToolRegistry()
        provider = fake_provider([final("ok")])
        loop = AgentLoop.from_config(config, provider, registry)
        assert loop.max_tool_rounds == 3
        assert loop.max_history_messages == 7
        assert loop.messages[0] == {"role": "system", "content": "来自配置"}

    def test_loop_result_is_frozen(self) -> None:
        result = LoopResult(content="x", usage=Usage(), rounds=0)
        with pytest.raises(Exception):
            result.content = "y"  # type: ignore[misc]

# ============================================================ 7. 人工审批
def _approval_config(tmp_path: Path, *, require: bool) -> Any:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return build_config(
        {
            "default_model": "m",
            "models": {
                "m": {
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "test-model",
                    "api_key_env": "LOOP_TEST_KEY",
                }
            },
            "agent": {"workspace": str(tmp_path / "workspace")},
            "tools": {
                "enabled": ["write_file"],
                "require_approval": require,
                "approval_required": ["write_file"],
            },
        }
    )


def test_approval_reject_does_not_execute_tool(tmp_path: Path) -> None:
    """拒绝：工具不执行，但 tool 结果照常回填（历史合法，模型能解释）。"""
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")

    executed: list[str] = []

    def handler(path: str = "") -> str:
        executed.append(path)
        return "should-not-run"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="write_file",
            description="写文件",
            parameters=string_schema(
                {"path": {"type": "string"}, "content": {"type": "string"}}
            ),
            handler=handler,
        )
    )

    calls: list[ToolCall] = []

    def approval(call: ToolCall) -> bool:
        calls.append(call)
        return False

    provider = fake_provider(
        [
            tool_result(ToolCall("c1", "write_file", '{"path": "a.txt", "content": "hi"}')),
            final("好的，我就不写了。"),
        ]
    )
    loop = AgentLoop(
        provider,
        registry,
        storage=storage,
        session_id=session.id,
        approval_handler=approval,
        approval_required={"write_file"},
    )

    result = loop.run("写个文件")

    assert executed == []  # 没有真正执行
    assert len(calls) == 1 and calls[0].name == "write_file"
    assert result.content == "好的，我就不写了。"
    tool_messages = [m for m in loop.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"] == "用户已拒绝此操作，工具未执行。"
    storage.close()


def test_approval_accept_executes_tool(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")

    decisions: list[bool] = []

    def approval(call: ToolCall) -> bool:
        decisions.append(True)
        return True

    config = _approval_config(tmp_path, require=True)
    registry = build_registry(config)
    provider = fake_provider(
        [
            tool_result(
                ToolCall("c1", "write_file", '{"path": "out.txt", "content": "hello"}')
            ),
            final("写好了。"),
        ]
    )
    loop = AgentLoop.from_config(
        config,
        provider,
        registry,
        storage=storage,
        session_id=session.id,
        approval_handler=approval,
    )
    loop.run("写文件")
    assert decisions == [True]
    assert (tmp_path / "workspace" / "out.txt").read_text(encoding="utf-8") == "hello"
    storage.close()


def test_approval_disabled_does_not_call_handler(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")

    called: list[ToolCall] = []

    def approval(call: ToolCall) -> bool:
        called.append(call)
        return False

    config = _approval_config(tmp_path, require=False)
    registry = build_registry(config)
    provider = fake_provider(
        [
            tool_result(
                ToolCall("c1", "write_file", '{"path": "auto.txt", "content": "no-ask"}')
            ),
            final("写好了。"),
        ]
    )
    loop = AgentLoop.from_config(
        config,
        provider,
        registry,
        storage=storage,
        session_id=session.id,
        approval_handler=approval,
    )
    loop.run("写文件")
    assert called == []  # require_approval=false：不触发审批
    assert (tmp_path / "workspace" / "auto.txt").read_text(encoding="utf-8") == "no-ask"
    storage.close()


def test_non_approval_tool_skips_handler(tmp_path: Path) -> None:
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")
    called: list[ToolCall] = []
    registry = ToolRegistry()
    registry.register(echo_spec())
    provider = fake_provider(
        [tool_result(ToolCall("c1", "echo", '{"text": "hi"}')), final("完成")]
    )
    loop = AgentLoop(
        provider,
        registry,
        storage=storage,
        session_id=session.id,
        approval_handler=lambda call: called.append(call) or False,
        approval_required={"write_file"},
    )
    loop.run("随便")
    assert called == []
    storage.close()

# ============================================================ 8. 上下文管理
def test_large_write_file_arguments_are_compacted_in_requests(tmp_path: Path) -> None:
    """大段代码已经落盘：后续请求的 assistant tool_calls 参数应压缩成占位 JSON。"""
    (tmp_path / "workspace").mkdir(exist_ok=True)
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")

    config = _approval_config(tmp_path, require=False)
    registry = build_registry(config)
    big_content = "const x = 1;\n" * 400  # ~5000 字符
    arguments = json.dumps({"path": "app.js", "content": big_content})
    provider = fake_provider(
        [tool_result(ToolCall("c1", "write_file", arguments)), final("写好了")]
    )
    loop = AgentLoop.from_config(
        config, provider, registry, storage=storage, session_id=session.id
    )
    loop.run("写一个 js 文件")

    second = provider.requests[1]["messages"]
    assistant = [m for m in second if m["role"] == "assistant"][0]
    sent_arguments = assistant["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(sent_arguments)
    assert parsed["path"] == "app.js"
    assert "历史中省略" in parsed["content"]
    assert len(sent_arguments) < 300, "大段代码不应再随历史发给模型"
    # 磁盘上的文件内容完整，只是历史请求里不再回显
    assert (tmp_path / "workspace" / "app.js").read_text(encoding="utf-8") == big_content
    storage.close()


def test_history_summary_replaces_old_messages_instead_of_trimming(tmp_path: Path) -> None:
    """超过 80% 窗口时：最旧的 10 条被压成一条摘要写进 system，而不是直接丢弃。"""
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")

    history: list[dict[str, Any]] = []
    for index in range(6):
        history.append({"role": "user", "content": f"第 {index} 轮旧需求：" + "贪吃蛇" * 80})
        history.append(
            {"role": "assistant", "content": f"第 {index} 轮旧回答：" + "已经处理" * 80}
        )

    transcripts: list[str] = []

    def summarizer(transcript: str) -> str:
        transcripts.append(transcript)
        return "用户之前要求写贪吃蛇，模型遇到 JSON 报错，最后修正了文件。"

    provider = fake_provider([final("继续")])
    loop = AgentLoop(
        provider,
        ToolRegistry(),
        system_prompt="你是助手。",
        storage=storage,
        session_id=session.id,
        history=history,
        context_window=500,
        summarizer=summarizer,
        context_summary_trigger=0.8,
        context_summary_messages=10,
    )
    loop.run("继续完善它")

    assert len(transcripts) == 1
    assert "贪吃蛇" in transcripts[0]
    sent = provider.requests[0]["messages"]
    assert "【历史记忆摘要】" in sent[0]["content"]
    assert "贪吃蛇" in sent[0]["content"]
    assert len(sent) < len(history) + 2
    # 最近两轮必须保留
    assert any("第 5 轮" in str(m.get("content") or "") for m in sent)
    storage.close()


def test_summary_failure_falls_back_to_trimming(tmp_path: Path) -> None:
    """摘要器异常时不能崩：退回旧的按条数裁剪路径。"""
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")
    history = [
        {"role": "user", "content": "旧需求" * 100},
        {"role": "assistant", "content": "旧回答" * 100},
        {"role": "user", "content": "旧需求 2" * 100},
        {"role": "assistant", "content": "旧回答 2" * 100},
    ]

    def broken_summarizer(transcript: str) -> str:
        raise RuntimeError("后台摘要模型挂了")

    provider = fake_provider([final("继续")])
    loop = AgentLoop(
        provider,
        ToolRegistry(),
        system_prompt="你是助手。",
        storage=storage,
        session_id=session.id,
        history=history,
        context_window=100,
        summarizer=broken_summarizer,
        max_history_messages=2,
    )
    assert loop.run("继续").content == "继续"
    # 摘要失败 → 退回按条数裁剪（system + 最近 2 条）
    assert len(provider.requests[0]["messages"]) <= 3
    storage.close()


def test_summary_emits_summary_notice_event(tmp_path: Path) -> None:
    """摘要成功时，run_stream 必须产出 summary_notice，而不是静默压缩。"""
    session = Storage(tmp_path / "s.db").create_session("m")
    storage = Storage(tmp_path / "s.db")
    history: list[dict[str, Any]] = []
    for index in range(6):
        history.append({"role": "user", "content": f"第 {index} 轮需求：" + "长文本" * 80})
        history.append({"role": "assistant", "content": "回答" * 80})

    provider = fake_provider([final("继续")])
    loop = AgentLoop(
        provider,
        ToolRegistry(),
        system_prompt="你是助手。",
        storage=storage,
        session_id=session.id,
        history=history,
        context_window=500,
        summarizer=lambda transcript: "用户之前要求做长任务。",
        context_summary_messages=10,
    )
    events = list(loop.run_stream("继续"))
    notices = [event for event in events if event.get("type") == "summary_notice"]
    assert len(notices) == 1
    notice = notices[0]
    assert notice["rounds"] >= 1
    assert "已自动压缩" in notice["content"]
    assert notice["summary"] == "用户之前要求做长任务。"
    assert any(event.get("type") == "done" for event in events)
    storage.close()


def test_unsafe_shell_forces_approval_even_if_global_list_empty(tmp_path: Path) -> None:
    """allow_unsafe_shell 是独立危险开关：run_shell 必须进入审批集合。"""
    (tmp_path / "workspace").mkdir(exist_ok=True)
    config = build_config(
        {
            "default_model": "m",
            "models": {
                "m": {
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "test-model",
                    "api_key_env": "LOOP_TEST_KEY",
                }
            },
            "agent": {"workspace": str(tmp_path / "workspace")},
            "tools": {
                "enabled": ["run_shell"],
                "allow_unsafe_shell": True,
                "require_approval": True,
                "approval_required": [],
                "run_shell": {"allowed_commands": []},
            },
        }
    )
    loop = AgentLoop.from_config(config, fake_provider([]), build_registry(config))
    assert "run_shell" in loop.approval_required
