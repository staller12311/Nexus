"""文件类工具：read_file / write_file / list_dir。

安全模型（README「设计决策」第 59 条）：
- 主 workspace 根目录来自 ``AppConfig.resolve_path()`` 解析出的**绝对路径**；
- 附加目录通过**命名挂载**访问：``@libs/http/client.py``（``@`` 前缀避开文件名冲突，
  也让「相对路径永远只对主根解析」这条语义保持简单可预测）；
- 每次访问都用 ``Path.resolve()`` 归一（展开 ``..``、跟随符号链接），
  再用 ``is_relative_to()`` 判断是否仍在**命中的那个根**内 —— 顺序不能颠倒，
  否则 ``workspace/link -> C:\\Windows`` 这类符号链接会绕过检查；
- 附加挂载默认**只读**（``writable=False``）：读依赖库很常见，改坏共享库很致命。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ToolError

#: 单次读取上限（字符数按字节算），避免把整个大文件灌进上下文
MAX_READ_BYTES = 200_000
#: 单次列目录上限
MAX_LIST_ENTRIES = 200
#: 写权限失败时给用户/模型的可操作提示（WinError 5 的两大常见原因）
WRITE_PERMISSION_HINT = (
    "请检查：① 文件夹是否被设为只读（可在资源管理器取消，或执行 attrib -R \"路径\"）；"
    "② 后端是否用系统全局 Python 启动 —— 某些安全策略会把 venv 里的 python.exe 降为 "
    "Low 完整性级别，导致它只能写工作区目录、写外部工作区一律 WinError 5。"
)

#: 挂载名规则：小写字母/数字开头，允许 - 和 _，最长 16，避免和路径歧义
MOUNT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")


class WorkspaceViolation(ToolError):
    """请求路径越出 workspace。"""


@dataclass(frozen=True)
class Mount:
    """一个附加目录：通过 ``@name/relative/path`` 访问，默认只读。"""

    name: str
    path: Path
    writable: bool = False

    def __post_init__(self) -> None:
        if not MOUNT_NAME_PATTERN.match(self.name or ""):
            raise ValueError(
                f"挂载名 {self.name!r} 不合法：只允许小写字母/数字开头，"
                "可含 - 和 _，最长 16 个字符"
            )
        resolved = Path(self.path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"挂载目录不存在或不是目录：{resolved}")
        object.__setattr__(self, "path", resolved)

    @property
    def label(self) -> str:
        return f"@{self.name} -> {self.path}（{'读写' if self.writable else '只读'}）"


@dataclass(frozen=True)
class Workspace:
    """主根 + 若干命名挂载。

    ``root`` 必须是绝对路径（调用方用 ``AppConfig.resolve_path(agent.workspace)`` 得到）。
    ``extras`` 为空时行为与单根版本**完全一致**（相对路径、``../`` 拦截、错误文案都不变）。
    """

    root: Path
    extras: tuple[Mount, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        resolved_root = Path(self.root).resolve()
        object.__setattr__(self, "root", resolved_root)

        names: set[str] = set()
        paths: set[Path] = {resolved_root}
        for mount in self.extras:
            if mount.name in names:
                raise ValueError(f"挂载名重复：@{mount.name}")
            if mount.path in paths:
                raise ValueError(f"附加目录与已授权的根重复：{mount.path}")
            names.add(mount.name)
            paths.add(mount.path)

    # ---- 只读视图 ----
    @property
    def mounts(self) -> tuple[Mount, ...]:
        return self.extras

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.root, *(mount.path for mount in self.extras))

    def _mount_by_name(self, name: str) -> Mount | None:
        for mount in self.extras:
            if mount.name == name:
                return mount
        return None

    def _mount_for(self, resolved: Path) -> Mount | None:
        for mount in self.extras:
            if resolved.is_relative_to(mount.path):
                return mount
        return None

    def _authorized_roots_text(self) -> str:
        if not self.extras:
            return str(self.root)
        return "、".join([str(self.root), *(mount.label for mount in self.extras)])

    def _mount_names_text(self) -> str:
        if not self.extras:
            return "（无附加挂载）"
        return "、".join(f"@{mount.name}" for mount in self.extras)

    # ---- 解析（唯一闸门） ----
    def resolve(self, user_path: str | Path) -> Path:
        """把用户/模型给的路径解析为绝对路径；越界则抛错。"""
        return self.resolve_with_mount(user_path)[0]

    def resolve_with_mount(self, user_path: str | Path) -> tuple[Path, Mount | None]:
        """同 :meth:`resolve`，但额外返回命中的挂载（主根为 ``None``）。

        解析顺序：
        1. 绝对路径 —— 必须落在**某个**授权根内；
        2. ``@name/...`` —— 相对该挂载解析，且必须仍在该挂载内；
        3. 其它相对路径 —— 相对主根解析（与单根版本语义完全一致）。

        所有分支都是「先 resolve、再 is_relative_to」，所以 ``@libs/../../Windows``
        这类穿越写法仍然会在边界判定处被拒。
        """
        if user_path is None or not str(user_path).strip():
            raise WorkspaceViolation("路径不能为空")

        text = str(user_path).strip()
        candidate = Path(text)

        if candidate.is_absolute():
            resolved = candidate.resolve()
            mount = self._mount_for(resolved)
            if resolved.is_relative_to(self.root) or mount is not None:
                return resolved, mount
            raise WorkspaceViolation(self._violation_text(text, resolved))

        parts = candidate.parts
        if parts and parts[0].startswith("@"):
            name = parts[0][1:]
            mount = self._mount_by_name(name)
            if mount is None:
                raise WorkspaceViolation(
                    f"未知挂载 {parts[0]!r}：可用挂载 {self._mount_names_text()}"
                    f"（附加目录需用 @名字/路径 的形式访问）"
                )
            rest = Path(*parts[1:]) if len(parts) > 1 else Path(".")
            resolved = (mount.path / rest).resolve()
            if not resolved.is_relative_to(mount.path):
                raise WorkspaceViolation(
                    f"路径越出挂载 @{mount.name}，已拒绝：{text!r} -> {resolved}"
                    f"（@{mount.name} 根目录：{mount.path}）"
                )
            return resolved, mount

        # 相对路径：永远只对主根解析（附加目录必须显式 @name）
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise WorkspaceViolation(self._violation_text(text, resolved))
        return resolved, None

    def _violation_text(self, user_path: str, resolved: Path) -> str:
        return (
            f"路径越出 workspace，已拒绝：{user_path!r} -> {resolved}"
            f"（允许的根：{self._authorized_roots_text()}；"
            f"附加目录请用 @名字/路径 的形式访问：{self._mount_names_text()}）"
        )

    def relative(self, resolved: Path) -> str:
        # 附加挂载优先（更具体），输出带 @name 前缀，便于模型区分来源
        mount = self._mount_for(resolved)
        if mount is not None:
            try:
                inner = resolved.relative_to(mount.path).as_posix()
            except ValueError:  # pragma: no cover - _mount_for 已保证在内部
                inner = ""
            return f"@{mount.name}/{inner}" if inner else f"@{mount.name}"
        try:
            return resolved.relative_to(self.root).as_posix() or "."
        except ValueError:  # pragma: no cover - resolve() 已保证在内部
            return str(resolved)

    def contains(self, path: Path) -> bool:
        try:
            resolved = Path(path).resolve()
        except OSError:  # pragma: no cover - 路径不可解析
            return False
        return any(resolved.is_relative_to(root) for root in self.roots)


def _human_size(count: int) -> str:
    if count < 1024:
        return f"{count} 字节"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def read_file(workspace: Workspace, path: str, max_bytes: int = MAX_READ_BYTES) -> str:
    """读取 workspace 内的文本文件（UTF-8，按需截断）。"""
    target = workspace.resolve(path)
    relative = workspace.relative(target)

    if target.is_dir():
        raise ToolError(f"{relative} 是目录，不是文件；用 list_dir 查看目录内容。")
    if not target.exists():
        raise ToolError(f"文件不存在：{relative}（workspace 根目录：{workspace.root}）")
    if not target.is_file():
        raise ToolError(f"{relative} 不是普通文件，已拒绝读取。")

    try:
        data = target.read_bytes()
    except PermissionError as exc:
        raise ToolError(
            f"没有权限读取 {relative}（{exc.strerror or exc}）。"
            f"请检查文件权限，或让用户换一个可访问的工作区。"
        ) from exc
    except OSError as exc:
        raise ToolError(f"读取 {relative} 失败：{exc}") from exc

    limit = max(1, int(max_bytes))
    truncated = len(data) > limit
    text = data[:limit].decode("utf-8", errors="replace")

    header = f"# {relative}（{_human_size(len(data))}）"
    if truncated:
        header += f"\n# 注意：文件过大，只返回前 {_human_size(limit)}，其余已截断。"
    return f"{header}\n{text}"


def write_file(workspace: Workspace, path: str, content: str) -> str:
    """写入 workspace 内的文本文件（UTF-8，自动创建父目录）。

    附加挂载默认只读：命中只读挂载时直接拒绝，避免模型改坏共享依赖库。
    """
    if not isinstance(content, str):
        raise ToolError("content 必须是字符串")

    target, mount = workspace.resolve_with_mount(path)
    relative = workspace.relative(target)

    if mount is not None and not mount.writable:
        raise ToolError(
            f"@{mount.name} 是只读挂载（{mount.path}），不允许写入。"
            "如需写入，请让用户在配置里把该挂载设为可写。"
        )

    if target.is_dir():
        raise ToolError(f"{relative} 是目录，不能覆盖写入。")

    parent_is_new = not target.parent.exists()
    try:
        if parent_is_new:
            target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        target.write_bytes(data)
    except PermissionError as exc:
        raise ToolError(
            f"当前目录无写入权限，无法写入 {relative}（{exc.strerror or exc}）。"
            f"{WRITE_PERMISSION_HINT}"
        ) from exc
    except OSError as exc:
        raise ToolError(f"写入 {relative} 失败：{exc}") from exc

    note = "（已创建缺失的父目录）" if parent_is_new else ""
    return f"已写入 {relative}，{_human_size(len(data))}{note}"


def create_dir(workspace: Workspace, path: str) -> str:
    """在 workspace（或可写挂载）内创建目录（必要时创建多级父目录）。"""
    target, mount = workspace.resolve_with_mount(path)
    relative = workspace.relative(target)

    if mount is not None and not mount.writable:
        raise ToolError(
            f"@{mount.name} 是只读挂载（{mount.path}），不允许创建目录。"
        )

    if target.exists():
        if target.is_dir():
            return f"目录已存在：{relative}"
        raise ToolError(f"{relative} 已存在且是文件，不能当目录创建。")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ToolError(
            f"当前目录无写入权限，无法创建 {relative}（{exc.strerror or exc}）。"
            f"{WRITE_PERMISSION_HINT}"
        ) from exc
    except OSError as exc:
        raise ToolError(f"创建目录 {relative} 失败：{exc}") from exc
    return f"已创建目录：{relative}"


def _writable_mount_or_raise(workspace: Workspace, mount: Mount | None, action: str) -> None:
    if mount is not None and not mount.writable:
        raise ToolError(
            f"@{mount.name} 是只读挂载（{mount.path}），不允许{action}。"
            "如需修改，请让用户在配置里把该挂载设为可写。"
        )


def delete_path(workspace: Workspace, path: str) -> str:
    """删除 workspace 内的**文件或空目录**（非空目录需先删内容，避免误删整棵树）。"""
    target, mount = workspace.resolve_with_mount(path)
    _writable_mount_or_raise(workspace, mount, "删除")
    relative = workspace.relative(target)

    # 禁区：工作区根 / 挂载根 —— 删掉它们等于把沙箱连根拔了
    if target == workspace.root:
        raise ToolError("不能删除工作区根目录本身。")
    if mount is not None and target == mount.path:
        raise ToolError(f"不能删除挂载根目录 @{mount.name}。")

    if not target.exists() and not target.is_symlink():
        raise ToolError(f"路径不存在：{relative}")

    try:
        if target.is_dir() and not target.is_symlink():
            try:
                target.rmdir()  # 只能删空目录
            except OSError as exc:
                if getattr(exc, "winerror", None) == 145 or "not empty" in str(exc).lower():
                    raise ToolError(
                        f"{relative} 不是空目录，delete_path 只删除空目录。"
                        "请先删除里面的文件/子目录（可用 list_dir 看清结构后逐个删）。"
                    ) from exc
                raise
        else:
            target.unlink()
    except PermissionError as exc:
        raise ToolError(
            f"当前目录无写入权限，无法删除 {relative}（{exc.strerror or exc}）。"
            f"{WRITE_PERMISSION_HINT}"
        ) from exc
    except OSError as exc:
        raise ToolError(f"删除 {relative} 失败：{exc}") from exc
    return f"已删除：{relative}"


def move_path(workspace: Workspace, src: str, dest: str) -> str:
    """在 workspace 内移动或重命名文件/目录（相当于 mv）。"""
    source, src_mount = workspace.resolve_with_mount(src)
    target, dst_mount = workspace.resolve_with_mount(dest)
    _writable_mount_or_raise(workspace, src_mount, "移动")
    _writable_mount_or_raise(workspace, dst_mount, "写入")

    src_relative = workspace.relative(source)
    dst_relative = workspace.relative(target)

    if source == workspace.root or (src_mount is not None and source == src_mount.path):
        raise ToolError("不能移动工作区/挂载的根目录。")
    if not source.exists() and not source.is_symlink():
        raise ToolError(f"源路径不存在：{src_relative}")
    if target.exists():
        raise ToolError(f"目标已存在，拒绝覆盖：{dst_relative}（如需覆盖请先 delete_path）")
    if source == target:
        raise ToolError("源和目标相同，无需移动。")

    parent_is_new = not target.parent.exists()
    try:
        if parent_is_new:
            target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    except PermissionError as exc:
        raise ToolError(
            f"当前目录无写入权限，无法移动 {src_relative} -> {dst_relative}"
            f"（{exc.strerror or exc}）。{WRITE_PERMISSION_HINT}"
        ) from exc
    except OSError as exc:
        # 跨卷移动：rename 会失败，退回 shutil.move（复制 + 删除）
        try:
            import shutil

            shutil.move(str(source), str(target))
        except PermissionError as inner:
            raise ToolError(
                f"当前目录无写入权限，无法移动 {src_relative} -> {dst_relative}"
                f"（{inner.strerror or inner}）。{WRITE_PERMISSION_HINT}"
            ) from inner
        except OSError as inner:
            raise ToolError(f"移动 {src_relative} -> {dst_relative} 失败：{inner}") from inner
    note = "（已创建缺失的父目录）" if parent_is_new else ""
    return f"已移动：{src_relative} -> {dst_relative}{note}"


def list_dir(workspace: Workspace, path: str = ".") -> str:
    """列出 workspace 内某个目录的直接子项（目录在前，按名称排序）。"""
    target = workspace.resolve(path)
    relative = workspace.relative(target)

    if not target.exists():
        raise ToolError(f"目录不存在：{relative}（workspace 根目录：{workspace.root}）")
    if not target.is_dir():
        raise ToolError(f"{relative} 不是目录，是文件；用 read_file 读取。")

    try:
        entries = list(target.iterdir())
    except PermissionError as exc:
        raise ToolError(
            f"没有权限列出目录 {relative}（{exc.strerror or exc}）。"
            f"请检查文件夹权限。"
        ) from exc
    except OSError as exc:
        raise ToolError(f"读取目录 {relative} 失败：{exc}") from exc

    entries.sort(key=lambda item: (not item.is_dir(), item.name.lower()))
    truncated = len(entries) > MAX_LIST_ENTRIES
    shown = entries[:MAX_LIST_ENTRIES]

    lines = [f"# {relative}/（{len(entries)} 项）"]
    for entry in shown:
        # 越界标记对文件和目录都要算：目录联接/符号链接最容易在这里被忽略
        marker = ""
        if not workspace.contains(entry):
            marker = "  [符号链接或目录联接指向 workspace 外，访问已被拒绝]"
        if entry.is_dir():
            lines.append(f"{entry.name}/{marker}")
            continue
        size = ""
        try:
            size = f"  {_human_size(entry.stat().st_size)}"
        except OSError:
            size = "  ?"
        lines.append(f"{entry.name}{size}{marker}")

    if truncated:
        lines.append(f"...（还有 {len(entries) - MAX_LIST_ENTRIES} 项未显示）")

    # 列主根时把附加挂载作为虚拟条目列出来：模型才知道可以用 @name/ 访问
    if target == workspace.root and workspace.mounts:
        lines.append("")
        lines.append("# 附加挂载（用 @名字/路径 访问）：")
        for mount in workspace.mounts:
            lines.append(f"@{mount.name}/  -> {mount.path}（{'读写' if mount.writable else '只读'}）")
    return "\n".join(lines)