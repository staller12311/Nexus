"""只读代码搜索：``search_code``。

安全模型与文件工具一致：只遍历主 workspace，不跟随符号链接；
跳过 VCS / 依赖 / 临时目录，避免把 .venv、node_modules 和 .git 的内容灌进上下文。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import ToolError
from .files import Workspace

#: 单次搜索最多返回的命中数
MAX_RESULTS = 200
#: 单文件超过这个大小直接跳过（代码/文本文件很少需要这么大）
MAX_FILE_BYTES = 1_000_000
#: 单行内容显示上限，避免压缩后的 JS 一行几万字符撑爆上下文
MAX_LINE_CHARS = 300
#: 明确不进入的目录：VCS、依赖、缓存、构建产物、Agent 临时脚本
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".agent_tmp",
        "dist",
        "build",
    }
)


def _normalize_extension(file_extension: str) -> str:
    extension = (file_extension or "").strip().lower()
    if not extension:
        return ""
    if extension.startswith("."):
        extension = extension[1:]
    if not extension or "/" in extension or "\\" in extension or any(char.isspace() for char in extension):
        raise ToolError(
            f"file_extension 只能是扩展名（例如 py 或 .py），收到 {file_extension!r}"
        )
    return extension


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return True
    return b"\x00" in chunk


def search_code(workspace: Workspace, keyword: str, file_extension: str = "") -> str:
    """在 workspace 内按行搜索关键字，返回「相对路径:行号: 内容」。"""
    text = (keyword or "").strip()
    if not text:
        raise ToolError("keyword 不能为空")
    if "\n" in text or "\r" in text:
        raise ToolError("keyword 不能包含换行，请一次搜索一个词")
    if len(text) > 200:
        raise ToolError("keyword 过长（上限 200 字符）")

    extension = _normalize_extension(file_extension)
    if extension and any(sep in extension for sep in ("/", "\\")):
        raise ToolError(f"file_extension 必须是扩展名，收到 {file_extension!r}")

    root = workspace.root
    needle = text.casefold()
    hits: list[str] = []
    scanned = 0
    skipped_large = 0

    for current_dir, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name.lower() not in SKIP_DIRS
            and not (Path(current_dir) / name).is_symlink()
        )
        for filename in sorted(files):
            path = Path(current_dir) / filename
            if extension and path.suffix.lower() != f".{extension}":
                continue
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
            except OSError:
                continue
            if _is_binary(path):
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            relative = path.relative_to(root).as_posix()
            for line_number, line in enumerate(content.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                snippet = line.strip()
                if len(snippet) > MAX_LINE_CHARS:
                    snippet = snippet[:MAX_LINE_CHARS].rstrip() + "…"
                hits.append(f"{relative}:{line_number}: {snippet}")
                if len(hits) >= MAX_RESULTS:
                    break
            if len(hits) >= MAX_RESULTS:
                break
        if len(hits) >= MAX_RESULTS:
            break

    header = (
        f"搜索关键字 {text!r}"
        + (f"（扩展名 .{extension}）" if extension else "（全部文本/代码文件）")
        + f"，扫描 {scanned} 个文件"
    )
    if not hits:
        return f"{header}\n未找到匹配内容。"
    lines = [header, *hits]
    if len(hits) >= MAX_RESULTS:
        lines.append(f"...（命中数达到上限 {MAX_RESULTS}，请缩小关键字或扩展名范围）")
    if skipped_large:
        lines.append(f"（另有 {skipped_large} 个文件超过 {MAX_FILE_BYTES} 字节，已跳过）")
    return "\n".join(lines)