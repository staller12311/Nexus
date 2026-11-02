"""最小示例：只用 `agent` 包跑一次「工具调用 → 回填 → 出结论」。

存在的意义有两个：

1. **证明内核可以脱离 backend 独立使用**。垂直应用（演示一个具体业务）不需要启动
   FastAPI、不需要前端，直接 import 这个包就能跑——这正是「内核」和「Web 应用」的分界线。
2. **当模板用**。新增一个领域工具只需要写一个 `ToolSpec` 并 register，**不改 agent 源码**。

运行：`python examples/standalone_agent.py`（不联网、不需要 API Key）。

要接真实模型，把下面的 `ScriptedProvider` 换成：

    from agent.config import load_config
    from agent.loop import AgentLoop

    loop = AgentLoop.from_config(load_config(), registry, ...)

然后把 `system_prompt` 换成你的领域提示词即可。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 直接从仓库根运行时，把根目录放进 sys.path（安装成包后这两行可以删掉）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 英文 locale 的 Windows 控制台默认是 cp1252，直接 print 中文会抛 UnicodeEncodeError。
# 这里显式把输出改成 UTF-8，保证在任何机器上都能正常跑（CI 的英文 runner 就是这种情况）。
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):  # pragma: no cover - 极少数不支持重配的标准流
            pass

from agent.loop import AgentLoop  # noqa: E402
from agent.provider import ToolCall, Usage  # noqa: E402
from agent.registry import ToolRegistry, ToolSpec  # noqa: E402

# ---------------------------------------------------------------- 领域工具
# 这就是垂直应用要写的那一层：跟业务绑定的工具，外部注册，不动内核。
_ROWS = [{"major": "计算机", "n": 120}, {"major": "会计", "n": 85}]


def count_rows(table: str) -> str:
    """真实项目里这里换成查库/读 Excel。"""
    return json.dumps(
        {"table": table, "total": sum(row["n"] for row in _ROWS)}, ensure_ascii=False
    )


COUNT_ROWS = ToolSpec(
    name="count_rows",
    description="统计某张表的总行数",
    parameters={
        "type": "object",
        "properties": {"table": {"type": "string", "description": "表名"}},
        "required": ["table"],
    },
    handler=count_rows,
)


# ---------------------------------------------------------------- 离线替身
@dataclass
class _StubProvider:
    """确定性替身：第 1 轮要求调工具，第 2 轮给结论。不联网。"""

    round: int = 0

    @property
    def model_name(self) -> str:
        return "offline-stub"

    def chat_stream(self, messages: list[dict[str, Any]], tools=None, **kwargs: Any):
        self.round += 1
        if self.round == 1:
            yield {
                "type": "done",
                "tool_calls": (
                    ToolCall(id="c1", name="count_rows", arguments='{"table": "students"}'),
                ),
                "usage": Usage(),
                "finish_reason": "tool_calls",
            }
            return
        answer = "学生表共 205 人，计算机 120、会计 85。"
        yield {"type": "text", "text": answer}
        yield {
            "type": "done",
            "content": answer,
            "tool_calls": (),
            "usage": Usage(),
            "finish_reason": "stop",
        }


def build_loop(provider: Any | None = None) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(COUNT_ROWS)  # ← 领域工具的注册点，内核零改动
    return AgentLoop(
        provider or _StubProvider(),
        registry,
        system_prompt="你是数据分析助手。回答前先用工具查数，不要凭空估算。",
    )


def main() -> int:
    loop = build_loop()
    result = loop.run("帮我统计学生表有多少人")

    print("finish      :", result.finish)
    print("content     :", result.content)
    print("tool 轮次    :", result.rounds)
    print("工具结果回填 :", [m["content"] for m in loop.messages if m["role"] == "tool"])

    ok = bool(result.content) and result.rounds >= 1
    print()
    print("结论：agent 包可脱离 backend 独立运行 =", ok)
    # ASCII 标记：给自动化测试用，避免依赖控制台编码与中文输出
    print("standalone_ok =", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
