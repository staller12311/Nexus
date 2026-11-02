"""自定义模型的持久化（backend 自己管，表在同一个 sqlite 文件里）。

设计约束：
- **不碰 `agent/`**：表由 backend 自己建、自己读写；会话持久化仍走 `agent.storage.Storage`。
- 与 agent 共用同一个库文件，所以沿用它的 WAL 模式与 ``utc_now()`` 时间格式（便于按时间排序/排查）。
- API Key 明文落库（与 `.env` 同一信任边界），但**绝不出现在任何响应、日志或异常信息里**：
  对外只给 ``mask_key()`` 的结果，日志侧由 ``register_secret()`` 兜底。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent.config import ConfigError, ModelConfig
from agent.storage import utc_now
from pydantic import ValidationError

#: 模型标识（下拉框/会话里记录的就是它）；不允许空格与中文，避免和路径、URL 混淆
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_models (
    name       TEXT PRIMARY KEY,
    base_url   TEXT NOT NULL,
    model      TEXT NOT NULL,
    api_key    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: 后加的列（旧库用 ALTER TABLE 补齐，不能重建表——重建会丢用户的密钥）
_EXTRA_COLUMNS = (
    # 推理模型的「思考」和「正文」共用这一个预算；不显式配置时以前固定 2048，
    # 结果是长思考直接把配额烧完、正文为空。允许用户按模型实际能力调。
    ("max_tokens", "INTEGER"),
    # 上游声明的上下文窗口；留空则按已知模型名推断（见 agent_service.context_window_for）
    ("context_window", "INTEGER"),
)


class CustomModelError(Exception):
    """自定义模型相关的业务错误（映射成 400）。"""


class DuplicateCustomModel(CustomModelError):
    """同名模型已存在（映射成 409）。"""


@dataclass(frozen=True)
class CustomModel:
    name: str
    base_url: str
    model: str
    api_key: str
    created_at: str
    updated_at: str
    #: 输出预算（思考 + 正文共用）；None = 用 CUSTOM_DEFAULTS
    max_tokens: int | None = None
    #: 上游上下文窗口；None = 按模型名推断（已知 1M 模型自动识别）
    context_window: int | None = None

    @property
    def masked_key(self) -> str:
        return mask_key(self.api_key)


def mask_key(key: str | None) -> str:
    """只露头尾：``sk-1234567890abcd`` → ``sk-****abcd``；太短就全遮。"""
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


def validate_name(name: str) -> str:
    text = (name or "").strip()
    if not NAME_PATTERN.match(text):
        raise CustomModelError("模型名只能包含字母、数字、下划线、点和连字符（不超过 64 个字符）")
    return text


def validate_fields(name: str, base_url: str, model: str, api_key: str) -> tuple[str, str, str, str]:
    """用 ``agent.config.ModelConfig`` 校验 base_url/model（复用同一套规则），并去掉 URL 尾部斜杠。"""
    clean_name = validate_name(name)
    clean_url = (base_url or "").strip().rstrip("/")
    clean_model = (model or "").strip()
    clean_key = (api_key or "").strip()
    if not clean_key:
        raise CustomModelError("API Key 不能为空")
    try:
        # 借用 ModelConfig 的校验器：base_url 必须 http(s)、非空等
        ModelConfig(base_url=clean_url, model=clean_model, api_key_env="(custom)")
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "参数"
        message = str(first.get("msg", "非法取值")).removeprefix("Value error, ")
        raise CustomModelError(f"{field}：{message}") from exc
    except ConfigError as exc:
        raise CustomModelError(str(exc)) from exc
    return clean_name, clean_url, clean_model, clean_key


class CustomModelStore:
    """`custom_models` 表的读写。每次操作短连接，避免跨线程复用 sqlite 连接。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._schema_ready = False

    # ---- 连接与建表 -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        if not self._schema_ready:
            connection.execute(_SCHEMA)
            self._migrate(connection)
            self._schema_ready = True
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """把后加的列补进旧库（幂等；已有该列就跳过）。"""
        existing = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(custom_models)")
        }
        for column, declaration in _EXTRA_COLUMNS:
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE custom_models ADD COLUMN {column} {declaration}"
                )

    # ---- 读 -------------------------------------------------------------
    def list(self) -> list[CustomModel]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT name, base_url, model, api_key, created_at, updated_at,"
                " max_tokens, context_window"
                " FROM custom_models ORDER BY created_at ASC"
            ).fetchall()
        finally:
            connection.close()
        return [self._to_model(row) for row in rows]

    def get(self, name: str) -> CustomModel | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT name, base_url, model, api_key, created_at, updated_at,"
                " max_tokens, context_window"
                " FROM custom_models WHERE name = ?",
                (name,),
            ).fetchone()
        finally:
            connection.close()
        return self._to_model(row) if row else None

    # ---- 写 -------------------------------------------------------------
    def add(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        *,
        max_tokens: int | None = None,
        context_window: int | None = None,
    ) -> CustomModel:
        clean_name, clean_url, clean_model, clean_key = validate_fields(
            name, base_url, model, api_key
        )
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO custom_models (name, base_url, model, api_key, created_at,"
                    " updated_at, max_tokens, context_window)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clean_name,
                        clean_url,
                        clean_model,
                        clean_key,
                        now,
                        now,
                        max_tokens,
                        context_window,
                    ),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise DuplicateCustomModel(f"模型名已存在：{clean_name}") from exc
            except sqlite3.Error:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return CustomModel(
            name=clean_name,
            base_url=clean_url,
            model=clean_model,
            api_key=clean_key,
            created_at=now,
            updated_at=now,
            max_tokens=max_tokens,
            context_window=context_window,
        )

    def delete(self, name: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute("DELETE FROM custom_models WHERE name = ?", (name,))
            return cursor.rowcount > 0
        finally:
            connection.close()

    def count_sessions_referencing(self, name: str) -> int:
        """有多少会话正在用这个模型（删除前提示用）。读不到 sessions 表就当 0。"""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE model = ?", (name,)
            ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            return 0
        finally:
            connection.close()

    @staticmethod
    def _to_model(row: sqlite3.Row) -> CustomModel:
        keys = row.keys()
        return CustomModel(
            name=str(row["name"]),
            base_url=str(row["base_url"]),
            model=str(row["model"]),
            api_key=str(row["api_key"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            max_tokens=int(row["max_tokens"]) if row["max_tokens"] is not None else None,
            context_window=(
                int(row["context_window"]) if "context_window" in keys and row["context_window"] is not None else None
            ),
        )