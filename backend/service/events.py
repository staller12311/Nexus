"""SSE 事件编码 + 「同步工具回调 → 异步生成器」的桥。

事件协议（与前端约定，见 README 设计决策第 34–36、48 条）：

    data: {"type": "message_start", "session_id": "..."}
    data: {"type": "reasoning_delta", "text": "..."}       ← 思维链增量（模型支持时才有）
    data: {"type": "tool_call_start", "tool_call_id": "...", "name": "...", "arguments": "<原始 JSON 字符串>"}
    data: {"type": "tool_call_end",   "tool_call_id": "...", "name": "...", "ok": true, "content": "..."}
    data: {"type": "text_delta", "delta": "..."}          ← 正文增量，真·逐 token
    data: {"type": "message_end", "content": "...", "reasoning": "...", "usage": {...}, "rounds": 1}
    data: {"type": "error", "message": "...", "code": "..."}

现在 ``text_delta`` 是**真的**流式增量（provider 侧 ``stream=True``），不再是切块伪造；
前端照旧「收到就 append」，所以从假流式切到真流式前端零改动（第 34 条的设计生效了）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def encode_event(event: dict[str, Any]) -> str:
    """序列化成 SSE 帧：``data: <json>\\n\\n``。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class EventBridge:
    """把工作线程里产生的同步事件安全地送进事件循环。

    ``service.chat()`` 在 ``asyncio.to_thread`` 的线程里跑，工具回调也从那个线程触发；
    ``asyncio.Queue`` 不是线程安全的，所以统一走 ``call_soon_threadsafe``。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except RuntimeError:  # 事件循环已关闭（客户端早就断开）：丢弃事件即可
            pass

    async def receive(self, timeout: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def drain(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    @property
    def empty(self) -> bool:
        return self._queue.empty()