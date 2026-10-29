"""阶段 2：SQLite 会话持久化 + 日志初始化/脱敏的单元测试。"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from agent import logging_setup
from agent.logging_setup import (
    REDACTED,
    RedactingFormatter,
    redact,
    register_secret,
    setup_logging,
)
from agent.storage import (
    SessionRecord,
    Storage,
    StorageError,
    new_session_id,
    normalize_message,
)


@pytest.fixture()
def storage(tmp_path: Path):
    db = Storage(tmp_path / "data" / "sessions.db")
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _clean_secrets():
    logging_setup.clear_registered_secrets()
    yield
    logging_setup.clear_registered_secrets()


# ------------------------------------------------------------------ 会话
def test_create_and_get_session(storage: Storage) -> None:
    record = storage.create_session("deepseek", title="第一个会话")
    assert isinstance(record, SessionRecord)
    assert len(record.id) == 12
    assert record.message_count == 0

    fetched = storage.get_session(record.id)
    assert fetched is not None
    assert fetched.title == "第一个会话"
    assert fetched.model == "deepseek"
    assert fetched.created_at.endswith("+00:00")


def test_get_missing_session_returns_none(storage: Storage) -> None:
    assert storage.get_session("nope") is None


def test_require_session_raises_with_id(storage: Storage) -> None:
    with pytest.raises(StorageError) as excinfo:
        storage.require_session("ghost")
    assert "ghost" in str(excinfo.value)


def test_create_session_requires_model(storage: Storage) -> None:
    with pytest.raises(StorageError):
        storage.create_session("   ")


def test_duplicate_session_id_rejected(storage: Storage) -> None:
    sid = new_session_id()
    storage.create_session("m", session_id=sid)
    with pytest.raises(StorageError) as excinfo:
        storage.create_session("m", session_id=sid)
    assert sid in str(excinfo.value)


def test_list_sessions_ordered_by_updated_at(storage: Storage) -> None:
    first = storage.create_session("m", title="one")
    second = storage.create_session("m", title="two")
    storage.append_message(first.id, {"role": "user", "content": "hi"})
    ids = [record.id for record in storage.list_sessions()]
    assert ids[0] == first.id
    assert second.id in ids


def test_list_sessions_order_is_deterministic(storage: Storage) -> None:
    """回归：同一秒内批量建会话时，顺序必须是「后建的在前」，不能退化成按随机 id 排序。"""
    ids = [storage.create_session("m", title=str(index)).id for index in range(5)]
    assert [record.id for record in storage.list_sessions()] == list(reversed(ids))


def test_timestamps_have_sub_second_precision(storage: Storage) -> None:
    record = storage.create_session("m")
    assert "." in record.created_at.split("+")[0]


def test_ordering_uses_logical_clock_not_wall_clock(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：Windows 墙钟粒度约 15.6ms，连续取值可能完全相同，排序必须靠 updated_seq。"""
    frozen = "2026-01-01T00:00:00.000000+00:00"
    monkeypatch.setattr("agent.storage.utc_now", lambda: frozen)

    first = storage.create_session("m", title="a")
    second = storage.create_session("m", title="b")
    storage.append_message(first.id, {"role": "user", "content": "hi"})

    # 时间戳确实撞在一起（这就是原来的 bug 现场）
    assert storage.require_session(first.id).updated_at == frozen
    assert storage.require_session(second.id).updated_at == frozen
    # 但顺序仍然正确：first 刚被更新，必须排在最前
    assert [record.id for record in storage.list_sessions()] == [first.id, second.id]


def test_migrates_old_database_without_updated_seq(tmp_path: Path) -> None:
    """旧库（没有 updated_seq 列）打开后应自动补列并保持可用。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('old1', '标题一', 'm', '2020-01-01T00:00:00+00:00',"
        " '2020-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('old2', '标题二', 'm', '2020-01-02T00:00:00+00:00',"
        " '2020-01-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    with Storage(db_path) as storage:
        assert [record.id for record in storage.list_sessions()] == ["old2", "old1"]
        storage.append_message("old1", {"role": "user", "content": "hi"})
        assert [record.id for record in storage.list_sessions()] == ["old1", "old2"]


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """连续两次打开同一个旧库都不应报错（第二次已无列可补）。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('old1', '', 'm', '2020-01-01T00:00:00+00:00',"
        " '2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    for attempt in range(2):
        with Storage(db_path) as storage:
            assert [record.id for record in storage.list_sessions()] == ["old1"], (
                f"第 {attempt + 1} 次打开时行为应一致"
            )
            storage.append_message("old1", {"role": "user", "content": f"第{attempt}次"})
    # 第二次打开不该重复补列，也不该丢数据
    with Storage(db_path) as storage:
        assert len(storage.get_messages("old1")) == 2


def test_find_session_empty_input_is_rejected(storage: Storage) -> None:
    """空串不能进前缀匹配：``LIKE '' || '%'`` 会匹配到所有会话。"""
    storage.create_session("m", title="存在的会话")
    for value in ("", "   ", None):
        with pytest.raises(StorageError) as excinfo:
            storage.find_session(value)  # type: ignore[arg-type]
        assert "请提供会话 id" in str(excinfo.value)


def test_find_session_by_full_id_and_unique_prefix(storage: Storage) -> None:
    first = storage.create_session("m", session_id="abc123aaaaaa")
    storage.create_session("m", session_id="xyz789bbbbbb")

    assert storage.find_session("abc123aaaaaa").id == first.id
    assert storage.find_session("abc1").id == first.id


def test_find_session_ambiguous_prefix_lists_candidates(storage: Storage) -> None:
    storage.create_session("m", session_id="dup123aaaaaa")
    storage.create_session("m", session_id="dup123bbbbbb")
    with pytest.raises(StorageError) as excinfo:
        storage.find_session("dup123")
    message = str(excinfo.value)
    assert "不唯一" in message
    assert "dup123aaaaaa" in message and "dup123bbbbbb" in message


def test_find_session_unknown_prefix(storage: Storage) -> None:
    storage.create_session("m")
    with pytest.raises(StorageError) as excinfo:
        storage.find_session("zzz")
    assert "找不到会话" in str(excinfo.value)


def test_list_sessions_limit(storage: Storage) -> None:
    for _ in range(3):
        storage.create_session("m")
    assert len(storage.list_sessions(limit=2)) == 2
    assert storage.list_sessions(limit=0) == []


def test_rename_session(storage: Storage) -> None:
    record = storage.create_session("m", title="old")
    storage.rename_session(record.id, "new")
    assert storage.require_session(record.id).title == "new"


def test_delete_session_cascades_messages(storage: Storage) -> None:
    record = storage.create_session("m")
    storage.append_message(record.id, {"role": "user", "content": "hi"})
    assert storage.delete_session(record.id) is True
    assert storage.get_session(record.id) is None
    rows = storage._conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (record.id,)
    ).fetchone()
    assert rows["n"] == 0
    assert storage.delete_session(record.id) is False


# ------------------------------------------------------------------ 消息
def test_append_and_read_message(storage: Storage) -> None:
    record = storage.create_session("m")
    seq = storage.append_message(record.id, {"role": "user", "content": "你好"})
    assert seq == 1
    assert storage.append_message(record.id, {"role": "assistant", "content": "hi"}) == 2
    assert storage.get_messages(record.id) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "hi"},
    ]
    assert storage.count_messages(record.id) == 2
    assert storage.require_session(record.id).message_count == 2


def test_tool_call_roundtrip(storage: Storage) -> None:
    record = storage.create_session("m")
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
        }
    ]
    storage.append_message(
        record.id, {"role": "assistant", "content": None, "tool_calls": tool_calls}
    )
    storage.append_message(
        record.id,
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "ok"},
    )
    messages = storage.get_messages(record.id)
    assert messages[0]["tool_calls"] == tool_calls
    assert "content" not in messages[0]
    assert messages[1]["tool_call_id"] == "call_1"
    assert messages[1]["name"] == "read_file"


def test_tool_calls_accept_sdk_objects(storage: Storage) -> None:
    class _Dumpable:
        def model_dump(self, exclude_none: bool = True) -> dict:
            return {"id": "call_9", "type": "function", "function": {"name": "list_dir"}}

    record = storage.create_session("m")
    storage.append_message(
        record.id, {"role": "assistant", "content": None, "tool_calls": [_Dumpable()]}
    )
    assert storage.get_messages(record.id)[0]["tool_calls"][0]["id"] == "call_9"


def test_get_messages_limit_returns_latest_in_order(storage: Storage) -> None:
    record = storage.create_session("m")
    for index in range(5):
        storage.append_message(record.id, {"role": "user", "content": f"m{index}"})
    assert [m["content"] for m in storage.get_messages(record.id, limit=2)] == ["m3", "m4"]
    assert storage.get_messages(record.id, limit=0) == []


def test_append_to_missing_session_raises(storage: Storage) -> None:
    with pytest.raises(StorageError) as excinfo:
        storage.append_message("ghost", {"role": "user", "content": "hi"})
    assert "ghost" in str(excinfo.value)


def test_invalid_role_rejected(storage: Storage) -> None:
    record = storage.create_session("m")
    with pytest.raises(StorageError):
        storage.append_message(record.id, {"role": "wizard", "content": "hi"})


def test_tool_message_requires_tool_call_id(storage: Storage) -> None:
    record = storage.create_session("m")
    with pytest.raises(StorageError):
        storage.append_message(record.id, {"role": "tool", "content": "ok"})


def test_empty_assistant_message_rejected(storage: Storage) -> None:
    record = storage.create_session("m")
    with pytest.raises(StorageError):
        storage.append_message(record.id, {"role": "assistant", "content": ""})


def test_normalize_message_does_not_mutate_input() -> None:
    source = {"role": "user", "content": "hi", "extra": "drop-me"}
    normalized = normalize_message(source)
    assert source == {"role": "user", "content": "hi", "extra": "drop-me"}
    assert "extra" not in normalized


def test_append_messages_batch(storage: Storage) -> None:
    record = storage.create_session("m")
    assert storage.append_messages(
        record.id, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    ) == [1, 2]


# ------------------------------------------------------------------ 持久化
def test_data_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    with Storage(db_path) as first:
        record = first.create_session("deepseek", title="持久化")
        first.append_message(record.id, {"role": "user", "content": "记住我"})

    with Storage(db_path) as second:
        assert second.require_session(record.id).title == "持久化"
        assert second.get_messages(record.id)[0]["content"] == "记住我"


def test_creates_parent_directories(tmp_path: Path) -> None:
    db_path = tmp_path / "a" / "b" / "sessions.db"
    with Storage(db_path):
        pass
    assert db_path.is_file()


def test_db_path_is_directory_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "adir"
    directory.mkdir()
    with pytest.raises(StorageError):
        Storage(directory)


# ------------------------------------------------------------------ 日志
def test_setup_logging_writes_utf8_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "agent.log"
    logger = setup_logging("INFO", log_file, environ={})
    logger.info("中文日志 %s", "可用")
    for handler in logger.handlers:
        handler.flush()

    text = log_file.read_text(encoding="utf-8")
    assert "中文日志 可用" in text
    assert "INFO" in text


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    log_file = tmp_path / "agent.log"
    logger = setup_logging("INFO", log_file, environ={})
    setup_logging("DEBUG", log_file, environ={})
    assert len(logger.handlers) == 1
    assert logger.level == logging.DEBUG


def test_setup_logging_rejects_bad_level(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        setup_logging("LOUD", tmp_path / "agent.log", environ={})


def test_setup_logging_respects_level(tmp_path: Path) -> None:
    log_file = tmp_path / "agent.log"
    logger = setup_logging("WARNING", log_file, environ={})
    logger.info("不该出现")
    logger.warning("应该出现")
    for handler in logger.handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "不该出现" not in text
    assert "应该出现" in text


def test_env_secret_is_redacted(tmp_path: Path) -> None:
    log_file = tmp_path / "agent.log"
    logger = setup_logging(
        "INFO", log_file, environ={"MY_API_KEY": "super-secret-value"}
    )
    logger.info("key is super-secret-value")
    for handler in logger.handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "super-secret-value" not in text
    assert REDACTED in text


def test_runtime_registered_secret_is_redacted(tmp_path: Path) -> None:
    log_file = tmp_path / "agent.log"
    logger = setup_logging("INFO", log_file, environ={})
    register_secret("sk-live-abcdef123456")
    logger.info("using sk-live-abcdef123456 now")
    for handler in logger.handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "sk-live-abcdef123456" not in text


def test_secret_in_exception_traceback_is_redacted(tmp_path: Path) -> None:
    log_file = tmp_path / "agent.log"
    logger = setup_logging("INFO", log_file, environ={"OPENAI_API_KEY": "sk-abcdef1234567890"})
    try:
        raise RuntimeError("boom sk-abcdef1234567890")
    except RuntimeError:
        logger.exception("调用失败")
    for handler in logger.handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "sk-abcdef1234567890" not in text


def test_redact_patterns() -> None:
    assert "sk-" not in redact("token sk-abcdef123456")
    assert "Bearer abcdefgh12345" not in redact("Authorization: Bearer abcdefgh12345")
    assert redact("普通文本没有密钥") == "普通文本没有密钥"


def test_redacting_formatter_formats_record() -> None:
    formatter = RedactingFormatter(secrets=["topsecretvalue"])
    record = logging.LogRecord(
        "agent", logging.INFO, __file__, 1, "值=%s", ("topsecretvalue",), None
    )
    output = formatter.format(record)
    assert "topsecretvalue" not in output
    assert REDACTED in output


def test_null_handler_when_no_output_targets() -> None:
    logger = setup_logging("INFO", None, console=False, environ={})
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.NullHandler)


def test_force_utf8_stdio_does_not_raise() -> None:
    logging_setup.force_utf8_stdio()