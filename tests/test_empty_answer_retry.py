"""空正文补救：思考把输出预算吃光时的那次重试。

背景（真实发生）：把某个推理模型的 max_tokens 从 8192 提到 16384 之后，
思考量跟着涨到 5 万多字，正文依然是空的 —— **单纯调大预算会陷入「预算越大、思考越长」**。
所以 loop 里加了一层补救：追加一条「不要再推理、直接输出结论」的 system 提醒后重跑一次，
只有在 finish_reason=length 且本轮既无正文也无 tool_calls 时才触发。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.loop import EMPTY_ANSWER_NUDGE, AgentLoop
from agent.provider import Usage
from agent.registry import ToolRegistry


class ScriptedProvider:
    """按脚本吐流式事件的最小替身（能控制 finish_reason 与 reasoning）。"""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.requests: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "fake-reasoner"

    def chat_stream(self, messages, tools=None, **kwargs: Any):
        self.requests.append([dict(m) for m in messages])
        if not self.steps:
            raise AssertionError("脚本已用尽（请求次数超出预期）")
        step = self.steps.pop(0)
        if step.get("reasoning"):
            yield {"type": "reasoning", "text": step["reasoning"]}
        if step.get("text"):
            yield {"type": "text", "text": step["text"]}
        yield {
            "type": "done",
            "content": step.get("text"),
            "reasoning": step.get("reasoning"),
            "tool_calls": (),
            "usage": Usage(),
            "finish_reason": step.get("finish_reason", "stop"),
        }


def _loop(provider: ScriptedProvider, **options: Any) -> AgentLoop:
    return AgentLoop(provider, ToolRegistry(), system_prompt="sys", **options)


def _run(loop: AgentLoop, prompt: str = "做一个雨夜便利店微缩模型") -> dict[str, Any]:
    events = list(loop.run_stream(prompt))
    return events[-1]


def test_retries_once_then_returns_real_content() -> None:
    """第一次全在思考 → 补救一次 → 第二次给出正文，整轮成功。"""
    provider = ScriptedProvider(
        [
            {"reasoning": "想了很久" * 1000, "finish_reason": "length"},
            {"reasoning": "略", "text": "最终答案：先给结论……", "finish_reason": "stop"},
        ]
    )
    loop = _loop(provider, empty_answer_retries=1)

    done = _run(loop)

    assert done["result"].content == "最终答案：先给结论……"
    assert done["result"].finish == "stop"
    assert len(provider.requests) == 2, "应该只多打一次请求"
    # 第二次请求末尾必须带上「别再想了」的提醒
    assert provider.requests[1][-1]["role"] == "system"
    assert provider.requests[1][-1]["content"] == EMPTY_ANSWER_NUDGE


def test_gives_up_after_retry_limit_and_explains_why() -> None:
    """两次都只有思考：返回人话提示，且提示里点明「1M 是窗口不是输出」。"""
    provider = ScriptedProvider(
        [
            {"reasoning": "想" * 3000, "finish_reason": "length"},
            {"reasoning": "还在想" * 3000, "finish_reason": "length"},
        ]
    )
    loop = _loop(provider, empty_answer_retries=1)

    done = _run(loop)

    content = done["result"].content or ""
    assert "思考阶段就用完了输出预算" in content
    assert "上下文窗口" in content, "要解释 1M/128K 不是输出上限"
    assert "32768" in content, "要给一个具体的建议数值"
    assert len(provider.requests) == 2


def test_retry_can_be_disabled() -> None:
    """empty_answer_retries=0 时不做补救，只打一次请求。"""
    provider = ScriptedProvider([{"reasoning": "想" * 100, "finish_reason": "length"}])
    loop = _loop(provider, empty_answer_retries=0)

    done = _run(loop)

    assert "没有产出正文" in (done["result"].content or "")
    assert len(provider.requests) == 1


def test_stop_with_empty_content_does_not_trigger_retry() -> None:
    """不是被预算截断（finish_reason=stop）就重试没有意义，不该白花一次请求。"""
    provider = ScriptedProvider([{"reasoning": "想", "finish_reason": "stop"}])
    loop = _loop(provider, empty_answer_retries=1)

    done = _run(loop)

    assert done["result"].content
    assert len(provider.requests) == 1


@pytest.mark.parametrize("rounds", [(0,), (1,), (3,)])
def test_retry_count_is_clamped(rounds: tuple[int]) -> None:
    """配置成负数/超大值都不会失控（内部 clamp 到 0..3）。"""
    loop = AgentLoop(
        ScriptedProvider([]), ToolRegistry(), empty_answer_retries=rounds[0] + 10
    )
    assert 0 <= loop.empty_answer_retries <= 3
