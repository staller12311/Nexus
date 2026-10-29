"""真实厂商流式验证：deepseek-reasoner 的 reasoning_content + 逐 token 正文。

只打印长度、首字延迟、片段；绝不打印 API Key。成本很低（max_tokens=400）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.provider import Provider  # noqa: E402

config = load_config(str(ROOT / "config.yaml"))
deepseek = config.models.get("deepseek")
if deepseek is None:
    print("config.yaml 里没有 deepseek 模型")
    sys.exit(1)

key = deepseek.api_key()
if not key:
    print("❌ 没有读到 DEEPSEEK_API_KEY")
    sys.exit(1)
print(f"模型配置: base_url={deepseek.base_url} model={deepseek.model} key=已配置({len(key)} 字节)")

provider = Provider(deepseek, request_timeout=120, max_retries=1)
question = "9.11 和 9.9 哪个大，为什么？请简短回答。"

try:
    print("\n" + "=" * 70)
    print(f"提问：{question}   （model=deepseek-reasoner）")
    print("=" * 70)
    start = time.perf_counter()
    first_reasoning = first_text = None
    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    done = None

    for event in provider.chat_stream(
        [
            {"role": "system", "content": "你是严谨的助手。"},
            {"role": "user", "content": question},
        ],
        model="deepseek-reasoner",
        max_tokens=400,
    ):
        if event["type"] == "reasoning":
            if first_reasoning is None:
                first_reasoning = (time.perf_counter() - start) * 1000
            reasoning_parts.append(event["text"])
        elif event["type"] == "text":
            if first_text is None:
                first_text = (time.perf_counter() - start) * 1000
            text_parts.append(event["text"])
        elif event["type"] == "done":
            done = event

    total = (time.perf_counter() - start) * 1000
    reasoning = "".join(reasoning_parts)
    text = "".join(text_parts)

    print(f"  reasoning 帧数={len(reasoning_parts)}  共 {len(reasoning)} 字")
    print(f"  首字（思考）延迟: {first_reasoning:.0f} ms" if first_reasoning else "  没有 reasoning")
    print(f"  text 帧数={len(text_parts)}  共 {len(text)} 字")
    print(f"  首字（正文）延迟: {first_text:.0f} ms" if first_text else "  没有正文增量")
    print(f"  总耗时: {total:.0f} ms")
    print(f"  usage: {done['usage']}")
    print(f"\n  思考片段: {reasoning[:90]!r}…")
    print(f"  正文: {text[:150]!r}")

    assert done is not None
    assert len(reasoning_parts) > 1, "❌ 真实模型没有 reasoning 增量"
    assert len(text_parts) > 1, "❌ 正文不是逐 token"
    assert first_reasoning is not None and first_text is not None
    assert first_reasoning < total and first_text < total
    print("\n✅ 真实厂商：思考与正文都是流式增量（首字远早于结束）")
finally:
    provider.close()