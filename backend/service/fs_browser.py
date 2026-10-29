"""目录选择器的后端支持：只列目录、只建目录，绝不读文件内容。

安全边界（与 workspace 校验同一套思路，README 第 60 条）：
- 只返回**子目录名**，不返回文件、不返回任何文件内容；
- 拒绝列出受保护目录（系统根 / Program Files / ProgramData / 用户主目录整体 / AppData 等）；
- 路径必须存在、必须是目录、必须是绝对路径，不接受 UNC；
- 新建目录只能建在**可列出的**父目录下，名字只能是单段（不能含分隔符、不能是 . / ..）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .workspace import (
    WorkspaceError,
    _home_dir,
    _is_within,
    _protected_roots,
    _sensitive_user_dirs,
)

#: 单次列出的目录上限（避免超大目录把前端卡住）
MAX_ENTRIES = 500


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str


@dataclass(frozen=True)
class DirectoryListing:
    path: str
    parent: str | None
    entries: tuple[DirectoryEntry, ...]
    drives: tuple[str, ...]
    truncated: bool


class FsBrowseError(Exception):
    """目录浏览/新建失败（映射成 400）。"""


def _is_forbidden(path: Path) -> str | None:
    """返回拒绝原因（None 表示允许）。"""
    for protected in _protected_roots():
        if _is_within(path, protected):
            return f"出于安全考虑，不允许浏览系统目录：{path}"
    for sensitive in _sensitive_user_dirs():
        if _is_within(path, sensitive):
            return f"出于安全考虑，不允许浏览凭据/应用数据目录：{path}"
    home = _home_dir()
    if home is not None and path == home:
        return "出于安全考虑，不允许浏览整个用户主目录；请进入其中的具体项目文件夹"
    return None


def list_drives() -> tuple[str, ...]:
    """列出可用的盘符根（Windows）/ 根目录（其它平台）。"""
    if os.name != "nt":  # pragma: no cover - 本项目主战场是 Windows
        return (str(Path("/")),)
    drives: list[str] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                drives.append(f"{letter}:\\")
        except OSError:  # pragma: no cover - 光驱/未挂载
            continue
    return tuple(drives)


def list_directories(raw_path: str | None) -> DirectoryListing:
    """列出某个目录的直接子目录。``raw_path`` 为空时只返回盘符列表。"""
    text = (raw_path or "").strip().strip('"')

    if not text:
        return DirectoryListing(
            path="",
            parent=None,
            entries=(),
            drives=list_drives(),
            truncated=False,
        )

    if text.startswith("\\\\"):
        raise FsBrowseError("出于安全考虑，不支持浏览网络路径（UNC）")

    candidate = Path(text)
    if not candidate.is_absolute():
        raise FsBrowseError("请提供绝对路径（或以盘符开头的目录）")

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FsBrowseError(f"目录不存在：{text}") from exc
    except OSError as exc:
        raise FsBrowseError(f"目录无法解析：{text}") from exc

    if not resolved.is_dir():
        raise FsBrowseError(f"这不是一个文件夹：{resolved}")

    reason = _is_forbidden(resolved)
    if reason:
        raise FsBrowseError(reason)

    entries: list[DirectoryEntry] = []
    truncated = False
    try:
        with os.scandir(resolved) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:  # pragma: no cover - 权限/竞态
                    continue
                # 符号链接/目录联接指向受保护目录时不展示（避免点进去才报错）
                try:
                    child = Path(entry.path).resolve()
                except OSError:  # pragma: no cover
                    continue
                if _is_forbidden(child) is not None:
                    continue
                entries.append(DirectoryEntry(name=entry.name, path=str(child)))
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
    except PermissionError as exc:
        raise FsBrowseError(f"没有权限读取该目录：{resolved}") from exc
    except OSError as exc:
        raise FsBrowseError(f"读取目录失败：{resolved}（{exc}）") from exc

    entries.sort(key=lambda item: item.name.lower())

    parent: str | None = None
    if resolved.parent != resolved:
        parent_path = resolved.parent
        if _is_forbidden(parent_path) is None or parent_path in {Path(d) for d in list_drives()}:
            parent = str(parent_path)

    return DirectoryListing(
        path=str(resolved),
        parent=parent,
        entries=tuple(entries),
        drives=list_drives(),
        truncated=truncated,
    )


def create_directory(raw_parent: str, raw_name: str) -> str:
    """在父目录下新建一个文件夹，返回新目录的绝对路径。"""
    name = (raw_name or "").strip()
    if not name:
        raise FsBrowseError("文件夹名不能为空")
    if name in {".", ".."} or any(sep in name for sep in ("/", "\\", ":", "\0")):
        raise FsBrowseError("文件夹名不能包含路径分隔符")
    if len(name) > 100:
        raise FsBrowseError("文件夹名过长")

    listing = list_directories(raw_parent)  # 复用同样的安全校验
    if not listing.path:
        raise FsBrowseError("请先选择一个父目录")
    parent = Path(listing.path)
    target = parent / name
    reason = _is_forbidden(target.resolve())
    if reason:
        raise FsBrowseError(reason)
    if target.exists():
        raise FsBrowseError(f"已存在同名文件夹：{target}")
    try:
        target.mkdir(parents=False)
    except PermissionError as exc:
        raise FsBrowseError(
            f"没有权限在 {parent} 下创建文件夹（{exc.strerror or exc}）。"
            "常见原因：该目录只读、需要管理员权限，或者程序本身被上层沙箱/安全策略限制了写入。"
            "可以换一个你有写权限的位置（例如 D:\\Projects\\SharedLibs），"
            "或者先在文件资源管理器里手动建好这个文件夹，再回到这里选择它。"
        ) from exc
    except OSError as exc:
        raise FsBrowseError(f"创建文件夹失败：{exc}") from exc
    return str(target.resolve())


__all__ = [
    "DirectoryEntry",
    "DirectoryListing",
    "FsBrowseError",
    "WorkspaceError",
    "create_directory",
    "list_directories",
    "list_drives",
]