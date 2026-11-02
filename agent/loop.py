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

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable, Collection, Iterator, Mapping, Sequence

from .config import AppConfig
from .logging_setup import get_logger
from .provider import ChatResult, Provider, ToolCall, Usage
from .registry import ERROR_PREFIX, ToolRegistry
from .storage import Storage
from .tools import ToolError

__all__ = [
    "AgentLoop",
    "LoopResult",
    "APPROVAL_REJECTED_MESSAGE",
    "INVALID_ARGUMENTS_MESSAGE",
    "ARGUMENT_COMPACT_TOOLS",
    "ARGUMENT_COMPACT_THRESHOLD",
    "ToolArgumentsError",
    "trim_history",
]

#: 审批被拒绝时回灌给模型的工具结果。
#: 不带「错误：」前缀：拒绝是用户的正常决策，不是程序/工具错误。
APPROVAL_REJECTED_MESSAGE = "用户已拒绝此操作，工具未执行。"

#: tool_call.arguments 不是合法 JSON 对象时的本地占位。
#: 占位必须是合法 JSON —— 否则回填后的历史发给上游仍会被 400 拒绝。
INVALID_ARGUMENTS_PLACEHOLDER = "{}"
#: 本地拦截后回灌给模型的错误文本（模型看到后可在同一轮内重新生成）。
INVALID_ARGUMENTS_MESSAGE = "参数不是合法 JSON，请重新生成（本次工具未执行）。"

#: 思考把输出预算吃光时的补救指令（以 system 消息追加，避免污染用户可见的对话气泡）。
#:
#: 要点是「把要求缩到极小」：上一轮已经证明，让它「直接输出完整结果」它只会继续思考
#: （实测第二轮又烧掉 27981 字）。只要求一句 50 字以内的分步计划，思考才有边界。
EMPTY_ANSWER_NUDGE = (
    "上一轮的思考已经耗尽本轮输出预算，没有写出任何正文。"
    "现在只做一件事：用不超过 50 个字说明你打算分几步完成这件事、下一步马上做什么；"
    "如果需要用工具，就在这段计划之后立刻发起第一个工具调用。"
    "不要写代码，不要重复分析，不要解释原因。"
)

#: 历史上这些工具的 arguments 超过阈值后压缩成占位 JSON（大段代码已落盘，无需在历史里回显）
ARGUMENT_COMPACT_TOOLS = frozenset({"write_file", "run_python_code"})
ARGUMENT_COMPACT_THRESHOLD = 2000

#: 历史摘要压缩：触发比例 / 每次压缩的旧消息条数 / 摘要提示词
SUMMARY_SYSTEM_PROMPT = (
    "你是对话记忆压缩器。把用户提供的较早对话压缩成一段不超过 200 字的中文摘要，"
    "保留：用户目标、关键决定、创建/修改过的文件、重要错误及修复、尚未完成的事项。"
    "只输出摘要正文，不要解释、不要 Markdown。"
)
SUMMARY_USER_TEMPLATE = "历史对话如下，请压缩成记忆摘要：\n\n{transcript}"


class ToolArgumentsError(ToolError):
    """模型的 function.arguments 不是合法 JSON：本地拦截，绝不发给上游。"""



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


def _approval_tool_names(config: AppConfig) -> tuple[str, ...]:
    names = set(config.tools.approval_required) if config.tools.require_approval else set()
    if config.tools.allow_unsafe_shell:
        names.add("run_shell")
    return tuple(sorted(names))


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
        context_window: int | None = None,
        summarizer: Callable[[str], str | None] | None = None,
        context_summary_trigger: float = 0.8,
        context_summary_messages: int = 10,
        empty_answer_retries: int = 1,
        request_transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.max_history_messages = max(1, int(max_history_messages))
        #: 思考把输出预算吃光时，最多补救几次（0 = 不补救，直接给提示）
        self.empty_answer_retries = max(0, min(3, int(empty_answer_retries)))
        self.storage = storage
        self.session_id = session_id
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        #: 人工审批：handler 返回 True 才执行；None 表示不启用（CLI 关闭审批时）
        self.approval_handler = approval_handler
        self.approval_required = frozenset(approval_required)
        #: 本轮被本地拦截的非法 arguments：call_id -> 错误文本
        self._invalid_arguments: dict[str, str] = {}
        #: 模型真实上下文窗口；None 时退回旧的「按条数裁剪」行为
        self.context_window = int(context_window) if context_window else None
        #: 后台静默摘要器：输入一段历史对话文本，输出一句摘要（None 表示不可用）
        self.summarizer = summarizer
        self.context_summary_trigger = max(0.1, min(float(context_summary_trigger), 1.0))
        self.context_summary_messages = max(2, int(context_summary_messages))
        #: 已压缩进 system 提示词的历史摘要（多轮压缩会累加）
        self._history_summary = ""
        self._last_prompt_tokens = 0
        #: 本轮摘要是否失败/为空（失败时退回按条数裁剪，避免请求体失控）
        self._summary_failed = False
        #: 可选：发给 provider 前对消息做一次纯函数转换（backend 的 Vision 适配器用）
        self.request_transform = request_transform
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
        context_window: int | None = None,
        summarizer: Callable[[str], str | None] | None = None,
        request_transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
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
            # 只有 require_approval 打开时才让 loop 认识「需要审批」的工具；
            # allow_unsafe_shell 是独立危险开关，即使全局审批被改回 false 也强制审批 run_shell。
            approval_required=_approval_tool_names(config),
            context_window=context_window,
            summarizer=summarizer,
            context_summary_trigger=config.agent.context_summary_trigger,
            context_summary_messages=config.agent.context_summary_messages,
            empty_answer_retries=config.agent.empty_answer_retries,
            request_transform=request_transform,
        )

    # ---- 只读视图 -------------------------------------------------------
    @property
    def messages(self) -> list[dict[str, Any]]:
        """完整历史（未裁剪），供持久化与展示用。"""
        return [dict(message) for message in self._messages]

    def request_messages(self) -> list[dict[str, Any]]:
        """实际会发给模型的历史。

        - 先压缩「写入文件/执行代码」这类大参数 tool_call（内容已落盘，历史只需保留调用）；
        - 启用摘要压缩时不再按条数粗暴裁剪，由 ``_maybe_summarize_history`` 在超阈值时
          把最旧的消息替换成 system 里的记忆摘要；未启用时退回 ``trim_history``。
        """
        messages = self._compact_tool_arguments(self._messages)
        if (
            self.context_window is not None
            and self.summarizer is not None
            and not self._summary_failed
        ):
            return self._transform_messages(messages)
        return self._transform_messages(trim_history(messages, self.max_history_messages))

    def _transform_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """可选扩展点：backend 的 Vision 适配器在这里把附件换成 content 数组。

        默认 no-op；CLI 与未配置视觉能力时行为完全不变。
        """
        if self.request_transform is None:
            return messages
        return self.request_transform(messages)

    # ---- 主循环 ---------------------------------------------------------
    def run(
        self,
        user_input: str,
        *,
        attachments: Sequence[Mapping[str, Any]] | None = None,
    ) -> LoopResult:
        """同步入口（CLI 用）：内部消费 ``run_stream()``，只返回最终结果。"""
        for event in self.run_stream(user_input, attachments=attachments):
            if event["type"] == "done":
                return event["result"]
        raise RuntimeError("run_stream() 没有产出 done 事件")  # pragma: no cover

    def run_stream(
        self,
        user_input: str,
        *,
        attachments: Sequence[Mapping[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """流式入口：逐条 yield 增量事件，最后 yield 一个 done 事件。

        事件：
        - ``{"type": "reasoning", "text": ...}`` 思维链增量
        - ``{"type": "text", "text": ...}`` 正文增量
        - ``{"type": "notice", "text": ...}`` 工具轮次上限提示（同时也会进 content）
        - ``{"type": "summary_notice", ...}`` 上下文摘要压缩成功（供 SSE 通知前端）
        - ``{"type": "done", "result": LoopResult, "reasoning": str | None}``
        """
        text = (user_input or "").strip()
        if not text and not attachments:
            raise ValueError("用户输入不能为空")

        user_message: dict[str, Any] = {"role": "user", "content": text or "[图片]"}
        if attachments:
            user_message["attachments"] = [dict(item) for item in attachments]
        self._add_message(user_message)

        run_usage = Usage()
        rounds = 0
        empty_retries = 0
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

            # 上下文超过阈值时：用「摘要」替换最旧的若干轮，而不是直接砍掉
            summary_notice = self._maybe_summarize_history()
            if summary_notice is not None:
                yield summary_notice
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
            if usage.prompt_tokens:
                self._last_prompt_tokens = usage.prompt_tokens
            self._record_usage(usage)

            content = "".join(content_parts) or None

            # 空回答兜底：模型可能把 token 预算全烧在思考上（reasoning 模型常见），
            # 导致这一轮既没有正文也没有 tool_calls。这种 assistant 消息**不能落库**
            # （OpenAI 协议要求 content 或 tool_calls 至少有一个），也不能让整轮 500。
            # 做法：合成一条人话提示作为正文，既让用户知道发生了什么，也让历史保持合法。
            if content is None and not tool_calls:
                # 推理模型可能把整个输出预算都花在思考上：实测把 max_tokens 从 8192 调到 16384，
                # 思考量也跟着涨到 5 万多字，正文依然是空的 —— 单纯调大预算解决不了。
                # 所以先做一次「别再想了，直接输出」的补救：追加一条 system 提醒后重跑。
                if finish_reason == "length" and empty_retries < self.empty_answer_retries:
                    empty_retries += 1
                    notice = (
                        "模型把本轮输出预算全用在思考上了"
                        f"（思考 {len(event_reasoning or '')} 字），正在要求它直接输出结论…"
                    )
                    self._logger.warning(notice)
                    yield {"type": "notice", "text": notice}
                    self._add_message({"role": "system", "content": EMPTY_ANSWER_NUDGE})
                    continue

                content = self._empty_answer_notice(finish_reason, event_reasoning)
                self._logger.warning(
                    "模型本轮没有产出正文（finish_reason=%s，思考 %d 字），已用提示语代替",
                    finish_reason,
                    len(event_reasoning or ""),
                )
                yield {"type": "text", "text": content}

            result = ChatResult(content=content, tool_calls=tool_calls, usage=usage)
            # 构造 assistant 历史前先校验 arguments：非法 JSON 绝不能回填/发给上游
            result, invalid_arguments = self._sanitize_tool_arguments(result)
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
            self._invalid_arguments = invalid_arguments
            self._execute_tool_calls(result.tool_calls)

    # ---- 消息构造 -------------------------------------------------------
    @staticmethod
    def _empty_answer_notice(finish_reason: str | None, reasoning: str | None) -> str:
        """模型没产出正文时给用户的人话提示（同时作为 assistant.content 落库）。"""
        if finish_reason == "length":
            length = len(reasoning or "")
            thinking = f"（本轮思考了 {length} 字）" if length else ""
            return (
                f"模型在思考阶段就用完了输出预算{thinking}，没有产出正文。"
                "注意「1M / 128K」说的是上下文窗口（能读多少），不是能写多少；"
                "输出上限由 max_tokens 决定，而推理模型的思考与正文共用这一个额度，"
                "所以只调大窗口并不能解决。"
                "建议在「模型管理」里把 max_tokens 调到 32768 以上（按厂商实际上限），"
                "或者换一个思考更短的模型后重试。"
            )
        return "模型这一轮没有返回正文。可以重试一次，或者换一个模型试试。"

    @staticmethod
    def build_assistant_message(result: ChatResult) -> dict[str, Any]:
        """严格按 OpenAI 规范构造 assistant 消息。

        - ``content`` 可以是 ``None``，但**字段必须存在**；
        - 带 ``tool_calls`` 时，``arguments`` 使用 provider 给出的原始字符串，
          在这里既不解析也不重新序列化；但调用前已经过
          ``_sanitize_tool_arguments()`` 校验，保证这里的一定是合法 JSON 对象。
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

    def _sanitize_tool_arguments(
        self, result: ChatResult
    ) -> tuple[ChatResult, dict[str, str]]:
        """构造 assistant 历史前校验每个 ``function.arguments`` 是否是合法 JSON 对象。

        模型生成 HTML/JS 这类引号密集的代码时，偶尔会把 JSON 转义写坏。原样回填
        ``assistant.tool_calls[].function.arguments`` 后，下一轮请求会被上游以
        ``400 The "function.arguments" parameter must be in JSON format`` 拒绝，
        模型还会反复重试直到打满轮数。这里在**本地**拦截：

        - 非法调用的 arguments 替换为 ``{}``（保证历史与上游请求永远合法）；
        - 该工具不执行，tool 结果回灌「参数不是合法 JSON，请重新生成」；
        - 模型在同一轮内看到错误后自行纠正，不会出现上游 400。
        """
        if not result.tool_calls:
            return result, {}

        invalid: dict[str, str] = {}
        sanitized: list[ToolCall] = []
        for call in result.tool_calls:
            try:
                parsed = json.loads(call.arguments)
            except (TypeError, ValueError):
                parsed = None
            if not isinstance(parsed, dict):
                invalid[call.id] = INVALID_ARGUMENTS_MESSAGE
                self._logger.warning(
                    "工具 %s 的 arguments 不是合法 JSON 对象，已本地拦截（原始 %d 字符）",
                    call.name,
                    len(call.arguments) if isinstance(call.arguments, str) else 0,
                )
                sanitized.append(
                    ToolCall(
                        id=call.id,
                        name=call.name,
                        arguments=INVALID_ARGUMENTS_PLACEHOLDER,
                    )
                )
            else:
                sanitized.append(call)

        if not invalid:
            return result, {}
        return (
            ChatResult(
                content=result.content,
                tool_calls=tuple(sanitized),
                usage=result.usage,
            ),
            invalid,
        )

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

    # ---- 上下文管理 -----------------------------------------------------
    @staticmethod
    def _compact_tool_arguments(
        messages: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """把历史上超大参数的 tool_call 替换成占位 JSON。

        大段代码已经写入文件/已执行，历史里没有必要再回显全文；
        保留 path/说明即可（仍满足上游「arguments 必须是合法 JSON」的要求）。
        """
        compacted: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                compacted.append(dict(message))
                continue

            calls: list[dict[str, Any]] = []
            for raw in message["tool_calls"]:
                call = dict(raw)
                function = dict(call.get("function") or {})
                name = function.get("name")
                arguments = function.get("arguments")
                if (
                    name in ARGUMENT_COMPACT_TOOLS
                    and isinstance(arguments, str)
                    and len(arguments) >= ARGUMENT_COMPACT_THRESHOLD
                ):
                    path = ""
                    try:
                        parsed = json.loads(arguments)
                        if isinstance(parsed, dict):
                            path = str(parsed.get("path") or "")
                    except (TypeError, ValueError):
                        pass
                    if name == "write_file":
                        placeholder = {
                            "path": path,
                            "content": f"（{len(arguments)} 字符的代码已写入文件，历史中省略）",
                        }
                    else:
                        placeholder = {
                            "code": f"（{len(arguments)} 字符的代码已执行，历史中省略）"
                        }
                    function["arguments"] = json.dumps(placeholder, ensure_ascii=False)
                call["function"] = function
                calls.append(call)
            message = dict(message)
            message["tool_calls"] = calls
            compacted.append(message)
        return compacted

    def _estimate_context_tokens(self) -> int:
        """粗估当前请求的 token 数：优先用上一轮真实 prompt_tokens，其次按字符数估算。"""
        if self._last_prompt_tokens:
            return self._last_prompt_tokens
        chars = 0
        for message in self._messages:
            chars += len(str(message.get("content") or ""))
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                chars += len(str(function.get("arguments") or "")) + len(
                    str(function.get("name") or "")
                )
        # 中英文混合按 2.5 字符/token 保守估算
        return int(chars / 2.5)

    def _maybe_summarize_history(self) -> dict[str, Any] | None:
        """上下文达到窗口的 ``trigger`` 比例时，把最旧的若干轮压缩成一条摘要。

        摘要写入 system 提示词（保持 OpenAI 消息序列合法），原消息从内存历史移除；
        DB 里的完整历史不动（前端回放仍是全文）。摘要失败时静默跳过，下一轮再试。
        """
        if self.context_window is None or self.summarizer is None:
            return None
        self._summary_failed = False
        estimated = self._estimate_context_tokens()
        if estimated < self.context_window * self.context_summary_trigger:
            return None

        pinned, groups = _split_groups(self._messages)
        # 至少给最近两轮留位置；最多压缩 context_summary_messages 条
        removable = max(0, len(groups) - 2)
        if removable <= 0:
            return None
        take = min(self.context_summary_messages, removable)
        removed_groups = groups[:take]
        removed = [message for group in removed_groups for message in group]

        transcript = self._format_history_for_summary(removed)
        try:
            summary = self.summarizer(transcript)
        except Exception as exc:  # noqa: BLE001 - 摘要失败不能让主对话崩溃
            self._summary_failed = True
            self._logger.warning("历史摘要失败，本轮退回按条数裁剪：%s", exc)
            return None
        summary = (summary or "").strip()
        if not summary:
            self._summary_failed = True
            return None

        self._history_summary = (
            f"{self._history_summary}；{summary}" if self._history_summary else summary
        )
        block = f"\n\n【历史记忆摘要】{self._history_summary}"
        if pinned:
            base = str(pinned[0].get("content") or "")
            # 避免每次摘要都重复追加旧摘要
            if "【历史记忆摘要】" in base:
                base = base.split("\n\n【历史记忆摘要】", 1)[0]
            pinned[0] = {**pinned[0], "content": f"{base}{block}"}
        else:
            pinned = [{"role": "system", "content": f"【历史记忆摘要】{self._history_summary}"}]

        self._messages = pinned + [
            message for group in groups[take:] for message in group
        ]
        self._last_prompt_tokens = 0
        self._logger.info(
            "上下文 %d tokens ≥ %.0f%% 窗口，已把 %d 条旧消息压缩成摘要（剩余 %d 条）",
            estimated,
            self.context_summary_trigger * 100,
            len(removed),
            len(self._messages),
        )
        removed_user_turns = sum(1 for message in removed if message.get("role") == "user")
        rounds = removed_user_turns or len(removed_groups)
        content = f"为节省上下文，已自动压缩前 {rounds} 轮对话。"
        return {
            "type": "summary_notice",
            "content": content,
            "summary": summary,
            "total_summary": self._history_summary,
            "rounds": rounds,
            "messages": len(removed),
        }

    @staticmethod
    def _format_history_for_summary(messages: Sequence[Mapping[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            calls = message.get("tool_calls") or []
            if role == "assistant" and calls:
                names = ", ".join(
                    str((call.get("function") or {}).get("name") or "?") for call in calls
                )
                lines.append(f"assistant 调用工具 [{names}]：{content[:300]}")
            elif role == "tool":
                name = str(message.get("name") or "tool")
                lines.append(f"tool({name}) 返回：{content[:400]}")
            else:
                lines.append(f"{role}: {content[:600]}")
        return "\n".join(lines)

    # ---- 工具执行 -------------------------------------------------------
    def _execute_tool_calls(self, tool_calls: Sequence[ToolCall]) -> None:
        """按 tool_calls 数组顺序逐个执行并逐个回填（顺序与 id 都要对齐）。"""
        for call in tool_calls:
            if self.on_tool_call is not None:
                self.on_tool_call(call)
            try:
                output = self._run_tool(call)
            except ToolArgumentsError as exc:
                output = f"{ERROR_PREFIX}{exc}"
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
        invalid = self._invalid_arguments.pop(call.id, None)
        if invalid is not None:
            raise ToolArgumentsError(invalid)
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