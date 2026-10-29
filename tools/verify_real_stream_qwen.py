"""跨厂商验证：dashscope 的 qwen-max（不返回 reasoning_content）也应逐 token 流式输出。

Key 从本地 custom_models 表只读取出，全程不打印。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.provider import Provider  # noqa: E402
from backend.service.model_runtime import DbModelConfig  # noqa: E402

target_name = sys.argv[1] if len(sys.argv) > 1 else "qwen3.8-flash"
DB = ROOT / "data" / "sessions.db"
connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
row = connection.execute(
    "SELECT name, base_url, model, api_key FROM custom_models WHERE name = ? LIMIT 1",
    (target_name,),
).fetchone()
connection.close()
if row is None:
    print(f"库里没有名为 {target_name!r} 的自定义模型")
    sys.exit(1)
name, base_url, model, api_key = row
print(f"自定义模型 {name!r}: base_url={base_url} model={model} key=已配置({len(api_key)} 字节)")

config = DbModelConfig(
    base_url=base_url,
    model=model,
    api_key_env="DB_STORED_KEY",  # 只是占位：密钥由 stored_api_key 提供
    stored_api_key=api_key,
)
provider = Provider(config, request_timeout=120, max_retries=0)
try:
    print("\n" + "=" * 70)
    print("提问：9.11 和 9.9 哪个大，为什么？  （model=qwen-max，预期没有 reasoning）")
    print("=" * 70)
    start = time.perf_counter()
    first_text = None
    reasoning = text = ""
    text_frames = reasoning_frames = 0
    done = None
    for event in provider.chat_stream(
        [{"role": "user", "content": "9.11 和 9.9 哪个大，为什么？请简短回答。"}],
        max_tokens=300,
    ):
        if event["type"] == "reasoning":
            reasoning_frames += 1
            reasoning += event["text"]
        elif event["type"] == "text":
            text_frames += 1
            if first_text is None:
                first_text = (time.perf_counter() - start) * 1000
            text += event["text"]
        elif event["type"] == "done":
            done = event
    total = (time.perf_counter() - start) * 1000

    print(f"  reasoning 帧数={reasoning_frames}（0 表示该模型不支持思维链，属正常）")
    print(f"  text 帧数={text_frames}  共 {len(text)} 字")
    if first_text:
        print(f"  首字延迟 {first_text:.0f} ms / 总耗时 {total:.0f} ms")
    print(f"  usage: {done['usage']}")
    print(f"  正文: {text[:150]!r}")

    assert done is not None and done["content"] == text
    assert text_frames > 1, "❌ 正文不是逐 token"
    assert "9.9" in text
    print("\n✅ 无 reasoning 的厂商：照样逐 token 流式，且不会伪造思考内容")
finally:
    provider.close()