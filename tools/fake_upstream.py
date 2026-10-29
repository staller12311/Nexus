"""开发用的可控假上游（OpenAI 兼容），供前端集成测试使用。

前端 `src/__tests__/integration.live.test.tsx` 需要三样东西：
  1. 真后端（uvicorn）跑在 127.0.0.1:8000
  2. 一个假上游（本文件）跑在 127.0.0.1:8128
  3. 后端的配置指向假上游（见 README「集成测试」一节的 YAML 片段）

用法::

    python tools/fake_upstream.py 8128

行为可用 POST /control 切换（集成测试自己会调）::

    {"mode": "simple", "delay": 0.0}

模式：
  simple  — 第一轮要 read_file("readme.txt")，第二轮给出最终回答（成功路径）
  missing — 第一轮要 read_file("nope.txt")，触发工具失败
  multi   — A(文字+get_time) → B(文字+get_time) → C(最终)，两轮工具调用
  boom    — 模型接口恒返回 500，用来验证重试与错误事件
  delay   — 每次模型调用前 sleep 指定秒数，用来测「客户端断开」
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"mode": "simple", "delay": 0.0, "n": 0}
#: 最后一次收到的请求（供验证脚本断言"后端确实用了自定义的 base_url 与 Key"）
LAST: dict[str, object] = {}


def tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        LAST.clear()
        LAST.update(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "raw": raw.decode("utf-8", errors="replace")[:400],
            }
        )

        if self.path == "/control":
            body = json.loads(raw)
            STATE["mode"] = body.get("mode", STATE["mode"])
            STATE["delay"] = float(body.get("delay", 0.0))
            STATE["n"] = 0
            self._send({"ok": True, "mode": STATE["mode"], "delay": STATE["delay"]})
            return

        body = json.loads(raw)
        LAST["model"] = body.get("model", "")
        LAST["n_messages"] = len(body.get("messages", []))
        STATE["n"] += 1
        if STATE["delay"]:
            time.sleep(STATE["delay"])

        if STATE["mode"] == "boom":
            self._send({"error": {"message": "upstream is down"}}, status=500)
            return

        messages = body.get("messages", [])
        tool_results = [item for item in messages if item.get("role") == "tool"]
        mode = STATE["mode"]

        if mode == "title":
            # 故意带「标题：」前缀、空格和标点，用来验证 clean_title 的清洗效果
            self._send(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model", "fake-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "标题：Python 快排。「实现」",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
                }
            )
            return

        if mode == "prompt":
            # 把收到的 system 消息原样回显，用来验证「改提示词后下一句立即生效」
            system = next(
                (str(item.get("content") or "") for item in messages if item.get("role") == "system"),
                "(无 system 消息)",
            )
            self._send(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model", "fake-model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": f"SYSTEM=[{system}]"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
            )
            return

        if mode == "kb":
            # 模拟「模型主动检索知识库」：第一轮调工具，第二轮把工具结果接上
            if not tool_results:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        tool_call(
                            "call_kb",
                            "search_knowledge_base",
                            json.dumps({"query": "一线城市住宿标准每晚多少钱"}, ensure_ascii=False),
                        )
                    ],
                }
                finish = "tool_calls"
            else:
                result = str(tool_results[-1].get("content", "")).strip()
                message = {
                    "role": "assistant",
                    "content": f"根据知识库：{result[:200]}",
                }
                finish = "stop"
        elif mode == "reason":
            # 带思维链的响应，用来验证 reasoning_delta 与思考面板
            message = {
                "role": "assistant",
                "content": "9.9 比 9.11 大，因为 9.9 = 9.90，十分位 9 > 1。",
                "reasoning_content": (
                    "先把两个数都写成同样的小数位数：9.11 与 9.90。"
                    "比较整数部分 9 = 9，再比较十分位 1 < 9，所以 9.11 < 9.9。"
                ),
            }
            finish = "stop"
        elif mode == "multi":
            done = len(tool_results)
            if done == 0:
                message = {
                    "role": "assistant",
                    "content": "A：我先看看时间。",
                    "tool_calls": [tool_call("call_a", "get_time", '{"zone": "UTC"}')],
                }
                finish = "tool_calls"
            elif done == 1:
                message = {
                    "role": "assistant",
                    "content": "B：再看一次时间。",
                    "tool_calls": [tool_call("call_b", "get_time", '{"zone": "LOCAL"}')],
                }
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "C：最终答案就是现在的时间。"}
                finish = "stop"
        else:  # simple / missing
            target = "nope.txt" if mode == "missing" else "readme.txt"
            if not tool_results:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call("call_read", "read_file", json.dumps({"path": target}))],
                }
                finish = "tool_calls"
            else:
                result = str(tool_results[-1].get("content", "")).strip()
                message = {"role": "assistant", "content": f"结果：{result}"}
                finish = "stop"

        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        }
        if body.get("stream"):
            self._send_sse(payload)
        else:
            self._send(payload)

    # ---- 流式 ----------------------------------------------------------
    def _send_sse(self, payload: dict) -> None:
        """把完整 completion 拆成 SSE 增量帧（含 reasoning 与工具调用碎片）。"""
        choice = payload["choices"][0]
        message = choice["message"]
        frames: list[dict] = []

        reasoning = message.get("reasoning_content")
        if reasoning:
            text = str(reasoning)
            for start in range(0, len(text), 6):
                frames.append(
                    {"choices": [{"index": 0, "delta": {"reasoning_content": text[start : start + 6]}}]}
                )

        content = message.get("content")
        if content:
            text = str(content)
            for start in range(0, len(text), 3):  # 每 3 个字符一片，模拟逐 token
                frames.append(
                    {"choices": [{"index": 0, "delta": {"content": text[start : start + 3]}}]}
                )

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
                                        "function": {
                                            "name": function.get("name"),
                                            "arguments": "",
                                        },
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
                                    "tool_calls": [{"index": index, "function": {"arguments": part}}]
                                },
                            }
                        ]
                    }
                )

        frames.append(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}
                ]
            }
        )
        frames.append({"choices": [], "usage": payload["usage"]})

        data = "".join(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames)
        data += "data: [DONE]\n\n"
        raw = data.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        """GET /last 返回最后一次请求；GET /v1/models 模拟 OpenAI 的模型列表接口。"""
        if self.path.rstrip("/").endswith("/models"):
            LAST["path"] = self.path
            LAST["authorization"] = self.headers.get("Authorization", "")
            self._send(
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-model-small", "object": "model"},
                        {"id": "mock-model-large", "object": "model"},
                        {"id": "mock-model-small", "object": "model"},  # 重复项应被去重
                    ],
                }
            )
            return
        if self.path != "/last":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(dict(LAST) or {"n": 0})

    def log_message(self, *args: object) -> None:
        return


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8128
    print(f"fake upstream on http://127.0.0.1:{port}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()