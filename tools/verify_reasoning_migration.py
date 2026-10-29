"""验证 messages.reasoning_content 的自动迁移与读写语义。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.storage import Storage  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_seq INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT,
    tool_call_id TEXT, name TEXT, created_at TEXT NOT NULL, UNIQUE (session_id, seq)
);
"""

tmp = Path(tempfile.mkdtemp()) / "old.db"
connection = sqlite3.connect(tmp)
connection.executescript(OLD_SCHEMA)
connection.execute(
    "INSERT INTO sessions (id, title, model, created_at, updated_at, updated_seq)"
    " VALUES ('s-old', '老会话', 'deepseek', '2026-01-01', '2026-01-01', 1)"
)
connection.execute(
    "INSERT INTO messages (session_id, seq, role, content, created_at)"
    " VALUES ('s-old', 1, 'user', '老消息', '2026-01-01')"
)
connection.commit()
connection.close()

print("=" * 70)
print("① 打开旧库 → 自动 ALTER TABLE，旧数据保留")
print("=" * 70)
storage = Storage(tmp)
columns = [row["name"] for row in storage._conn.execute("PRAGMA table_info(messages)")]
print(f"  messages 列: {columns}")
assert "reasoning_content" in columns
old = storage.get_messages("s-old", with_reasoning=True)
print(f"  旧消息: {old}")
assert old[0]["content"] == "老消息"
assert "reasoning_content" not in old[0]  # 旧行是 NULL，不返回空字段

print("\n" + "=" * 70)
print("② 写入带 reasoning 的 assistant 消息")
print("=" * 70)
seq = storage.append_message(
    "s-old",
    {"role": "assistant", "content": "9.9 更大"},
    reasoning="先把两个数写成同样小数位数…",
)
print(f"  写入 seq={seq}")
full = storage.get_messages("s-old", with_reasoning=True)
replay = storage.get_messages("s-old", with_reasoning=False)
print(f"  详情视图（给前端）: {full[-1]}")
print(f"  回放视图（给模型）: {replay[-1]}")
assert full[-1]["reasoning_content"] == "先把两个数写成同样小数位数…"
assert "reasoning_content" not in replay[-1], "❌ 给模型的消息里混入了 reasoning_content"

print("\n" + "=" * 70)
print("③ 空/空白 reasoning 存成 NULL，不写空字符串")
print("=" * 70)
storage.append_message("s-old", {"role": "assistant", "content": "没有思考"}, reasoning="   ")
row = storage._conn.execute(
    "SELECT reasoning_content FROM messages WHERE session_id='s-old' ORDER BY seq DESC LIMIT 1"
).fetchone()
print(f"  库里值: {row['reasoning_content']!r}")
assert row["reasoning_content"] is None

print("\n" + "=" * 70)
print("④ 再次打开（已迁移的库）→ 幂等，不重复 ALTER")
print("=" * 70)
storage.close()
storage2 = Storage(tmp)
count = len([r for r in storage2._conn.execute("PRAGMA table_info(messages)") if r["name"] == "reasoning_content"])
print(f"  reasoning_content 列数量: {count}")
assert count == 1
history = storage2.get_messages("s-old")
print(f"  重开后消息数: {len(history)}")
storage2.close()

print("\n全部通过 ✅")