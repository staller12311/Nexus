"""SQLite 会话持久化。

只用标准库 sqlite3，不引入 ORM。设计决策见 README：
- 消息以「OpenAI API 形状的 dict」存取，工具调用序列化成 JSON 文本；
- 与模型交互用的历史裁剪依赖 ``get_messages(..., limit=n)`` 返回**最近 n 条**并按时间升序；
- 时间统一存 UTC ISO-8601 字符串（秒精度），跨平台可比较、可读。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

VALID_ROLES = ("system", "user", "assistant", "tool")

#: 允许落库的消息字段（其余字段如 usage、reasoning 不持久化）
_MESSAGE_COLUMNS = ("role", "content", "tool_calls", "tool_call_id", "name")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_seq INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    name         TEXT,
    reasoning_content TEXT,
    usage_json   TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
"""


class StorageError(Exception):
    """会话不存在、数据非法或数据库不可用。"""


@dataclass(frozen=True)
class SessionRecord:
    id: str
    title: str
    model: str
    created_at: str
    updated_at: str
    message_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0


def utc_now() -> str:
    """人类可读的 UTC ISO-8601（微秒精度）。

    注意：**不要**拿它做排序依据。Windows 墙钟粒度约 15.6ms，
    实测 200 次连续取值只得到 1 个不同结果，同一次操作批里时间戳会完全相同。
    排序一律用 ``sessions.updated_seq`` 这个逻辑序号。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_session_id() -> str:
    """短会话 id（12 位十六进制），便于在命令行里输入。"""
    return uuid.uuid4().hex[:12]


def _jsonable(value: Any) -> Any:
    """把 openai SDK 对象（有 model_dump）递归转成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(exclude_none=True))
    return str(value)


def normalize_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """校验并裁剪成落库形状；不修改入参。"""
    role = message.get("role")
    if role not in VALID_ROLES:
        raise StorageError(f"非法 role={role!r}，允许 {list(VALID_ROLES)}")

    normalized: dict[str, Any] = {"role": role}
    for key in _MESSAGE_COLUMNS:
        if key == "role":
            continue
        if key in message:
            normalized[key] = _jsonable(message[key])

    if role == "tool" and not normalized.get("tool_call_id"):
        raise StorageError("role=tool 的消息必须带 tool_call_id")
    if role == "assistant" and not normalized.get("content") and not normalized.get("tool_calls"):
        raise StorageError("assistant 消息必须至少有 content 或 tool_calls")
    return normalized


class Storage:
    """会话与消息的持久化。线程内使用（CLI 单线程），不共享连接。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.exists() and self.db_path.is_dir():
            raise StorageError(f"数据库路径是一个目录：{self.db_path}")
        if self.db_path.parent != Path(""):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：为旧版本建的库补上后加的列。"""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "updated_seq" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN updated_seq INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.execute("UPDATE sessions SET updated_seq = rowid")
        for column in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_hit_tokens"):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )

        # messages 后加的列：思维链 + 单条消息的 usage（缓存命中统计要用）
        message_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")
        }
        if "reasoning_content" not in message_columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")
        if "usage_json" not in message_columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN usage_json TEXT")

    def _next_seq(self) -> int:
        """下一个逻辑序号（会话顺序的唯一依据）。调用方需处于事务中。"""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(updated_seq), 0) + 1 AS next FROM sessions"
        ).fetchone()
        return int(row["next"])

    # ---- 生命周期 -------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- 会话 -----------------------------------------------------------
    def create_session(
        self,
        model: str,
        title: str = "",
        session_id: str | None = None,
    ) -> SessionRecord:
        if not model or not str(model).strip():
            raise StorageError("创建会话必须指定 model")
        sid = session_id or new_session_id()
        now = utc_now()
        with self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO sessions (id, title, model, created_at, updated_at, updated_seq)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, title or "", model, now, now, self._next_seq()),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(f"会话 id 已存在：{sid}") from exc
        return SessionRecord(sid, title or "", model, now, now, 0)

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)"
            " AS message_count FROM sessions s WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        return self._row_to_session(row) if row else None

    def require_session(self, session_id: str) -> SessionRecord:
        record = self.get_session(session_id)
        if record is None:
            raise StorageError(f"找不到会话：{session_id}")
        return record

    def find_session(self, partial: str) -> SessionRecord:
        """按完整 id 或**唯一前缀**查会话（``sessions list`` 只显示 8 位短 id）。

        - 完整 id 精确命中优先；
        - 否则按前缀匹配：唯一则返回，多个则报「前缀不唯一」并列出候选，
          没有则报「找不到会话」。
        """
        key = (partial or "").strip()
        if not key:
            # 空串绝不能进前缀匹配：LIKE '' || '%' 会匹配到所有会话
            raise StorageError("请提供会话 id（不能为空）")

        exact = self.get_session(key)
        if exact is not None:
            return exact

        rows = self._conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)"
            " AS message_count FROM sessions s WHERE s.id LIKE ? || '%'"
            " ORDER BY s.updated_seq DESC, s.rowid DESC",
            (key,),
        ).fetchall()
        if not rows:
            raise StorageError(f"找不到会话：{key}")
        if len(rows) > 1:
            candidates = ", ".join(row["id"] for row in rows)
            raise StorageError(
                f"会话 id 前缀 {key!r} 不唯一，匹配到 {len(rows)} 个：{candidates}。请给出更长的前缀。"
            )
        return self._row_to_session(rows[0])

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        if limit <= 0:
            return []
        # updated_seq 是排序依据（墙钟在同一次操作里可能完全相同）；rowid 仅作最终兜底
        rows = self._conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)"
            " AS message_count FROM sessions s"
            " ORDER BY s.updated_seq DESC, s.rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def rename_session(self, session_id: str, title: str) -> None:
        self.require_session(session_id)
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ?, updated_seq = ? WHERE id = ?",
                (title, utc_now(), self._next_seq(), session_id),
            )

    def touch_session(self, session_id: str, model: str | None = None) -> None:
        self.require_session(session_id)
        with self._conn:
            if model:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ?, model = ?, updated_seq = ? WHERE id = ?",
                    (utc_now(), model, self._next_seq(), session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ?, updated_seq = ? WHERE id = ?",
                    (utc_now(), self._next_seq(), session_id),
                )

    def delete_session(self, session_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0

    def add_usage(
        self,
        session_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_hit_tokens: int = 0,
    ) -> SessionRecord:
        """把一次（或一轮）模型调用的 token 用量累加到会话记录上。"""
        self.require_session(session_id)
        with self._conn:
            self._conn.execute(
                "UPDATE sessions SET prompt_tokens = prompt_tokens + ?,"
                " completion_tokens = completion_tokens + ?, total_tokens = total_tokens + ?,"
                " cache_hit_tokens = cache_hit_tokens + ?"
                " WHERE id = ?",
                (
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    int(cache_hit_tokens),
                    session_id,
                ),
            )
        return self.require_session(session_id)

    def usage_totals(self) -> dict[str, int]:
        """全库聚合：所有会话的累计用量（/api/stats 用）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS sessions,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens"
            " FROM sessions"
        ).fetchone()
        keys = ("sessions", "prompt_tokens", "completion_tokens", "total_tokens", "cache_hit_tokens")
        return {key: int(row[key]) for key in keys}

    # ---- 消息 -----------------------------------------------------------
    def append_message(
        self,
        session_id: str,
        message: Mapping[str, Any],
        *,
        reasoning: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> int:
        """追加一条消息，返回其 seq（从 1 开始）。

        ``reasoning`` / ``usage`` 单独传参（而不是塞进 message）：因为 message 会原样进入
        ``request_messages()`` 发给模型，OpenAI 协议里没有这两个字段，混进去会让部分严格
        校验的上游 400。它们只写进库、只回放给前端（``get_messages(with_usage=True)``）。
        """
        normalized = normalize_message(message)
        reasoning_text = (reasoning or "").strip() or None
        usage_text = json.dumps(dict(usage), ensure_ascii=False) if usage else None
        with self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                raise StorageError(f"找不到会话：{session_id}")

            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["last"]) + 1
            now = utc_now()
            self._conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, tool_calls,"
                " tool_call_id, name, reasoning_content, usage_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    normalized["role"],
                    normalized.get("content"),
                    json.dumps(normalized["tool_calls"], ensure_ascii=False)
                    if normalized.get("tool_calls")
                    else None,
                    normalized.get("tool_call_id"),
                    normalized.get("name"),
                    reasoning_text,
                    usage_text,
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ?, updated_seq = ? WHERE id = ?",
                (now, self._next_seq(), session_id),
            )
        return seq

    def append_messages(
        self, session_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> list[int]:
        return [self.append_message(session_id, message) for message in messages]

    def get_messages(
        self,
        session_id: str,
        limit: int | None = None,
        *,
        with_reasoning: bool = False,
        with_usage: bool = False,
    ) -> list[dict[str, Any]]:
        """按时间升序返回消息；limit 表示只取**最近** limit 条。

        ``with_reasoning`` / ``with_usage`` 默认 False（给 agent 回放用）：这两个字段
        只给前端展示，不能出现在发回模型的请求里。
        """
        self.require_session(session_id)
        if limit is not None and limit <= 0:
            return []
        if limit is None:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id = ?"
                " ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_message(row, with_reasoning, with_usage) for row in rows]

    def count_messages(self, session_id: str) -> int:
        self.require_session(session_id)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"])

    def delete_from_last_user(self, session_id: str) -> str | None:
        """删除「最后一条 user 消息及其之后」的全部消息，返回那条 user 的文本。

        供「重新生成」使用：删完再把同一句话重新入历史，历史既不重复也不会出现
        孤立的 assistant/tool 消息（README 第 53 条）。

        事务：显式 ``BEGIN IMMEDIATE`` 把「查最后一条 user」与「整段删除」放进同一个写事务，
        任何一步抛错都整体回滚 —— 绝不会出现「删了一半」导致历史里留孤立 tool 消息
        （那种状态下次请求会被上游 400 拒掉）。
        """
        self.require_session(session_id)
        started_here = not self._conn.in_transaction
        if started_here:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT seq, content FROM messages WHERE session_id = ? AND role = 'user'"
                " ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                if started_here:
                    self._conn.commit()
                return None
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq >= ?",
                (session_id, row["seq"]),
            )
            if started_here:
                self._conn.commit()
        except Exception:
            if started_here:
                self._conn.rollback()
            raise
        return row["content"]

    def iter_sessions(self, limit: int = 20) -> Iterator[SessionRecord]:
        yield from self.list_sessions(limit)

    # ---- 内部 -----------------------------------------------------------
    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> SessionRecord:
        keys = row.keys()

        def pick_int(column: str) -> int:
            return int(row[column]) if column in keys and row[column] is not None else 0

        return SessionRecord(
            id=row["id"],
            title=row["title"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=pick_int("message_count"),
            prompt_tokens=pick_int("prompt_tokens"),
            completion_tokens=pick_int("completion_tokens"),
            total_tokens=pick_int("total_tokens"),
            cache_hit_tokens=pick_int("cache_hit_tokens"),
        )

    @staticmethod
    def _row_to_message(
        row: sqlite3.Row, with_reasoning: bool = False, with_usage: bool = False
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": row["role"]}
        if row["content"] is not None:
            message["content"] = row["content"]
        if row["tool_calls"]:
            message["tool_calls"] = json.loads(row["tool_calls"])
        if row["tool_call_id"]:
            message["tool_call_id"] = row["tool_call_id"]
        if row["name"]:
            message["name"] = row["name"]
        if with_reasoning and row["reasoning_content"]:
            message["reasoning_content"] = row["reasoning_content"]
        if with_usage and row["usage_json"]:
            try:
                message["usage"] = json.loads(row["usage_json"])
            except (TypeError, ValueError):  # pragma: no cover - 旧库脏数据
                pass
        return message