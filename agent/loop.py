"""对话 + 工具调用主循环。

这一层最容易出隐蔽 bug，三条硬约束写在代码里（README「设计决策」第 20–22 条）：

1. **历史按「消息组」裁剪**，不按条数切。OpenAI 要求每条 ``role="tool"`` 前面必须有
   对应的 ``assistant``（带 ``tool_calls``）；按条数从后往前保留会切掉那个 assistant，
   留下孤立 tool 消息，下一次请求直接 400。``trim_history()`` 因此以「组」为单位取舍：
   assistant(tool_calls) + 其后所有兄弟 tool 消息同生共死。system 永远保留且不计入上限。
2. **assistant 消息严格按规范回填**：``content`` 可为 ``None`` 但字段必须在；
   ``tool_calls[].function.arguments`` 用模型返回的**原始字符串**，不解析再序列化
   （重新序列化会改变空白与转义，某些模型会因此行为异常）。解析只发生在执行工具时。
3. **``max_tool_rounds`` 是「已执行完 N 轮工具、准备发起第 N+1 轮请求」时才拦**：
   此时历史末尾是 tool 消息且前面有对应 assistant，序列合法；绝不出现
   「assistant 带了 tool_calls 但 tool 结果没回填」就退出的情况。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable, Collection, Iterator, Mapping, Sequence

from .config import AppConfig
from .logging_setup import get_logger
from .provider import ChatResult, Provider, ToolCall, Usage
from .registry import ERROR_PREFIX, ToolRegistry
from .storage import Storage

__all__ = ["AgentLoop", "LoopResult", "APPROVAL_REJECTED_MESSAGE", "trim_history"]

#: 审批被拒绝时回灌给模型的工具结果。
#: 不带「错误：」前缀：拒绝是用户的正常决策，不是程序/工具错误。
APPROVAL_REJECTED_MESSAGE = "用户已拒绝此操作，工具未执行。"


@dataclass(frozen=True)
class LoopResult:
    """一次 ``run()`` 的结果。"""

    content: str | None
    usage: Usage
    rounds: int
    tool_limit_reached: bool = False
    finish: str = "stop"  # stop | tool_limit


def _split_groups(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[list[dict]]]:
    """把消息切成「固定的 system」+「按组划分的正文」。

    一组的定义：``assistant(tool_calls)`` 连同紧随其后的**全部** ``tool`` 消息算一组；
    其余消息各自成组。孤立 tool 消息（前面没有 assistant(tool_calls)）永远非法，直接丢弃。
    """
    body = [dict(message) for message in messages]
    pinned: list[dict] = []
    while body and body[0].get("role") == "system":
        pinned.append(body.pop(0))

    groups: list[list[dict]] = []
    index = 0
    while index < len(body):
        message = body[index]
        role = message.get("role")

        if role == "tool":
            index += 1  # 孤立 tool 消息：丢弃
            continue
        if role == "assistant" and message.get("tool_calls"):
            group = [message]
            index += 1
            while index < len(body) and body[index].get("role") == "tool":
                group.append(body[index])
                index += 1
            groups.append(group)
            continue
        groups.append([message])
        index += 1
    return pinned, groups


def trim_history(
    messages: Sequence[Mapping[str, Any]], limit: int | None
) -> list[dict[str, Any]]:
    """保留最近 ``limit`` 条正文消息（按组取舍），system 消息永远保留且不计入。

    只会保留一个**连续的尾部**：某组装不下就停止，不会为了凑数保留更早的消息
    （那会在历史中间留洞，同样产生非法序列）。
    """
    pinned, groups = _split_groups(messages)
    effective = 1 if limit is None or limit < 1 else int(limit)

    kept: list[list[dict]] = []
    count = 0
    for group in reversed(groups):
        if count + len(group) > effective:
            break
        kept.append(group)
        count += len(group)

    if not kept and groups:
        # 退化情形：最后一组本身就超限。宁可略微超限，也不发出只有 system 的空请求，
        # 且保留整组仍然保证 tool 消息有前驱 assistant。
        kept = [groups[-1]]

    result: list[dict[str, Any]] = list(pinned)
    for group in reversed(kept):
        result.extend(group)
    return result


class AgentLoop:
    """把 provider、工具注册表、持久化串起来的多轮循环。"""

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        *,
        system_prompt: str | None = None,
        max_tool_rounds: int = 8,
        max_history_messages: int = 40,
        storage: Storage | None = None,
        session_id: str | None = None,
        history: Sequence[Mapping[str, Any]] | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        approval_handler: Callable[[ToolCall], bool] | None = None,
        approval_required: Collection[str] = (),
        logger: logging.Logger | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.max_history_messages = max(1, int(max_history_messages))
        self.storage = storage
        self.session_id = session_id
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        #: 人工审批：handler 返回 True 才执行；None 表示不启用（CLI 关闭审批时）
        self.approval_handler = approval_handler
        self.approval_required = frozenset(approval_required)
        self._logger = logger or get_logger("agent.loop")

        self._messages: list[dict[str, Any]] = [dict(m) for m in (history or [])]
        if system_prompt and not any(m.get("role") == "system" for m in self._messages):
            system_message = {"role": "system", "content": system_prompt}
            self._messages.insert(0, system_message)
            # 全新的会话要把 system 消息落库（否则 resume 后提示词会丢）。
            # 已有消息的会话不落库：那会把它追加到 seq 末尾，破坏「system 在最前」的顺序；
            # 这种会话每次构造循环时都会在内存里补上 system，发给模型的内容依然正确。
            if (
                self.storage is not None
                and self.session_id
                and self.storage.count_messages(self.session_id) == 0
            ):
                self.storage.append_message(self.session_id, system_message)

    # ---- 构造 -----------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        provider: Provider,
        registry: ToolRegistry,
        *,
        storage: Storage | None = None,
        session_id: str | None = None,
        history: Sequence[Mapping[str, Any]] | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        approval_handler: Callable[[ToolCall], bool] | None = None,
    ) -> "AgentLoop":
        return cls(
            provider,
            registry,
            system_prompt=config.system_prompt_text(),
            max_tool_rounds=config.agent.max_tool_rounds,
            max_history_messages=config.agent.max_history_messages,
            storage=storage,
            session_id=session_id,
            history=history,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            approval_handler=approval_handler,
            # 只有 require_approval 打开时才让 loop 认识「需要审批」的工具
            approval_required=(
                config.tools.approval_required if config.tools.require_approval else ()
            ),
        )

    # ---- 只读视图 -------------------------------------------------------
    @property
    def messages(self) -> list[dict[str, Any]]:
        """完整历史（未裁剪），供持久化与展示用。"""
        return [dict(message) for message in self._messages]

    def request_messages(self) -> list[dict[str, Any]]:
        """实际会发给模型的历史（已按组裁剪）。"""
        return trim_history(self._messages, self.max_history_messages)

    # ---- 主循环 ---------------------------------------------------------
    def run(self, user_input: str) -> LoopResult:
        """同步入口（CLI 用）：内部消费 ``run_stream()``，只返回最终结果。"""
        for event in self.run_stream(user_input):
            if event["type"] == "done":
                return event["result"]
        raise RuntimeError("run_stream() 没有产出 done 事件")  # pragma: no cover

    def run_stream(self, user_input: str) -> Iterator[dict[str, Any]]:
        """流式入口：逐条 yield 增量事件，最后 yield 一个 done 事件。

        事件：
        - ``{"type": "reasoning", "text": ...}`` 思维链增量
        - ``{"type": "text", "text": ...}`` 正文增量
        - ``{"type": "notice", "text": ...}`` 工具轮次上限提示（同时也会进 content）
        - ``{"type": "done", "result": LoopResult, "reasoning": str | None}``
        """
        text = (user_input or "").strip()
        if not text:
            raise ValueError("用户输入不能为空")

        self._add_message({"role": "user", "content": text})

        run_usage = Usage()
        rounds = 0
        while True:
            # 上限判定：已执行完 rounds 轮工具、正要从这里发起第 rounds+1 轮请求
            if rounds >= self.max_tool_rounds:
                notice = f"已达工具调用上限（{self.max_tool_rounds} 轮），已停止继续调用工具。"
                self._logger.warning(notice)
                yield {"type": "notice", "text": notice}
                yield {
                    "type": "done",
                    "reasoning": None,
                    "result": LoopResult(
                        content=notice,
                        usage=run_usage,
                        rounds=rounds,
                        tool_limit_reached=True,
                        finish="tool_limit",
                    ),
                }
                return

            tools = self.registry.schemas() or None
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: tuple[ToolCall, ...] = ()
            usage = Usage()
            event_reasoning: str | None = None
            finish_reason: str | None = None

            for event in self.provider.chat_stream(self.request_messages(), tools=tools):
                kind = event.get("type")
                if kind == "reasoning":
                    reasoning_parts.append(str(event.get("text") or ""))
                    yield event
                elif kind == "text":
                    content_parts.append(str(event.get("text") or ""))
                    yield event
                elif kind == "done":
                    tool_calls = tuple(event.get("tool_calls") or ())
                    usage = event.get("usage") or Usage()
                    event_reasoning = event.get("reasoning")
                    finish_reason = event.get("finish_reason")

            run_usage = run_usage + usage
            self._record_usage(usage)

            content = "".join(content_parts) or None

            # 空回答兜底：模型可能把 token 预算全烧在思考上（reasoning 模型常见），
            # 导致这一轮既没有正文也没有 tool_calls。这种 assistant 消息**不能落库**
            # （OpenAI 协议要求 content 或 tool_calls 至少有一个），也不能让整轮 500。
            # 做法：合成一条人话提示作为正文，既让用户知道发生了什么，也让历史保持合法。
            if content is None and not tool_calls:
                content = self._empty_answer_notice(finish_reason, event_reasoning)
                self._logger.warning(
                    "模型本轮没有产出正文（finish_reason=%s，思考 %d 字），已用提示语代替",
                    finish_reason,
                    len(event_reasoning or ""),
                )
                yield {"type": "text", "text": content}

            result = ChatResult(content=content, tool_calls=tool_calls, usage=usage)
            assistant_message = self.build_assistant_message(result)
            # 思维链与单轮 usage 随 assistant 消息一起落库（只给前端回放，不进请求）
            usage_payload = asdict(usage) if usage.total_tokens or usage.prompt_tokens else None
            self._add_message(
                assistant_message, reasoning=event_reasoning, usage=usage_payload
            )

            if not result.wants_tools:
                yield {
                    "type": "done",
                    "reasoning": "".join(reasoning_parts) or None,
                    "result": LoopResult(
                        content=content, usage=run_usage, rounds=rounds, finish="stop"
                    ),
                }
                return

            rounds += 1
            self._execute_tool_calls(tool_calls)

    # ---- 消息构造 -------------------------------------------------------
    @staticmethod
    def _empty_answer_notice(finish_reason: str | None, reasoning: str | None) -> str:
        """模型没产出正文时给用户的人话提示（同时作为 assistant.content 落库）。"""
        if finish_reason == "length":
            length = len(reasoning or "")
            thinking = f"（本轮思考了 {length} 字）" if length else ""
            return (
                f"模型在思考阶段就用完了 max_tokens 配额{thinking}，没有产出正文。"
                "请调大该模型的 max_tokens（或在模型管理里换一个上下文/输出预算更大的模型）后重试。"
            )
        return "模型这一轮没有返回正文。可以重试一次，或者换一个模型试试。"

    @staticmethod
    def build_assistant_message(result: ChatResult) -> dict[str, Any]:
        """严格按 OpenAI 规范构造 assistant 消息。

        - ``content`` 可以是 ``None``，但**字段必须存在**；
        - 带 ``tool_calls`` 时，``arguments`` 使用 provider 给出的原始字符串，
          在这里既不解析也不重新序列化。
        """
        message: dict[str, Any] = {"role": "assistant", "content": result.content}
        if result.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in result.tool_calls
            ]
        return message

    @staticmethod
    def build_tool_message(call: ToolCall, output: str) -> dict[str, Any]:
        """``content`` 必须是字符串（registry 已保证），且按 ToolCall.id 对齐。"""
        if not isinstance(output, str):
            raise TypeError(f"工具结果必须是 str，实际是 {type(output).__name__}")
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": output,
        }

    # ---- 工具执行 -------------------------------------------------------
    def _execute_tool_calls(self, tool_calls: Sequence[ToolCall]) -> None:
        """按 tool_calls 数组顺序逐个执行并逐个回填（顺序与 id 都要对齐）。"""
        for call in tool_calls:
            if self.on_tool_call is not None:
                self.on_tool_call(call)
            output = self._run_tool(call)
            if self.on_tool_result is not None:
                self.on_tool_result(call, output)
            if output == APPROVAL_REJECTED_MESSAGE:
                self._logger.warning("工具 %s 被用户拒绝，未执行", call.name)
            elif output.startswith(ERROR_PREFIX):
                self._logger.warning(
                    "工具 %s 执行返回错误，已把错误文本回灌给模型：%s",
                    call.name,
                    output[:200],
                )
            else:
                self._logger.info("工具 %s 执行完成（%d 字符）", call.name, len(output))
            self._add_message(self.build_tool_message(call, output))

    def _run_tool(self, call: ToolCall) -> str:
        """执行单个工具；启用审批时先**阻塞**等人工决策。

        审批 handler 返回 False（拒绝 / 超时 / 客户端断开）时不执行工具，
        把「用户已拒绝此操作」作为 tool 结果回灌 —— 历史仍是合法序列
        （assistant tool_calls 有对应 tool 消息），模型可以继续解释或换方案。
        """
        if self.approval_handler is not None and call.name in self.approval_required:
            approved = self.approval_handler(call)
            if not approved:
                return APPROVAL_REJECTED_MESSAGE
        # 参数解析与校验都发生在这一层（registry.call），失败会返回错误文本
        return self.registry.call(call.name, call.arguments)

    # ---- 中断处理 -------------------------------------------------------
    def pending_tool_calls(self) -> list[ToolCall]:
        """最后一轮 assistant(tool_calls) 里还没有回填结果的调用。"""
        position: int | None = None
        for index in range(len(self._messages) - 1, -1, -1):
            message = self._messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                position = index
                break
        if position is None:
            return []

        calls: list[ToolCall] = []
        for offset, raw in enumerate(self._messages[position]["tool_calls"]):
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{offset}"),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or "{}"),
                )
            )
        answered = {
            message.get("tool_call_id")
            for message in self._messages[position + 1 :]
            if message.get("role") == "tool"
        }
        return [call for call in calls if call.id not in answered]

    def abort_pending_tool_calls(self, reason: str) -> int:
        """把被中断而没执行的 tool_call 补成错误结果，保证历史仍是合法序列。

        消息是边跑边落库的，中途退出可能停在「assistant 带了 tool_calls、tool 结果还没写」，
        那样的历史发给下一次请求会直接 400。这里补上显式的「已中断」结果，
        既保住了用户那一句提问，也不会留下悬空的 tool_call。
        """
        pending = self.pending_tool_calls()
        for call in pending:
            self._add_message(self.build_tool_message(call, f"{ERROR_PREFIX}{reason}"))
        if pending:
            self._logger.warning("中断处理：补全了 %d 条未执行的工具结果", len(pending))
        return len(pending)

    # ---- 持久化 ---------------------------------------------------------
    def _add_message(
        self,
        message: Mapping[str, Any],
        *,
        reasoning: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """加入内存历史并落库。

        ``reasoning`` / ``usage`` 只落库（前端回放用），**不**进入 ``self._messages``，
        避免下次请求把它们当成 OpenAI 字段发给模型。
        """
        record = dict(message)
        self._messages.append(record)
        if self.storage is not None and self.session_id:
            self.storage.append_message(
                self.session_id, record, reasoning=reasoning, usage=usage
            )
        return record

    def _record_usage(self, usage: Usage) -> None:
        if self.storage is None or not self.session_id:
            return
        self.storage.add_usage(
            self.session_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
        )