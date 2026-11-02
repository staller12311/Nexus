"""工作区黑名单的平台语义测试。

为什么单独测这个：FALLBACK_DENY 以前只有 Windows 路径，而 README 推荐用 Docker 部署，
Linux 才是真正的运行环境 —— 等于在最该拦的地方没拦。但补 POSIX 条目不能无条件加：
Windows 上 `Path("/etc")` 会解析成「当前盘符下的 \\etc」，无条件加会把 `D:\\etc`
这种正常用户目录一起挡掉。所以这份测试重点不是「有没有 /etc」，而是「按平台分」。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.service import workspace
from backend.service.workspace import _fallback_deny, validate_workspace
from backend.service.workspace import WorkspaceError

#: 审计点名的六个目录：Docker 部署时这些是真正的攻击面
AUDITED_POSIX_DIRS = ("/etc", "/var", "/usr", "/root", "/proc", "/sys")


def test_posix_paths_present_when_running_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    deny = _fallback_deny()
    for directory in AUDITED_POSIX_DIRS:
        assert directory in deny, f"POSIX 侧缺少对 {directory} 的拦截"


def test_posix_paths_absent_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 上不能带 POSIX 条目：否则 `Path('/etc')` → `D:\\etc` 会误伤用户目录。"""
    monkeypatch.setattr(os, "name", "nt")
    deny = _fallback_deny()
    for directory in AUDITED_POSIX_DIRS:
        assert directory not in deny


def test_windows_paths_kept_on_both_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 条目在 POSIX 下保留：多一条无害，少一条会在跨平台工作区里漏拦。"""
    monkeypatch.setattr(os, "name", "posix")
    assert r"c:\windows" in _fallback_deny()


def test_posix_entries_are_absolute_posix_paths() -> None:
    """POSIX 条目必须是 `/xxx` 形式，防止有人把盘符路径混进来导致永不命中。"""
    for entry in workspace._POSIX_FALLBACK_DENY:
        assert entry.startswith("/"), f"{entry} 不是 POSIX 绝对路径"
        assert ":" not in entry, f"{entry} 含盘符，混入了 Windows 语义"


def test_workspace_inside_protected_root_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用临时目录冒充系统目录，验证「受保护目录的子孙一律拒绝」这条判定真的生效。

    不依赖真实的 /etc：跨平台都能跑，也不需要有写系统目录的权限。
    """
    fake_system = tmp_path / "fake-system"
    inside = fake_system / "nested"
    inside.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMROOT", str(fake_system))
    monkeypatch.setattr(workspace, "_fallback_deny", lambda: ())

    with pytest.raises(WorkspaceError) as excinfo:
        validate_workspace(str(inside))
    assert "系统核心目录" in str(excinfo.value)
