"""工作区切换（Workspace Switcher）：校验 + 应用 + 持久化。

安全边界（README 第 58 条）：
- 只接受**绝对路径**，且必须是**已存在的目录**（不接受文件、不接受 UNC 网络路径）；
- 拒绝盘符根目录（`C:\\`、`D:\\`）—— 那等于把整块盘交给模型；
- 拒绝系统核心目录：`%SYSTEMROOT%`（C:\\Windows）、`%PROGRAMFILES%`、`%ProgramFiles(x86)%`、
  `%PROGRAMDATA%`、`%USERPROFILE%`（用户主目录，里面全是隐私）；
- 判定用**解析后**的路径 + `is_relative_to`，先 resolve 再比较（与 `agent/tools/files.py` 同一套思路），
  所以 `D:\\x\\..\\..\\Windows` 这类穿越写法也会被拦住；
- 选择结果落库（`app_settings` 表），重启后仍然生效；用户可一键恢复默认。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent.storage import utc_now
from agent.tools.files import Mount

#: 设置项 key
WORKSPACE_KEY = "workspace_root"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: 兜底黑名单：即使环境变量缺失也要挡住这些众所周知的系统目录（小写、反斜杠统一）
FALLBACK_DENY = (
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata",
    r"c:\system volume information",
    r"c:\$recycle.bin",
    r"c:\boot",
    r"c:\recovery",
    r"c:\perflogs",
    r"c:\windows.old",
)

#: 用户主目录下这些子目录一律不许当工作区（凭据 / 应用数据 / 密码库）
SENSITIVE_HOME_SUBDIRS = (
    "AppData",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    ".gnupg",
    ".kube",
    ".docker",
    "NTUSER.DAT",
)


class WorkspaceError(Exception):
    """工作区路径不合法（映射成 400）。"""


def _resolve_quietly(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - 环境变量指向不存在的路径
        return None



#: 持久化格式：{"root": "...", "extras": [{"name": ..., "path": ..., "writable": ...}]}
def validate_extras(items: list[dict] | None) -> tuple[Mount, ...]:
    """把前端传的附加挂载列表校验成 Mount 元组（复用 validate_workspace 的路径策略）。"""
    mounts: list[Mount] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            raise WorkspaceError("附加目录格式不正确")
        name = str(item.get("name") or "").strip().lstrip("@")
        path = str(item.get("path") or "").strip()
        writable = bool(item.get("writable", False))
        resolved = validate_workspace(path)  # 系统目录/根目录/UNC/不存在 一并拦住
        try:
            mount = Mount(name=name, path=resolved, writable=writable)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc
        if mount.name in seen_names:
            raise WorkspaceError(f"附加目录名重复：@{mount.name}")
        key = str(mount.path).lower()
        if key in seen_paths:
            raise WorkspaceError(f"附加目录重复：{mount.path}")
        seen_names.add(mount.name)
        seen_paths.add(key)
        mounts.append(mount)
    return tuple(mounts)


def dump_settings(root: Path | None, extras: tuple[Mount, ...]) -> str | None:
    if root is None and not extras:
        return None
    payload = {
        "root": str(root) if root is not None else None,
        "extras": [
            {"name": m.name, "path": str(m.path), "writable": m.writable} for m in extras
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_settings(raw: str | None) -> tuple[Path | None, tuple[Mount, ...]]:
    """兼容旧格式（纯路径字符串）。"""
    if not raw:
        return None, ()
    text = raw.strip()
    if not text.startswith("{"):
        return Path(text), ()  # 老版本只存了主根路径
    data = json.loads(text)
    root_text = data.get("root")
    root = Path(root_text) if root_text else None
    extras: list[Mount] = []
    for item in data.get("extras") or []:
        try:
            extras.append(
                Mount(
                    name=str(item.get("name") or ""),
                    path=Path(str(item.get("path") or "")),
                    writable=bool(item.get("writable", False)),
                )
            )
        except (ValueError, OSError):
            continue  # 挂载目录已不存在：跳过它，不影响主根
    return root, tuple(extras)


def _protected_roots() -> list[Path]:
    """系统核心目录：整棵树都不许当工作区。"""
    candidates: list[Path] = []
    for env_name in ("SYSTEMROOT", "windir", "ProgramFiles", "ProgramFiles(x86)", "PROGRAMDATA"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value))
    candidates.extend(Path(raw) for raw in FALLBACK_DENY)

    protected: list[Path] = []
    for candidate in candidates:
        resolved = _resolve_quietly(candidate)
        if resolved is not None and resolved not in protected:
            protected.append(resolved)
    return protected


def _sensitive_user_dirs() -> list[Path]:
    """用户主目录里的敏感子目录（AppData / .ssh 等）：也不许当工作区。"""
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if not home:
        return []  # pragma: no cover - 非 Windows 且没有 HOME 的极端情况
    sensitive: list[Path] = []
    for name in SENSITIVE_HOME_SUBDIRS:
        resolved = _resolve_quietly(Path(home) / name)
        if resolved is not None:
            sensitive.append(resolved)
    return sensitive


def _home_dir() -> Path | None:
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    return _resolve_quietly(Path(home)) if home else None


@dataclass(frozen=True)
class WorkspaceValidation:
    """校验结果（便于上层拼错误信息，也方便测试断言）。"""

    path: Path
    reason: str


def validate_workspace(raw: str) -> Path:
    """把用户输入校验成可用的 workspace 绝对路径；不合法就抛 WorkspaceError。"""
    text = (raw or "").strip().strip('"')
    if not text:
        raise WorkspaceError("工作区路径不能为空")

    if text.startswith("\\\\"):
        raise WorkspaceError("出于安全考虑，不支持把网络路径（UNC）设为工作区")

    candidate = Path(text)
    if not candidate.is_absolute():
        raise WorkspaceError("请填写绝对路径，例如 D:\\MyProject")

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceError(f"路径不存在：{text}") from exc
    except OSError as exc:
        raise WorkspaceError(f"路径无法解析：{text}（{exc.__class__.__name__}）") from exc

    if not resolved.is_dir():
        raise WorkspaceError(f"这不是一个文件夹：{resolved}")

    # 盘符根目录（C:\、D:\）等于交出整块盘
    if resolved.parent == resolved:
        raise WorkspaceError("出于安全考虑，不能把整个磁盘根目录设为工作区")

    # 用户主目录本身也不行：里面一眼能看到所有个人文件、含敏感子目录
    home = _home_dir()
    if home is not None and resolved == home:
        raise WorkspaceError("出于安全考虑，不能把用户主目录整体设为工作区，请选择其中的具体项目目录")

    for protected in _protected_roots():
        if _is_within(resolved, protected):
            raise WorkspaceError(
                "出于安全考虑，禁止将系统核心目录设为工作区"
                f"（{resolved} 位于受保护目录 {protected} 之内）"
            )

    for sensitive in _sensitive_user_dirs():
        if _is_within(resolved, sensitive):
            raise WorkspaceError(
                "出于安全考虑，禁止把凭据/应用数据目录设为工作区"
                f"（{resolved} 位于 {sensitive} 之内）"
            )
    return resolved


def _is_within(path: Path, parent: Path) -> bool:
    """path 是否等于 parent 或位于其内部（不同盘符时安全返回 False）。"""
    try:
        return path == parent or path.is_relative_to(parent)
    except (ValueError, OSError):  # pragma: no cover - 不同盘符
        return False


class WorkspaceSettings:
    """把用户选的工作区落在同一个 SQLite 里（backend 自己管的表）。

    **每次操作短连接**，与 ``CustomModelStore`` 同一套做法。原因：``AgentService`` 是
    进程级单例，但 Web 路由跑在线程池里（uvicorn/TestClient 都是），任何跨请求复用的
    sqlite 连接都会在线程切换后抛 ``sqlite3.ProgrammingError``（默认
    ``check_same_thread=True`` 只允许创建它的线程使用）。短连接没有跨线程状态，
    也就不需要额外的锁；并发写的互斥交给 SQLite 自己的写锁 + ``timeout``。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            # 与 agent.storage / CustomModelStore 保持一致：WAL 下读写并发更友好
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:  # pragma: no cover - 只读卷等极端情况
            pass
        return connection

    def close(self) -> None:
        """兼容旧接口：短连接模式下没有常驻连接需要关闭（保留方法避免调用方报错）。"""
        return None

    def get(self) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (WORKSPACE_KEY,)
            ).fetchone()
            return str(row["value"]) if row and row["value"] else None
        finally:
            connection.close()

    def set(self, value: str | None) -> None:
        connection = self._connect()
        try:
            # with connection：正常结束提交、异常回滚
            with connection:
                if value is None:
                    connection.execute(
                        "DELETE FROM app_settings WHERE key = ?", (WORKSPACE_KEY,)
                    )
                    return
                connection.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (WORKSPACE_KEY, value, utc_now()),
                )
        finally:
            connection.close()