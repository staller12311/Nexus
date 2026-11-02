"""循环护栏：原地打转检测 + 假完成检测。

这两个护栏解决的是「循环画对了、但终止条件用错了代理信号」的问题：

- 原来的终止信号只有两个：模型不再要工具（= 它自认为完事）、`max_tool_rounds` 硬上限。
  前者会被「假完成」骗过（说写好了、其实没写），后者在模型打转时要白烧到上限。
- 现在补两条**纯规则**（不增加任何模型调用）：
  ① 连续 N 轮「同样的调用 + 同样的结果」→ 判定打转，立即停；
  ② 声称已落盘但本轮零写入 → 追问一次，让它真正写。

刻意不做的：每轮让模型自评「目标达成了吗」——成本和延迟翻倍，而且模型自评不可靠。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.loop import ARTIFACT_CLAIM_NUDGE, AgentLoop
from agent.provider import ToolCall, Usage
from agent.registry import ToolRegistry, ToolSpec


class ScriptedProvider:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.requests: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "fake"

    def chat_stream(self, messages, tools=None, **kwargs: Any):
        self.requests.append([dict(m) for m in messages])
        step = self.steps.pop(0) if self.steps else {"text": "兜底回答"}
        if step.get("reasoning"):
            yield {"type": "reasoning", "text": step["reasoning"]}
        if step.get("text"):
            yield {"type": "text", "text": step["text"]}
        yield {
            "type": "done",
            "content": step.get("text"),
            "reasoning": step.get("reasoning"),
            "tool_calls": step.get("tool_calls", ()),
            "usage": Usage(),
            "finish_reason": step.get("finish_reason", "stop"),
        }


def _call(name: str = "probe", args: str = "{}", call_id: str = "c1") -> dict[str, Any]:
    return {
        "tool_calls": (ToolCall(id=call_id, name=name, arguments=args),),
        "finish_reason": "tool_calls",
    }


def _spec(name: str, handler) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def _loop(provider, specs, **options) -> AgentLoop:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return AgentLoop(provider, registry, system_prompt="sys", **options)


# ---------------------------------------------------------------- 原地打转
def test_identical_call_and_result_stops_immediately() -> None:
    """同一调用返回同一结果 → 第二轮结束就停，不再多打一次请求。"""
    provider = ScriptedProvider([_call() for _ in range(5)])
    loop = _loop(provider, [_spec("probe", lambda **_: "same")], no_progress_limit=2)

    events = list(loop.run_stream("做点事"))
    done = events[-1]

    assert done["result"].finish == "no_progress"
    assert done["result"].rounds == 2
    assert len(provider.requests) == 2, "判定打转后不该再请求模型"
    assert any(e["type"] == "notice" for e in events)


def test_same_call_with_different_result_is_progress() -> None:
    """失败后重试同一调用（结果变了）不算打转——这是最容易误杀的场景。"""
    counter = {"n": 0}

    def bump(**_):
        counter["n"] += 1
        return f"attempt-{counter['n']}"

    provider = ScriptedProvider([_call(), _call(), {"text": "好了"}])
    loop = _loop(provider, [_spec("probe", bump)], no_progress_limit=2)

    done = list(loop.run_stream("做点事"))[-1]

    assert done["result"].finish == "stop"
    assert done["result"].content == "好了"
    assert len(provider.requests) == 3


def test_no_progress_detection_can_be_disabled() -> None:
    provider = ScriptedProvider([_call() for _ in range(5)])
    loop = _loop(
        provider,
        [_spec("probe", lambda **_: "same")],
        no_progress_limit=0,
        max_tool_rounds=2,
    )

    done = list(loop.run_stream("做点事"))[-1]

    assert done["result"].finish == "tool_limit"
    assert len(provider.requests) == 2


# ---------------------------------------------------------------- 假完成
def test_claim_without_write_triggers_one_nudge() -> None:
    provider = ScriptedProvider(
        [
            {"text": "已创建 index.html，包含完整登录页。"},
            {"text": "抱歉，我现在真正写入。"},
        ]
    )
    loop = _loop(
        provider,
        [_spec("write_file", lambda **_: "ok")],
        verify_artifact_claims=True,
    )

    done = list(loop.run_stream("写个登录页"))[-1]

    assert done["result"].finish == "stop"
    assert len(provider.requests) == 2, "只应追问一次"
    assert provider.requests[1][-1]["role"] == "system"
    assert provider.requests[1][-1]["content"] == ARTIFACT_CLAIM_NUDGE


def test_claim_after_real_write_is_not_nudged() -> None:
    provider = ScriptedProvider(
        [_call(name="write_file", args='{"path": "a.html"}'), {"text": "已写入 a.html"}]
    )
    loop = _loop(provider, [_spec("write_file", lambda **_: "ok")])

    done = list(loop.run_stream("写个页面"))[-1]

    assert done["result"].finish == "stop"
    assert len(provider.requests) == 2, "真写过了就不该追问"


def test_claim_check_can_be_disabled() -> None:
    provider = ScriptedProvider([{"text": "已创建 index.html。"}])
    loop = _loop(
        provider,
        [_spec("write_file", lambda **_: "ok")],
        verify_artifact_claims=False,
    )

    list(loop.run_stream("写个登录页"))

    assert len(provider.requests) == 1


def test_plain_answer_is_not_treated_as_claim() -> None:
    """「已完成分析」这类修辞不该被当成「声称产出文件」。"""
    provider = ScriptedProvider([{"text": "已完成分析，结论是转化率在下降。"}])
    loop = _loop(provider, [_spec("write_file", lambda **_: "ok")])

    list(loop.run_stream("分析一下"))

    assert len(provider.requests) == 1


def test_claim_without_write_tool_is_ignored() -> None:
    """注册表里没有写工具时，追问「为什么没落盘」毫无意义。"""
    provider = ScriptedProvider([{"text": "已创建 index.html。"}])
    loop = _loop(provider, [_spec("read_file", lambda **_: "content")])

    list(loop.run_stream("写个登录页"))

    assert len(provider.requests) == 1


@pytest.mark.parametrize("limit", [-5, 0, 3, 99])
def test_no_progress_limit_is_clamped(limit: int) -> None:
    loop = AgentLoop(
        ScriptedProvider([]), ToolRegistry(), no_progress_limit=limit
    )
    assert 0 <= loop.no_progress_limit <= 10
