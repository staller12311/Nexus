"""验证 delete_from_last_user 的事务安全：中途失败必须整体回滚，不留半截历史。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.storage import Storage  # noqa: E402


def fresh_db() -> tuple[Storage, str]:
    path = Path(tempfile.mkdtemp()) / "tx.db"
    storage = Storage(path)
    session = storage.create_session("mock")
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
        {"role": "user", "content": "读一下 readme.txt"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "hi"},
        {"role": "assistant", "content": "结果：hi"},
    ]
    for message in messages:
        storage.append_message(session.id, message)
    return storage, session.id


print("=" * 74)
print("① 正常路径：删掉最后一条 user 及其之后，返回该 user 文本")
print("=" * 74)
storage, session_id = fresh_db()
before = storage.get_messages(session_id)
print(f"  删除前 {len(before)} 条: {[m['role'] for m in before]}")
text = storage.delete_from_last_user(session_id)
after = storage.get_messages(session_id)
print(f"  返回: {text!r}")
print(f"  删除后 {len(after)} 条: {[m['role'] for m in after]}")
assert text == "读一下 readme.txt"
assert [m["role"] for m in after] == ["system", "user", "assistant"]

print("\n" + "=" * 74)
print("② 中途失败：DELETE 抛错时必须回滚，历史保持原样")
print("=" * 74)
storage2, session_id2 = fresh_db()
original = storage2.get_messages(session_id2)
# 用 SQLite 触发器让 DELETE 必定失败，模拟「删到一半崩溃」
storage2._conn.executescript(
    "CREATE TRIGGER boom BEFORE DELETE ON messages"
    " BEGIN SELECT RAISE(ABORT, '模拟删除过程中崩溃'); END;"
)
storage2._conn.commit()

try:
    storage2.delete_from_last_user(session_id2)
    print("  ❌ 竟然没抛异常")
    raise SystemExit(1)
except sqlite3.IntegrityError as exc:
    print(f"  抛出预期异常: {exc}")
finally:
    storage2._conn.executescript("DROP TRIGGER boom;")
    storage2._conn.commit()

restored = storage2.get_messages(session_id2)
print(f"  回滚后 {len(restored)} 条: {[m['role'] for m in restored]}")
assert [m["role"] for m in restored] == [m["role"] for m in original], "❌ 回滚不完整！"
assert len(restored) == len(original), "❌ 消息数变了，说明删了一半"
assert storage2._conn.in_transaction is False, "❌ 失败后事务没关掉"
print("  ✅ 一条都没少，也没有半截历史")

print("\n" + "=" * 74)
print("③ 没有 user 消息的会话：返回 None，不抛错")
print("=" * 74)
empty_path = Path(tempfile.mkdtemp()) / "empty.db"
storage3 = Storage(empty_path)
session3 = storage3.create_session("mock")
storage3.append_message(session3.id, {"role": "system", "content": "只有系统提示"})
again = storage3.delete_from_last_user(session3.id)
print(f"  返回: {again!r}")
assert again is None
print(f"  消息仍在: {[m['role'] for m in storage3.get_messages(session3.id)]}")

print("\n" + "=" * 74)
print("④ 事务不残留：操作后连接不在事务中")
print("=" * 74)
storage4, session_id4 = fresh_db()
storage4.delete_from_last_user(session_id4)
print(f"  in_transaction = {storage4._conn.in_transaction}")
assert storage4._conn.in_transaction is False

print("\n全部通过 ✅ 事务安全：要么全删，要么什么都不动")